from datasette.app import Datasette
import httpx
import pytest


@pytest.fixture
async def ds():
    return Datasette(
        [],
        memory=True,
        metadata={
            "plugins": {
                "datasette-auth-tokens": {
                    "tokens": [
                        {"token": "one", "actor": {"id": "one"}},
                        {"token": "two", "actor": {"id": "two"}},
                    ]
                }
            },
            "databases": {":memory:": {"allow_sql": {"id": "one"},}},
        },
    )
    return ds


@pytest.mark.parametrize(
    "token,path,expected_status",
    [
        ("", "/", 200),
        ("", "/:memory:?sql=select+1", 403),
        ("one", "/", 200),
        ("one", "/:memory:?sql=select+1", 200),
        ("two", "/", 200),
        ("two", "/:memory:?sql=select+1", 403),
    ],
)
@pytest.mark.asyncio
async def test_fallback(ds, token, path, expected_status):
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.get(
            "http://localhost{}".format(path),
            headers={"Authorization": "Bearer {}".format(token)},
        )
        assert expected_status == response.status_code
