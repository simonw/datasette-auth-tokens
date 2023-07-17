from datasette import hookimpl, Forbidden
import json
import secrets
import time
from markupsafe import Markup
from .views import create_api_token, check_permission, tokens_index, token_details

CREATE_TABLES_SQL = """
CREATE TABLE _datasette_auth_tokens (
    id INTEGER PRIMARY KEY,
    token_status TEXT DEFAULT 'L', -- [L]ive, [R]evoked, [E]xpired
    description TEXT,
    actor_id TEXT,
    permissions TEXT,
    created_timestamp INTEGER,
    last_used_timestamp INTEGER,
    expires_after_seconds INTEGER,
    secret_version INTEGER DEFAULT 0
);
"""

TOKEN_STATUSES = {
    "L": "Live",
    "R": "Revoked",
    "E": "Expired",
}


@hookimpl
def table_actions(datasette, actor, database, table):
    if actor and table == "_datasette_auth_tokens":
        return menu_links(datasette, actor)


@hookimpl
def menu_links(datasette, actor):
    if not actor:
        return
    try:
        check_permission(datasette, actor)
    except Forbidden:
        return
    return [
        {
            "href": datasette.urls.path("/-/api/tokens/create"),
            "label": "Create API token",
        }
    ]


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
    return [
        (r"^/-/api/tokens/create$", create_api_token),
        (r"^/-/api/tokens$", tokens_index),
        (r"^/-/api/tokens/(?P<id>\d+)$", token_details),
    ]


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

    actor = {"id": row["actor_id"], "token": "dsatok"}
    permissions = json.loads(row["permissions"])
    if permissions:
        actor["_r"] = permissions

    # Is token revoked?
    if row["token_status"] == "R":
        return None

    # Expired?
    if row["token_status"] == "E":
        return None

    # Also expire if it just hit expiry
    if (
        row["expires_after_seconds"]
        and (row["created_timestamp"] + row["expires_after_seconds"]) < time.time()
    ):
        await db.execute_write(
            "update _datasette_auth_tokens set token_status='E' where id=:token_id",
            {"token_id": token_id},
        )
        return None

    # Update last_used_timestamp if more than 60 seconds old
    if row["last_used_timestamp"] is None or (
        row["last_used_timestamp"] < (time.time() - 60)
    ):
        await db.execute_write(
            "update _datasette_auth_tokens set last_used_timestamp=:now where id=:token_id",
            {"now": int(time.time()), "token_id": token_id},
        )

    return actor


@hookimpl
def render_cell(value, column, table, row):
    if table != "_datasette_auth_tokens":
        return None
    if column.endswith("_timestamp"):
        return value and time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    if column != "token_status":
        return None
    return Markup(
        ('<strong>{status}</strong><br><a href="/-/api/tokens/{id}">{link}</a>').format(
            status=TOKEN_STATUSES.get(value, value),
            id=row["id"],
            link="edit / revoke" if value == "L" else "view",
        )
    )
