from datasette_test import Datasette
from datasette.plugins import pm
from datasette import hookimpl
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
                "auth-tokens-create": {"id": "*"},
            },
        },
    )


@pytest_asyncio.fixture
async def ds_managed_is_member(db_path):
    class IsMemberPlugin:
        __name__ = "IsMemberPlugin"

        @hookimpl
        def permission_allowed(self, datasette, actor, action):
            if action == "auth-tokens-create":
                return actor.get("is_member", False)

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
    ds_csrftoken = create_page.cookies["ds_csrftoken"]
    post_fields = {}
    post_fields["csrftoken"] = ds_csrftoken
    response = await ds_managed.client.post(
        "/-/api/tokens/create",
        data=post_fields,
        cookies={"ds_actor": root_cookie, "ds_csrftoken": ds_csrftoken},
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
    # Extract ds_csrftoken
    ds_csrftoken = create_page.cookies["ds_csrftoken"]
    # Use that to create the token
    post_fields["csrftoken"] = ds_csrftoken
    response = await ds_managed.client.post(
        "/-/api/tokens/create",
        data=post_fields,
        cookies={"ds_actor": cookie, "ds_csrftoken": ds_csrftoken},
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
    # We always get CSRF token from /-/permissions
    csrftoken = (
        await ds_managed_is_member.client.get("/-/permissions", cookies=cookies)
    ).cookies["ds_csrftoken"]
    cookies["ds_csrftoken"] = csrftoken
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
        data={"csrftoken": csrftoken},
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

    csrftoken = "-"

    if not should_allow_view:
        assert response.status_code == 403
    else:
        assert response.status_code == 200
        csrftoken = response.cookies["ds_csrftoken"]
        cookies["ds_csrftoken"] = csrftoken
        # Is the revoke button present?
        if should_allow_revoke:
            assert 'name="revoke"' in response.text
        else:
            assert 'name="revoke"' not in response.text

    # Now try to revoke it
    revoke_response = await ds_managed.client.post(
        "/-/api/tokens/{}".format(token_id),
        data={"revoke": "1", "csrftoken": csrftoken},
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
