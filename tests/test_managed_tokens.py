from datasette_test import Datasette
from datasette.plugins import pm
from datasette import hookimpl
from datasette.permissions import PermissionSQL
from datasette.tokens import TokenRestrictions
from datasette_auth_tokens import ManagedTokenHandler
import json
import pytest
import pytest_asyncio
import sqlite_utils
import time


class ActorsPlugin:
    __name__ = "ActorsPlugin"

    @hookimpl
    def actors_from_ids(self, datasette):
        return getattr(datasette, "_test_actors", {})


pm.register(ActorsPlugin(), name="undo_actors_plugin")


@pytest.fixture
def db_path(tmp_path_factory):
    db_directory = tmp_path_factory.mktemp("dbs")
    db_path = db_directory / "demo.db"
    sqlite_utils.Database(db_path)["foo"].insert({"bar": 1})
    return db_path


@pytest_asyncio.fixture
async def ds_managed(db_path):
    return Datasette(
        [db_path],
        plugin_config={
            "datasette-auth-tokens": {
                "manage_tokens": True,
                "param": "_auth_token",
            }
        },
        config={
            "permissions": {
                "auth-tokens-revoke-all": {"id": "admin"},
                "auth-tokens-view-all": {"id": "admin"},
                "auth-tokens-edit-all": {"id": "admin"},
                "auth-tokens-create": {"id": "*"},
            },
        },
    )


@pytest_asyncio.fixture
async def ds_managed_is_member(db_path):
    class IsMemberPlugin:
        __name__ = "IsMemberPlugin"

        @hookimpl
        def permission_resources_sql(self, datasette, actor, action):
            if (
                action == "auth-tokens-create"
                and actor
                and actor.get("is_member", False)
            ):
                return PermissionSQL.allow(reason="is-member")

    pm.register(IsMemberPlugin(), name="undo_is_member_plugin")
    try:
        yield Datasette(
            [db_path],
            plugin_config={
                "datasette-auth-tokens": {
                    "manage_tokens": True,
                    "param": "_auth_token",
                }
            },
        )
    finally:
        pm.unregister(name="undo_is_member_plugin")


# Alternative database fixture
@pytest_asyncio.fixture
async def ds_api_db(tmp_path_factory):
    db_directory = tmp_path_factory.mktemp("dbs")
    db_path = db_directory / "demo.db"
    sqlite_utils.Database(db_path)["foo"].insert({"bar": 1})
    api_db_path = db_directory / "api.db"
    sqlite_utils.Database(api_db_path)["comment"].insert({"this-is-for-tokens": 1})
    return Datasette(
        [db_path, api_db_path],
        plugin_config={
            "datasette-auth-tokens": {
                "manage_tokens": True,
                "param": "_auth_token",
                "manage_tokens_database": "api",
            }
        },
        config={
            "permissions": {
                "auth-tokens-create": {"id": "*"},
            },
        },
    )


@pytest.mark.asyncio
async def test_register_token_handler(ds_managed):
    """The plugin registers a ManagedTokenHandler with name='dsatok'."""
    handlers = ds_managed._token_handlers()
    names = [h.name for h in handlers]
    assert "dsatok" in names


@pytest.mark.asyncio
async def test_register_token_handler_disabled():
    """No handler is registered when manage_tokens is off."""
    ds = Datasette(memory=True)
    await ds.invoke_startup()
    handlers = ds._token_handlers()
    names = [h.name for h in handlers]
    assert "dsatok" not in names


@pytest.mark.asyncio
async def test_verify_token_via_datasette(ds_managed):
    """datasette.verify_token() resolves a dsatok_ token to the token actor."""
    token_id, token = await _create_token(ds_managed)
    actor = await ds_managed.verify_token(token)
    assert actor == {"id": "root", "token": "dsatok", "token_id": token_id}


