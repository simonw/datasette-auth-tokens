from datasette.app import Datasette
import pytest
import pytest_asyncio
import sqlite_utils


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
    print(create_page.text)
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
