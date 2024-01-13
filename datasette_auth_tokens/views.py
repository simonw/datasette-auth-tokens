from datasette import Forbidden, Response, NotFound
from datasette.utils import (
    tilde_encode,
    tilde_decode,
    display_actor,
)
from .utils import ago_difference, format_permissions
import datetime
import json
import time

TOKEN_PAGE_SIZE = 30


async def create_api_token(request, datasette):
    await check_permission(datasette, request.actor)
    if request.method == "GET":
        return Response.html(
            await datasette.render_template(
                "create_api_token.html",
                await _shared(datasette, request),
                request=request,
            )
        )
    elif request.method == "POST":
        post = await request.post_vars()
        errors = []
        expires_after = None
        if post.get("expire_type"):
            duration_string = post.get("expire_duration")
            if (
                not duration_string
                or not duration_string.isdigit()
                or not int(duration_string) > 0
            ):
                errors.append("Invalid expire duration")
            else:
                unit = post["expire_type"]
                if unit == "minutes":
                    expires_after = int(duration_string) * 60
                elif unit == "hours":
                    expires_after = int(duration_string) * 60 * 60
                elif unit == "days":
                    expires_after = int(duration_string) * 60 * 60 * 24
                else:
                    errors.append("Invalid expire duration unit")

        # Are there any restrictions?
        restrict_all = []
        restrict_database = {}
        restrict_resource = {}

        for key in post:
            if key.startswith("all:") and key.count(":") == 1:
                restrict_all.append(key.split(":")[1])
            elif key.startswith("database:") and key.count(":") == 2:
                bits = key.split(":")
                database = tilde_decode(bits[1])
                action = bits[2]
                restrict_database.setdefault(database, []).append(action)
            elif key.startswith("resource:") and key.count(":") == 3:
                bits = key.split(":")
                database = tilde_decode(bits[1])
                resource = tilde_decode(bits[2])
                action = bits[3]
                restrict_resource.setdefault(database, {}).setdefault(
                    resource, []
                ).append(action)

        # Reuse Datasette signed tokens mechanism to create parts of the token
        throwaway_signed_token = datasette.create_token(
            request.actor["id"],
            expires_after=expires_after,
            restrict_all=restrict_all,
            restrict_database=restrict_database,
            restrict_resource=restrict_resource,
        )
        token_bits = datasette.unsign(
            throwaway_signed_token[len("dstok_") :], namespace="token"
        )
        permissions = token_bits.get("_r") or None

        config = Config(datasette)
        db = config.db
        cursor = await db.execute_write(
            """
            insert into _datasette_auth_tokens
            (secret_version, description, permissions, actor_id, created_timestamp, expires_after_seconds)
            values
            (:secret_version, :description, :permissions, :actor_id, :created_timestamp, :expires_after_seconds)
        """,
            {
                "secret_version": 0,
                "permissions": json.dumps(permissions),
                "description": post.get("description") or None,
                "actor_id": request.actor["id"],
                "created_timestamp": int(time.time()),
                "expires_after_seconds": expires_after,
            },
        )
        token = "dsatok_{}".format(datasette.sign(cursor.lastrowid, "dsatok"))

        context = await _shared(datasette, request)
        context.update({"errors": errors, "token": token, "token_bits": token_bits})
        return Response.html(
            await datasette.render_template(
                "create_api_token.html", context, request=request
            )
        )
    else:
        raise Forbidden("Invalid method")


async def check_permission(datasette, actor):
    if not actor or not actor.get("id"):
        raise Forbidden(
            "You must be logged in as an actor with an ID to create a token"
        )
    if not await datasette.permission_allowed(actor, "auth-tokens-create"):
        raise Forbidden("You do not have permission to create a token")


async def _shared(datasette, request):
    await check_permission(datasette, request.actor)
    db = Config(datasette).db

    tokens_exist = bool(
        (await db.execute("select 1 from _datasette_auth_tokens limit 1")).first()
    )
    # Build list of databases and tables the user has permission to view
    database_with_tables = []
    for database in datasette.databases.values():
        if database.name in ("_internal", "_memory"):
            continue
        if not await datasette.permission_allowed(
            request.actor, "view-database", database.name
        ):
            continue
        hidden_tables = await database.hidden_table_names()
        tables = []
        for table in await database.table_names():
            if table in hidden_tables:
                continue
            if not await datasette.permission_allowed(
                request.actor,
                "view-table",
                resource=(database.name, table),
            ):
                continue
            tables.append({"name": table, "encoded": tilde_encode(table)})
        database_with_tables.append(
            {
                "name": database.name,
                "encoded": tilde_encode(database.name),
                "tables": tables,
            }
        )
    return {
        "actor": request.actor,
        "all_permissions": [
            {"name": key, "description": value.description}
            for key, value in datasette.permissions.items()
            if key
            not in (
                "auth-tokens-create",
                "auth-tokens-revoke-all",
                "debug-menu",
                "permissions-debug",
            )
        ],
        "database_permissions": [
            {"name": key, "description": value.description}
            for key, value in datasette.permissions.items()
            if value.takes_database
        ],
        "resource_permissions": [
            {"name": key, "description": value.description}
            for key, value in datasette.permissions.items()
            if value.takes_resource
        ],
        "database_with_tables": database_with_tables,
        "tokens_exist": tokens_exist,
    }