@pytest.mark.asyncio
async def test_verify_token_rejects_bogus(ds_managed):
    """A bogus dsatok_ token returns None from datasette.verify_token()."""
    actor = await ds_managed.verify_token("dsatok_bad-token")
    assert actor is None


@pytest.mark.asyncio
async def test_verify_token_ignores_non_dsatok(ds_managed):
    """The dsatok handler returns None for non-dsatok_ tokens."""
    handler = next(h for h in ds_managed._token_handlers() if h.name == "dsatok")
    result = await handler.verify_token(ds_managed, "dstok_something-else")
    assert result is None


@pytest.mark.parametrize("status", ("active", "revoked", "expired", "invalid"))
@pytest.mark.parametrize("database", (None, "api"))
@pytest.mark.asyncio
async def test_active_revoked_expired_tokens(ds_managed, ds_api_db, status, database):
    if database is not None:
        ds_managed = ds_api_db
        db = ds_managed.get_database(database)
    else:
        db = ds_managed.get_internal_database()

    token_id, token = await _create_token(ds_managed)
    expected_actor = {"id": "root", "token": "dsatok", "token_id": token_id}
    if status in ("revoked", "expired"):
        expected_actor = None
    if status == "revoked":
        await db.execute_write(
            "update _datasette_auth_tokens set token_status = 'R' where id=:id",
            {"id": token_id},
        )
    elif status == "expired":
        # Expire it by setting the created_timestamp and expires_after_seconds
        await db.execute_write(
            "update _datasette_auth_tokens set created_timestamp = :created, expires_after_seconds = 60 where id=:id",
            {"id": token_id, "created": time.time() - 120},
        )
    elif status == "invalid":
        token = "dsatok_bad-token"
        expected_actor = None
    actor_response = await ds_managed.client.get(
        "/-/actor.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    assert actor_response.status_code == 200
    assert actor_response.json() == {"actor": expected_actor}


async def _create_token(ds_managed, actor_id="root"):
    root_cookie = ds_managed.client.actor_cookie({"id": actor_id})
    create_page = await ds_managed.client.get(
        "/-/api/tokens/create", cookies={"ds_actor": root_cookie}
    )
    assert create_page.status_code == 200
    response = await ds_managed.client.post(
        "/-/api/tokens/create",
        data={},
        cookies={"ds_actor": root_cookie},
    )
    assert response.status_code == 200
    api_token = response.text.split('class="copyable" style="width: 40%" value="')[
        1
    ].split('"')[0]
    # Decode token to find token ID
    token_id = ds_managed.unsign(api_token.split("dsatok_")[1], namespace="dsatok")
    return token_id, api_token


@pytest.mark.parametrize(
    "post_fields,expected_actor",
    [
        ({}, {"id": "root", "token": "dsatok"}),
        (
            {"resource:demo:foo:view-table": "1"},
            {"id": "root", "token": "dsatok", "_r": {"r": {"demo": {"foo": ["vt"]}}}},
        ),
        (
            {"database:demo:insert-row": "1"},
            {"id": "root", "token": "dsatok", "_r": {"d": {"demo": ["ir"]}}},
        ),
    ],
)
@pytest.mark.parametrize("database", (None, "api"))
@pytest.mark.parametrize("custom_actor_display", (False, True))
@pytest.mark.asyncio
async def test_create_token(
    ds_managed, ds_api_db, post_fields, expected_actor, database, custom_actor_display
):
    if database is not None:
        ds_managed = ds_api_db

    if custom_actor_display:
        ds_managed._test_actors = {
            "root": {
                "id": "root",
                "name": "Root",
            },
            "owner": {
                "id": "owner",
                "name": "Owner",
            },
        }
    else:
        ds_managed._test_actors = {}

    cookie = ds_managed.client.actor_cookie({"id": "root"})
    # Load initial create token page
    create_page = await ds_managed.client.get(
        "/-/api/tokens/create", cookies={"ds_actor": cookie}
    )
    assert create_page.status_code == 200
    response = await ds_managed.client.post(
        "/-/api/tokens/create",
        data=post_fields,
        cookies={"ds_actor": cookie},
    )
    assert response.status_code == 200
    api_token = response.text.split('class="copyable" style="width: 40%" value="')[
        1
    ].split('"')[0]
    assert api_token
    # Now try using it to request /-/actor.json
    response = await ds_managed.client.get(
        "/-/actor.json", headers={"Authorization": "Bearer {}".format(api_token)}
    )
    assert response.status_code == 200
    token_id = ds_managed.unsign(api_token.split("dsatok_")[1], namespace="dsatok")
    expected_actor["token_id"] = token_id
    assert response.json()["actor"] == expected_actor
    # Token should be visible in the HTML list
    response = await ds_managed.client.get(
        "/-/api/tokens", cookies={"ds_actor": cookie}
    )
    assert response.status_code == 200
    assert f'<a href="tokens/{token_id}">1&nbsp;-&nbsp;Active</a>' in response.text
    if custom_actor_display:
        assert "<td>Root (root)</td>" in response.text
    else:
        assert "<td>root</td>" in response.text
    # And should have its own page
    token_details = await ds_managed.client.get(
        f"/-/api/tokens/{token_id}", cookies={"ds_actor": cookie}
    )
    assert token_details.status_code == 200
    if custom_actor_display:
        assert "<dd>Root (root)</dd>" in token_details.text
    else:
        assert "<dd>root</dd>" in token_details.text


@pytest.mark.asyncio
@pytest.mark.parametrize("is_member", (False, True))
async def test_create_token_permissions(ds_managed_is_member, is_member):
    actor = {"id": "root", "is_member": is_member}
    cookies = {"ds_actor": ds_managed_is_member.client.actor_cookie(actor)}
    # tokens/create link should only show for users with permission to create tokens
    list_page = await ds_managed_is_member.client.get("/-/api/tokens", cookies=cookies)
    if is_member:
        assert 'href="tokens/create"' in list_page.text
    else:
        assert 'href="tokens/create"' not in list_page.text
    create_page = await ds_managed_is_member.client.get(
        "/-/api/tokens/create", cookies=cookies
    )
    if is_member:
        assert create_page.status_code == 200
    else:
        assert create_page.status_code == 403
    # Now try a POST to create a token
    response = await ds_managed_is_member.client.post(
        "/-/api/tokens/create",
        data={},
        cookies=cookies,
    )
    if is_member:
        assert response.status_code == 200
    else:
        assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,should_allow_view,should_allow_revoke",
    (
        ("owner", True, True),
        ("admin", True, True),
        ("other-user", False, False),
        ("anonymous", False, False),
    ),
)
async def test_token_permissions(
    ds_managed, scenario, should_allow_view, should_allow_revoke
):
    # Create a token
    token_id, _ = await _create_token(ds_managed, "owner")

    async def get_token(token_id):
        return (
            await ds_managed.get_internal_database().execute(
                "select * from _datasette_auth_tokens where id=:id",
                {"id": token_id},
            )
        ).first()

    assert (await get_token(token_id))["ended_timestamp"] is None

    if scenario != "anonymous":
        cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": scenario})}
    else:
        cookies = {}

    # Get the token details page
    response = await ds_managed.client.get(
        "/-/api/tokens/{}".format(token_id), cookies=cookies
    )

    if not should_allow_view:
        assert response.status_code == 403
    else:
        assert response.status_code == 200
        # Is the revoke button present?
        if should_allow_revoke:
            assert 'name="revoke"' in response.text
        else:
            assert 'name="revoke"' not in response.text

    # Now try to revoke it
    revoke_response = await ds_managed.client.post(
        "/-/api/tokens/{}".format(token_id),
        data={"revoke": "1"},
        cookies=cookies,
    )

    if should_allow_revoke:
        assert revoke_response.status_code == 302
        # Check token was revoked in the database
        token = await get_token(token_id)
        assert token["token_status"] == "R"
        assert token["ended_timestamp"]
    else:
        assert revoke_response.status_code == 403


