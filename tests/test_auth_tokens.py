from datasette.app import Datasette
import httpx
import pytest
import sqlite_utils


@pytest.fixture
async def ds(tmp_path_factory):
    db_directory = tmp_path_factory.mktemp("dbs")
    db_path1 = db_directory / "test1.db"
    sqlite_utils.Database(db_path1)["foo"].insert({"bar": 1})
    db_path2 = db_directory / "test2.db"
    db = sqlite_utils.Database(db_path2)
    db["tokens"].insert_all(
        [
            {
                "id": 1,
                "actor_id": "one",
                "actor_name": "Cleo",
                "token_secret": "oneone",
            },
            {
                "id": 2,
                "actor_id": "two",
                "actor_name": "Pancakes",
                "token_secret": "twotwo",
            },
        ],
        pk="id",
    )
    return Datasette(
        [db_path1, db_path2],
        metadata={
            "plugins": {
                "datasette-auth-tokens": {
                    "query": {
                        "sql": "select actor_id, actor_name, token_secret from tokens where id = :token_id",
                        "database": "test2",
                    },
                    "tokens": [
                        {"token": "one", "actor": {"id": "one"}},
                        {"token": "two", "actor": {"id": "two"}},
                    ],
                }
            },
            "databases": {"test1": {"allow_sql": {"id": "one"},}},
        },
    )
    return ds


@pytest.mark.parametrize(
    "token,path,expected_status",
    [
        ("", "/", 200),
        ("", "/test1?sql=select+1", 403),
        ("one", "/", 200),
        ("one", "/test1?sql=select+1", 200),
        ("two", "/", 200),
        ("two", "/test1?sql=select+1", 403),
    ],
)
@pytest.mark.asyncio
async def test_token(ds, token, path, expected_status):
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.get(
            "http://localhost{}".format(path),
            headers={"Authorization": "Bearer {}".format(token)},
        )
        assert expected_status == response.status_code


@pytest.mark.parametrize(
    "token,path,expected_status",
    [
        ("", "/", 200),
        ("", "/test1?sql=select+1", 403),
        ("1-oneone", "/", 200),
        ("1-oneone", "/test1?sql=select+1", 200),
        ("2-twotwo", "/", 200),
        ("2-twotwo", "/test1?sql=select+1", 403),
        ("invalid", "/", 200),
        ("invalid", "/test1?sql=select+1", 403),
    ],
)
@pytest.mark.asyncio
async def test_query(ds, token, path, expected_status):
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.get(
            "http://localhost{}".format(path),
            headers={"Authorization": "Bearer {}".format(token)},
        )
        assert expected_status == response.status_code


@pytest.mark.parametrize(
    "token,expected_actor",
    [
        ("1-oneone", {"id": "one", "name": "Cleo"}),
        ("2-twotwo", {"id": "two", "name": "Pancakes"}),
        ("invalid", None),
        ("invalid", None),
    ],
)
@pytest.mark.asyncio
async def test_actor(ds, token, expected_actor):
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.get(
            "http://localhost/-/actor.json",
            headers={"Authorization": "Bearer {}".format(token)},
        )
        assert {"actor": expected_actor} == response.json()