async def tokens_index(datasette, request):
    from . import TOKEN_STATUSES, make_expire_function

    db = Config(datasette).db

    # Expire any tokens that are due for expiring
    await db.execute_write_fn(make_expire_function())

    next = request.args.get("next")

    where_bits = []
    params = {}
    if next:
        where_bits.append("id <= :next")
        params["next"] = next
    where = " and ".join(where_bits)

    # Users can only see their own tokens, unless they have the
    # auth-tokens-view-all permission
    if not await datasette.permission_allowed(request.actor, "auth-tokens-view-all"):
        where_bits.append("actor_id = :actor_id")
        params["actor_id"] = request.actor["id"] if request.actor else None

    tokens = [
        dict(row)
        for row in (
            await db.execute(
                """
                select * from _datasette_auth_tokens
                {where} order by id desc limit {limit}
            """.format(
                    where="where {}".format(where) if where else "",
                    limit=TOKEN_PAGE_SIZE + 1,
                ),
                params,
            )
        ).rows
    ]
    next = None
    if len(tokens) == TOKEN_PAGE_SIZE + 1:
        next = tokens[-1]["id"]
        tokens = tokens[:-1]

    for token in tokens:
        token["status"] = TOKEN_STATUSES.get(
            token["token_status"], token["token_status"]
        )

    # Resolve actors
    actor_ids = set([token["actor_id"] for token in tokens])
    actors = await datasette.actors_from_ids(list(actor_ids))
    for token in tokens:
        actor = actors.get(token["actor_id"])
        token["actor"] = actor
        token["actor_display"] = display_actor(actor) if actor else None

    def _format_permissions(json_string):
        return format_permissions(datasette, json.loads(json_string))

    return Response.html(
        await datasette.render_template(
            "tokens_index.html",
            {
                "tokens": tokens,
                "next": next,
                "is_first_page": not bool(request.args.get("next")),
                "timestamp": _timestamp,
                "ago_difference": ago_difference,
                "format_permissions": _format_permissions,
                "can_create_tokens": await datasette.permission_allowed(
                    request.actor, "auth-tokens-create"
                ),
            },
            request=request,
        )
    )


async def token_details(request, datasette):
    from . import TOKEN_STATUSES

    config = Config(datasette)
    db = config.db

    id = request.url_vars["id"]

    async def fetch_row():
        return (
            await db.execute("select * from _datasette_auth_tokens where id = ?", (id,))
        ).first()

    row = await fetch_row()
    if row is None:
        raise NotFound("Token not found")

    # User can manage if they own the token or they have auth-tokens-revoke-all
    if not await actor_can_view(datasette, request.actor, row["actor_id"]):
        raise Forbidden("You do not have permission to manage this token")

    can_revoke = await actor_can_revoke(datasette, request.actor, row["actor_id"])

    if (
        row["expires_after_seconds"]
        and (row["created_timestamp"] + row["expires_after_seconds"]) < time.time()
    ):
        await db.execute_write(
            "update _datasette_auth_tokens set token_status='E' where id=:token_id",
            {"token_id": id},
        )
        row = await fetch_row()

    if request.method == "POST":
        post_vars = await request.post_vars()
        if post_vars.get("revoke"):
            if not can_revoke:
                raise Forbidden("You do not have permission to revoke this token")
            else:
                await db.execute_write(
                    """
                    update _datasette_auth_tokens
                    set
                        token_status = 'R',
                        ended_timestamp = :now
                    where id = :id
                    """,
                    {"id": id, "now": int(time.time())},
                )
        return Response.redirect(request.path)

    restrictions = "None"
    permissions = json.loads(row["permissions"])
    if permissions:
        restrictions = format_permissions(datasette, permissions)

    actors = await datasette.actors_from_ids([row["actor_id"]])
    actor_display = None
    if actors and actors.get(row["actor_id"]):
        actor_display = display_actor(actors[row["actor_id"]])

    return Response.html(
        await datasette.render_template(
            "token_details.html",
            {
                "token": row,
                "actor_display": actor_display,
                "token_status": TOKEN_STATUSES.get(
                    row["token_status"], row["token_status"]
                ),
                "timestamp": _timestamp,
                "ago_difference": ago_difference,
                "restrictions": restrictions,
                "can_revoke": can_revoke,
            },
            request=request,
        )
    )


def _timestamp(ts):
    if ts:
        return datetime.datetime.fromtimestamp(ts).isoformat()
    else:
        return ""


async def actor_can_view(datasette, actor, token_actor_id):
    if not actor or not actor.get("id"):
        # Only works for actors that have an ID set
        return False
    if token_actor_id and str(token_actor_id) == str(actor.get("id")):
        return True
    # User with auth-tokens-view-all can view any token
    return await datasette.permission_allowed(actor, "auth-tokens-view-all")


async def actor_can_revoke(datasette, actor, token_actor_id):
    if not actor or not actor.get("id"):
        # Only works for actors that have an ID set
        return False
    if token_actor_id and str(token_actor_id) == str(actor.get("id")):
        return True
    # User with auth-tokens-revoke-all can revoke any token
    return await datasette.permission_allowed(actor, "auth-tokens-revoke-all")


class Config:
    def __init__(self, datasette):
        self._plugin_config = datasette.plugin_config("datasette-auth-tokens") or {}
        self._datasette = datasette
        self.enabled = self._plugin_config.get("manage_tokens")

    def get(self, key):
        return self._plugin_config.get(key)

    @property
    def db(self):
        db_name = self._plugin_config.get("manage_tokens_database") or None
        if db_name is None:
            return self._datasette.get_internal_database()
        else:
            return self._datasette.get_database(db_name)