@pytest.mark.asyncio
async def test_viewing_tokens_expires_some(ds_managed):
    # Viewing the /-/api/tokens page should expire any tokens that need it
    db = ds_managed.get_internal_database()
    token_id, _ = await _create_token(ds_managed)
    await db.execute_write(
        "update _datasette_auth_tokens set created_timestamp = :created, expires_after_seconds = 60 where id=:id",
        {"id": token_id, "created": time.time() - 120},
    )

    async def get_token():
        return (
            await db.execute(
                "select * from _datasette_auth_tokens where id=:token_id",
                {"token_id": token_id},
            )
        ).first()

    token = await get_token()
    assert token["token_status"] == "A"

    # Viewing the list of tokens should expire it
    response = await ds_managed.client.get(
        "/-/api/tokens",
        cookies={"ds_actor": ds_managed.client.actor_cookie({"id": "admin"})},
    )
    assert response.status_code == 200
    token = await get_token()
    assert token["token_status"] == "E"


@pytest.mark.asyncio
async def test_token_pagination(ds_managed):
    num_tokens = 100
    for i in range(num_tokens):
        await _create_token(ds_managed)
    cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": "admin"})}
    collected = []
    next_ = None
    pages = 0
    while True:
        path = "/-/api/tokens"
        if next_:
            path += "?next={}".format(next_)
        response = await ds_managed.client.get(path, cookies=cookies)
        pages += 1
        assert response.status_code == 200
        bits = response.text.split('<td><a href="tokens/')
        new_token_ids = []
        for bit in bits[1:]:
            token_id = bit.split('">')[0]
            new_token_ids.append(token_id)
        if '<a href="?next=' in response.text:
            next_ = response.text.split('<a href="?next=')[1].split('">')[0]
        else:
            next_ = None
        # Protect against infinite loops
        if any(id in collected for id in new_token_ids):
            assert False, "Infinite loop detected"
        collected.extend(new_token_ids)
        if next_ is None:
            break
    assert len(set(collected)) == num_tokens
    assert pages > 1


