from datasette.app import Datasette
import httpx
import sqlite_utils
import pytest


def create_tables(conn):
    db = sqlite_utils.Database(conn)
    db["table_access"].insert_all(
        [
            {"user_id": 1, "database": "test", "table": "dogs"},
            {"user_id": 2, "database": "test", "table": "dogs"},
            {"user_id": 1, "database": "test", "table": "cats"},
        ]
    )
    db["cats"].insert({"name": "Casper"})
    db["dogs"].insert({"name": "Cleo"})
    db["other"].insert({"name": "Other"})

    # user_id = 3 is banned from 'sqlite_master'
    db["banned"].insert({"table": "other", "user_id": 3})


@pytest.fixture
async def ds(tmpdir):
    filepath = tmpdir / "test.db"
    ds = Datasette(
        [filepath],
        metadata={
            "plugins": {
                "datasette-permissions-sql": [
                    {
                        "action": "view-query",
                        "fallback": True,
                        "resource": ["test", "sqlite_master"],
                        "sql": """
                            SELECT
                                -1
                            FROM
                                banned
                            WHERE
                                user_id = :actor_id
                        """,
                    },
                    {
                        "action": "view-table",
                        "sql": """
                            SELECT
                                *
                            FROM
                                table_access
                            WHERE
                                user_id = :actor_id
                                AND "database" = :resource_1
                                AND "table" = :resource_2
                        """,
                    },
                ]
            },
            "databases": {
                "test": {
                    "allow_sql": {},
                    "queries": {"sqlite_master": "select * from sqlite_master"},
                }
            },
        },
    )
    await ds.get_database().execute_write_fn(create_tables, block=True)
    return ds


@pytest.mark.asyncio
async def test_ds_fixture(ds):
    assert {"table_access", "cats", "dogs", "banned", "other"} == set(
        await ds.get_database().table_names()
    )


@pytest.mark.parametrize(
    "actor,table,expected_status",
    [
        (None, "dogs", 403),
        (None, "cats", 403),
        ({"id": 1}, "dogs", 200),
        ({"id": 2}, "dogs", 200),
        ({"id": 1}, "cats", 200),
        ({"id": 2}, "cats", 403),
    ],
)
@pytest.mark.asyncio
async def test_permissions_sql(ds, actor, table, expected_status):
    async with httpx.AsyncClient(app=ds.app()) as client:
        cookies = {}
        if actor:
            cookies = {"ds_actor": ds.sign({"a": actor}, "actor")}
        response = await client.get(
            "http://localhost/test/{}".format(table), cookies=cookies
        )
        assert expected_status == response.status_code


@pytest.mark.parametrize(
    "actor,expected_status", [(None, 200), ({"id": 1}, 200), ({"id": 3}, 403),],
)
@pytest.mark.asyncio
async def test_fallback(ds, actor, expected_status):
    async with httpx.AsyncClient(app=ds.app()) as client:
        cookies = {}
        if actor:
            cookies = {"ds_actor": ds.sign({"a": actor}, "actor")}
        response = await client.get(
            "http://localhost/test/sqlite_master", cookies=cookies
        )
        assert expected_status == response.status_code
