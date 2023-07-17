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
        },
    )


@pytest.mark.parametrize("status", ("live", "revoked", "expired"))
@pytest.mark.asyncio
async def test_live_revoked_expired_tokens(ds_managed, status):
    token_id, token = await _create_token(ds_managed)
    expected_actor = {"id": "root", "token": "dsatok"}
    if status in ("revoked", "expired"):
        expected_actor = None
    if status == "revoked":
        await ds_managed.get_database().execute_write(
            "update _datasette_auth_tokens set token_status = 'R' where id=:id",
            {"id": token_id},
        )
    elif status == "expired":
        # Expire it by setting the created_timestamp and expires_after_seconds
        await ds_managed.get_database().execute_write(
            "update _datasette_auth_tokens set created_timestamp = :created, expires_after_seconds = 60 where id=:id",
            {"id": token_id, "created": time.time() - 120},
        )
    actor_response = await ds_managed.client.get(
        "/-/actor.json", headers={"Authorization": "Bearer {}".format(token)}
    )
    assert actor_response.status_code == 200
    assert actor_response.json() == {"actor": expected_actor}


async def _create_token(ds_managed):
    root_cookie = ds_managed.sign({"a": {"id": "root"}}, "actor")
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
@pytest.mark.asyncio
async def test_create_token(ds_managed, post_fields, expected_actor):
    # TODO: switch to ds_managed.client.actor_cookie after next Datasette release
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
    assert response.json()["actor"] == expected_actor