@pytest.mark.asyncio
async def test_tokens_cannot_be_restricted_to_auth_tokens_revoke_all(ds_managed):
    root_cookie = ds_managed.client.actor_cookie({"id": "root"})
    create_page = await ds_managed.client.get(
        "/-/api/tokens/create", cookies={"ds_actor": root_cookie}
    )
    assert "auth-tokens-revoke-all" not in create_page.text


@pytest.mark.asyncio
async def test_table_level_permissions_shown_under_database_heading(ds_managed):
    # Issue: table-level actions (e.g. insert-row) should be available as
    # checkboxes under each database heading so a token can be restricted to
    # insert-row across every table in that database.
    cookie = ds_managed.client.actor_cookie({"id": "root"})
    response = await ds_managed.client.get(
        "/-/api/tokens/create", cookies={"ds_actor": cookie}
    )
    assert response.status_code == 200
    # The "All tables in" heading should exist for the demo database
    assert 'All tables in "demo"' in response.text
    # Table-level permissions should render as database-scoped checkboxes
    for action in (
        "insert-row",
        "update-row",
        "delete-row",
        "view-table",
        "alter-table",
        "drop-table",
    ):
        assert (
            f'name="database:demo:{action}"' in response.text
        ), f"Expected database-scoped checkbox for {action}"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_a_table", (True, False))
