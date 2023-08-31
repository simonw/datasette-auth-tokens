from datasette.app import Datasette
import pytest
import pytest_asyncio
import sqlite_utils
import time


@pytest_asyncio.fixture
async def ds_managed(tmp_path_factory):
    db_directory = tmp_path_factory.mktemp("dbs")
    db_path = db_directory / "demo.db"
    sqlite_utils.Database(db_path)["foo"].insert({"bar": 1})
    return Datasette(
        [db_path],
        metadata={
            "plugins": {
                "datasette-auth-tokens": {
                    "manage_tokens": True,
                    "param": "_auth_token",
                }
            },
            "permissions": {"auth-tokens-revoke-any": {"id": "admin"}},
        },
    )


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
        metadata={
            "plugins": {
                "datasette-auth-tokens": {
                    "manage_tokens": True,
                    "param": "_auth_token",
                    "manage_tokens_database": "api",
                }
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
    root_cookie = ds_managed.sign({"a": {"id": actor_id}}, "actor")
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
@pytest.mark.asyncio
async def test_create_token(
    ds_managed, ds_api_db, post_fields, expected_actor, database
):
    # TODO: switch to ds_managed.client.actor_cookie after next Datasette release
    if database is not None:
        ds_managed = ds_api_db
    cookie = ds_managed.sign({"a": {"id": "root"}}, "actor")
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
    expected_actor["token_id"] = ds_managed.unsign(
        api_token.split("dsatok_")[1], namespace="dsatok"
    )
    assert response.json()["actor"] == expected_actor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,should_allow_revoke",
    (
        ("owner", True),
        ("admin", True),
        ("other-user", False),
        ("anonymous", False),
    ),
)
async def test_revoke_permissions(ds_managed, scenario, should_allow_revoke):
    # Create a token
    token_id, _ = await _create_token(ds_managed, "owner")

    if scenario != "anonymous":
        cookies = {"ds_actor": ds_managed.sign({"a": {"id": scenario}}, "actor")}
    else:
        cookies = {}

    # Get the token details page
    response = await ds_managed.client.get(
        "/-/api/tokens/{}".format(token_id), cookies=cookies
    )

    csrftoken = "-"

    if not should_allow_revoke:
        assert response.status_code == 403
    else:
        assert response.status_code == 200
        csrftoken = response.cookies["ds_csrftoken"]
        cookies["ds_csrftoken"] = csrftoken
        # Is the revoke button present?
        if scenario in ("owner", "admin"):
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
        row = await ds_managed.get_internal_database().execute(
            "select token_status from _datasette_auth_tokens where id=:id",
            {"id": token_id},
        )
        token_status = row.first()["token_status"]
        assert token_status == "R"
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
        cookies={"ds_actor": ds_managed.sign({"a": {"id": "admin"}}, "actor")},
    )
    assert response.status_code == 200
    token = await get_token()
    assert token["token_status"] == "E"
