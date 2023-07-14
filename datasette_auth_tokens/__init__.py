from datasette import hookimpl
import json
import secrets
import time
from .views import create_api_token

CREATE_TABLES_SQL = """
CREATE TABLE _datasette_auth_tokens (
   id INTEGER PRIMARY KEY,
   secret_id INTEGER,
   description TEXT,
   permissions TEXT,
   actor_id TEXT,
   created_timestamp INTEGER,
   last_used_timestamp INTEGER,
   expires_after_seconds INTEGER
);
"""


@hookimpl
def startup(datasette):
    config = _config(datasette)
    if not config.get("manage_tokens"):
        return

    async def inner():
        db = datasette.get_database()
        if "_datasette_auth_tokens" not in await db.table_names():
            await db.execute_write(CREATE_TABLES_SQL)

    return inner


@hookimpl
def register_routes(datasette):
    config = _config(datasette)
    if not config.get("manage_tokens"):
        return
    return [(r"^/-/api/tokens/create$", create_api_token)]


@hookimpl
def actor_from_request(datasette, request):
    async def inner():
        config = _config(datasette)
        allowed_tokens = config.get("tokens") or []
        query_param = config.get("param")
        authorization = request.headers.get("authorization")
        if authorization:
            if not authorization.startswith("Bearer "):
                return None
            incoming_token = authorization[len("Bearer ") :]
        elif query_param:
            query_param_token = request.args.get(query_param)
            if query_param_token:
                incoming_token = query_param_token
            else:
                return None
        else:
            return None

        if config.get("manage_tokens"):
            return await _actor_from_managed(datasette, incoming_token)

        # First try hard-coded tokens in the list
        for token in allowed_tokens:
            if secrets.compare_digest(token["token"], incoming_token):
                return token["actor"]
        # Now try the SQL query, if present
        query = config.get("query")
        if query:
            if "-" not in incoming_token:
                # Invalid token
                return None
            token_id, token_secret = incoming_token.split("-", 2)
            sql = query["sql"]
            database = query.get("database")
            db = datasette.get_database(database)
            results = await db.execute(sql, {"token_id": token_id})
            if not results:
                return None
            row = results.first()
            assert (
                "token_secret" in row.keys()
            ), "Returned row must contain a token_secret"
            if secrets.compare_digest(row["token_secret"], token_secret):
                # Set actor based on actor_* columns
                return {
                    k.replace("actor_", ""): row[k]
                    for k in row.keys()
                    if k.startswith("actor_")
                }

    return inner


def _config(datasette):
    return datasette.plugin_config("datasette-auth-tokens") or {}


async def _actor_from_managed(datasette, incoming_token):
    db = datasette.get_database()
    if not incoming_token.startswith("dsatok_"):
        return None
    incoming_token = incoming_token[len("dsatok_") :]
    token_id = datasette.unsign(incoming_token, "dsatok")
    results = await db.execute(
        "select * from _datasette_auth_tokens where id=:token_id",
        {"token_id": token_id},
    )
    row = results.first()
    if not row:
        return None

    # TODO: check expiry

    actor = {"id": row["actor_id"], "token": "dsatok"}
    permissions = json.loads(row["permissions"])
    if permissions:
        actor["_r"] = permissions

    # Update last_used_timestamp if more than 60 seconds old
    if row["last_used_timestamp"] is None or (
        row["last_used_timestamp"] < (time.time() - 60)
    ):
        await db.execute_write(
            "update _datasette_auth_tokens set last_used_timestamp=:now where id=:token_id",
            {"now": int(time.time()), "token_id": token_id},
        )

    return actor