async def test_no_table_heading_if_no_tables(tmpdir, has_a_table):
    # https://github.com/simonw/datasette-auth-tokens/issues/32
    db_path = str(tmpdir / "empty.db")
    db = sqlite_utils.Database(db_path)
    db.vacuum()
    if has_a_table:
        db["foo"].insert({"bar": 1})
    ds = Datasette(
        [db_path],
        plugin_config={"datasette-auth-tokens": {"manage_tokens": True}},
        config={
            "permissions": {
                "auth-tokens-create": {"id": "*"},
            },
        },
    )
    response = await ds.client.get(
        "/-/api/tokens/create",
        cookies={"ds_actor": ds.client.actor_cookie({"id": "admin"})},
    )
    assert response.status_code == 200
    fragment = ">Specific tables in specific databases<"
    if has_a_table:
        assert fragment in response.text
    else:
        assert fragment not in response.text


@pytest.mark.asyncio
async def test_query_param_token_authenticates(ds_managed):
    """A dsatok_ token passed via ?_auth_token= query string authenticates."""
    token_id, token = await _create_token(ds_managed)
    response = await ds_managed.client.get("/-/actor.json?_auth_token={}".format(token))
    assert response.status_code == 200
    assert response.json() == {
        "actor": {"id": "root", "token": "dsatok", "token_id": token_id}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restrictions,expected_r",
    [
        (
            TokenRestrictions().allow_database("demo", "insert-row"),
            {"d": {"demo": ["ir"]}},
        ),
        (
            TokenRestrictions().allow_resource("demo", "foo", "view-table"),
            {"r": {"demo": {"foo": ["vt"]}}},
        ),
    ],
)
async def test_query_param_token_with_restrictions(
    ds_managed, restrictions, expected_r
):
    """A dsatok_ token with _r restrictions sent via ?_auth_token= exposes
    those restrictions on the resolved actor."""
    await ds_managed.invoke_startup()
    handler = ManagedTokenHandler()
    token = await handler.create_token(ds_managed, "root", restrictions=restrictions)
    token_id = ds_managed.unsign(token[len("dsatok_") :], namespace="dsatok")
    response = await ds_managed.client.get("/-/actor.json?_auth_token={}".format(token))
    assert response.status_code == 200
    assert response.json() == {
        "actor": {
            "id": "root",
            "token": "dsatok",
            "token_id": token_id,
            "_r": expected_r,
        }
    }


@pytest.mark.asyncio
async def test_handler_create_token_stores_abbreviated_r(ds_managed):
    """ManagedTokenHandler.create_token stores abbreviated _r in the DB row
    and the verified actor exposes a matching _r dict."""
    await ds_managed.invoke_startup()
    handler = ManagedTokenHandler()
    restrictions = (
        TokenRestrictions()
        .allow_all("view-instance")
        .allow_database("demo", "insert-row")
        .allow_resource("demo", "foo", "view-table")
    )
    token = await handler.create_token(ds_managed, "root", restrictions=restrictions)
    assert token.startswith("dsatok_")

    expected_r = {
        "a": ["vi"],
        "d": {"demo": ["ir"]},
        "r": {"demo": {"foo": ["vt"]}},
    }

    token_id = ds_managed.unsign(token[len("dsatok_") :], namespace="dsatok")
    row = (
        await ds_managed.get_internal_database().execute(
            "select permissions from _datasette_auth_tokens where id=:id",
            {"id": token_id},
        )
    ).first()
    assert json.loads(row["permissions"]) == expected_r

    actor = await ds_managed.verify_token(token)
    assert actor == {
        "id": "root",
        "token": "dsatok",
        "token_id": token_id,
        "_r": expected_r,
    }


@pytest.mark.asyncio
async def test_checkbox_names_from_permissions(ds_managed):
    from datasette_auth_tokens.utils import checkbox_names_from_permissions

    await ds_managed.invoke_startup()
    permissions = {"a": ["vi"], "d": {"demo": ["ir"]}, "r": {"demo": {"foo": ["vt"]}}}
    names = checkbox_names_from_permissions(ds_managed, permissions)
    assert names == {
        "all:view-instance",
        "database:demo:insert-row",
        "resource:demo:foo:view-table",
    }


