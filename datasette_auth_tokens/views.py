from datasette import Forbidden, Response
from datasette.utils import (
    tilde_encode,
    tilde_decode,
)
import json
import secrets
import time


async def create_api_token(request, datasette):
    _check_permission(datasette, request)
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

        db = datasette.get_database()
        cursor = await db.execute_write(
            """
            insert into _datasette_auth_tokens
            (secret_id, description, permissions, actor_id, created_timestamp, expires_after_seconds)
            values
            (:secret_id, :description, :permissions, :actor_id, :created_timestamp, :expires_after_seconds)
        """,
            {
                "secret_id": 0,
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


def _check_permission(datasette, request):
    if not request.actor:
        raise Forbidden("You must be logged in to create a token")
    if not request.actor.get("id"):
        raise Forbidden(
            "You must be logged in as an actor with an ID to create a token"
        )
    if request.actor.get("token"):
        raise Forbidden(
            "Token authentication cannot be used to create additional tokens"
        )


async def _shared(datasette, request):
    _check_permission(datasette, request)
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
        "all_permissions": datasette.permissions.keys(),
        "database_permissions": [
            key for key, value in datasette.permissions.items() if value.takes_database
        ],
        "resource_permissions": [
            key for key, value in datasette.permissions.items() if value.takes_resource
        ],
        "database_with_tables": database_with_tables,
    }
