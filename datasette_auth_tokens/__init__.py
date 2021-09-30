from datasette import hookimpl
import secrets


@hookimpl
def actor_from_request(datasette, request):
    async def inner():
        config = datasette.plugin_config("datasette-auth-tokens") or {}
        allowed_tokens = config.get("tokens") or []
        query_param = config.get("param")
        authorization = request.headers.get("authorization")
        if authorization:
            if not authorization.startswith("Bearer "):
                return None
            incoming_token = authorization[len("Bearer "):]
        elif query_param:
            query_param_token = request.args.get(query_param)
            if query_param_token:
                incoming_token = query_param_token
            else:
                return None
        else:
            return None
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