@pytest.mark.asyncio
async def test_checkbox_names_from_permissions_empty(ds_managed):
    from datasette_auth_tokens.utils import checkbox_names_from_permissions

    await ds_managed.invoke_startup()
    assert checkbox_names_from_permissions(ds_managed, None) == set()


@pytest.mark.asyncio
async def test_recent_token_usage_suggestions(ds_managed):
    from datasette_auth_tokens.utils import recent_token_usage

    token_id, token = await _create_token(ds_managed)
    await ds_managed.client.get(
        "/demo/foo.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    suggestions = await recent_token_usage(ds_managed, token_id)
    names = {s["name"] for s in suggestions}
    assert "resource:demo:foo:view-table" in names
    # Each suggestion carries a human-readable display string
    assert all(s["display"] for s in suggestions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,allowed",
    (
        ("owner", True),
        ("admin", True),
        ("other-user", False),
        ("anonymous", False),
    ),
)
async def test_edit_token_gating(ds_managed, scenario, allowed):
    token_id, _ = await _create_token(ds_managed, "owner")
    if scenario == "anonymous":
        cookies = {}
    else:
        cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": scenario})}
    response = await ds_managed.client.get(
        "/-/api/tokens/{}/edit".format(token_id), cookies=cookies
    )
    assert response.status_code == (200 if allowed else 403)


@pytest.mark.asyncio
async def test_edit_token_updates_permissions(ds_managed):
    token_id, token = await _create_token(ds_managed, "owner")
    cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": "owner"})}
    response = await ds_managed.client.post(
        "/-/api/tokens/{}/edit".format(token_id),
        data={"resource:demo:foo:view-table": "1"},
        cookies=cookies,
    )
    assert response.status_code == 302
    row = (
        await ds_managed.get_internal_database().execute(
            "select permissions from _datasette_auth_tokens where id=:id",
            {"id": token_id},
        )
    ).first()
    assert json.loads(row["permissions"]) == {"r": {"demo": {"foo": ["vt"]}}}
    # Effective restrictions change immediately, no re-issue needed
    actor_response = await ds_managed.client.get(
        "/-/actor.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    assert actor_response.json()["actor"]["_r"] == {"r": {"demo": {"foo": ["vt"]}}}


@pytest.mark.asyncio
async def test_edit_token_form_prechecks_current_restrictions(ds_managed):
    await ds_managed.invoke_startup()
    handler = ManagedTokenHandler()
    token = await handler.create_token(
        ds_managed,
        "owner",
        restrictions=TokenRestrictions().allow_resource("demo", "foo", "view-table"),
    )
    token_id = ds_managed.unsign(token[len("dsatok_") :], namespace="dsatok")
    cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": "owner"})}
    response = await ds_managed.client.get(
        "/-/api/tokens/{}/edit".format(token_id), cookies=cookies
    )
    assert response.status_code == 200
    assert 'name="resource:demo:foo:view-table" checked' in response.text


@pytest.mark.asyncio
async def test_edit_revoked_token_forbidden(ds_managed):
    token_id, _ = await _create_token(ds_managed, "owner")
    await ds_managed.get_internal_database().execute_write(
        "update _datasette_auth_tokens set token_status='R' where id=:id",
        {"id": token_id},
    )
    cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": "owner"})}
    response = await ds_managed.client.get(
        "/-/api/tokens/{}/edit".format(token_id), cookies=cookies
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_edit_page_shows_recent_usage_suggestion(ds_managed):
    token_id, token = await _create_token(ds_managed, "owner")
    await ds_managed.client.get(
        "/demo/foo.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": "owner"})}
    response = await ds_managed.client.get(
        "/-/api/tokens/{}/edit".format(token_id), cookies=cookies
    )
    assert response.status_code == 200
    assert "Used in the last" in response.text
    assert "demo/foo table: view-table" in response.text


@pytest.mark.asyncio
async def test_edit_page_lockdown_has_caveat(ds_managed):
    token_id, token = await _create_token(ds_managed, "owner")
    await ds_managed.client.get(
        "/demo/foo.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": "owner"})}
    response = await ds_managed.client.get(
        "/-/api/tokens/{}/edit".format(token_id), cookies=cookies
    )
    assert response.status_code == 200
    # The lockdown panel warns that only directly-accessed resources are
    # captured, not ones the token merely listed or browsed
    assert "accessed directly" in response.text


@pytest.mark.asyncio
async def test_details_page_has_edit_link(ds_managed):
    token_id, _ = await _create_token(ds_managed, "owner")
    cookies = {"ds_actor": ds_managed.client.actor_cookie({"id": "owner"})}
    response = await ds_managed.client.get(
        "/-/api/tokens/{}".format(token_id), cookies=cookies
    )
    assert 'href="{}/edit"'.format(token_id) in response.text


async def _usage_rows(ds, token_id):
    db = ds.get_internal_database()
    return [
        dict(r)
        for r in (
            await db.execute(
                "select * from auth_tokens_usage where token_id = :t order by id",
                {"t": token_id},
            )
        ).rows
    ]


@pytest.mark.asyncio
async def test_token_usage_is_recorded(ds_managed):
    """Using a token records the permission checks it triggered."""
    token_id, token = await _create_token(ds_managed)
    response = await ds_managed.client.get(
        "/demo/foo.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    assert response.status_code == 200
    rows = await _usage_rows(ds_managed, token_id)
    assert rows
    # Only checks attributed to this token are recorded
    assert all(r["token_id"] == token_id for r in rows)
    seen = {(r["action"], r["parent"], r["child"], r["result"]) for r in rows}
    assert ("view-table", "demo", "foo", 1) in seen


@pytest.mark.asyncio
async def test_token_usage_not_duplicated(ds_managed):
    """A later request must not re-insert checks that are still in the
    in-memory deque from an earlier request."""
    token_id, token = await _create_token(ds_managed)
    await ds_managed.client.get(
        "/demo/foo.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    first = await _usage_rows(ds_managed, token_id)
    assert first
    # An anonymous request produces no new token checks
    await ds_managed.client.get("/demo/foo.json")
    second = await _usage_rows(ds_managed, token_id)
    assert len(second) == len(first)


@pytest.mark.asyncio
async def test_token_usage_can_be_disabled(db_path):
    ds = Datasette(
        [db_path],
        plugin_config={
            "datasette-auth-tokens": {
                "manage_tokens": True,
                "param": "_auth_token",
                "log_token_usage": False,
            }
        },
        config={"permissions": {"auth-tokens-create": {"id": "*"}}},
    )
    token_id, token = await _create_token(ds)
    await ds.client.get(
        "/demo/foo.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    db = ds.get_internal_database()
    count = (await db.execute("select count(*) from auth_tokens_usage")).first()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_handler_create_token_when_signed_tokens_disabled(db_path):
    """Creating a managed token with restrictions must work even when the
    Datasette instance has allow_signed_tokens disabled -- the dsatok handler
    should not depend on the signed-token handler."""
    ds = Datasette(
        [db_path],
        plugin_config={
            "datasette-auth-tokens": {"manage_tokens": True},
        },
        config={
            "permissions": {"auth-tokens-create": {"id": "*"}},
        },
        settings={"allow_signed_tokens": False},
    )
    await ds.invoke_startup()
    handler = ManagedTokenHandler()
    restrictions = TokenRestrictions().allow_database("demo", "insert-row")
    token = await handler.create_token(ds, "root", restrictions=restrictions)
    assert token.startswith("dsatok_")

    token_id = ds.unsign(token[len("dsatok_") :], namespace="dsatok")
    row = (
        await ds.get_internal_database().execute(
            "select permissions from _datasette_auth_tokens where id=:id",
            {"id": token_id},
        )
    ).first()
    assert json.loads(row["permissions"]) == {"d": {"demo": ["ir"]}}
