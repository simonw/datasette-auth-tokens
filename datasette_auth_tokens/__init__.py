from datasette import hookimpl
import secrets


@hookimpl
def actor_from_request(datasette, request):
    allowed_tokens = datasette.plugin_config("datasette-auth-tokens") or []
    print(allowed_tokens)
    print(request.headers)
    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    if not authorization.startswith("Bearer "):
        return None
    incoming_token = authorization[len("Bearer ") :]
    for token in allowed_tokens:
        if secrets.compare_digest(token["token"], incoming_token):
            return token["actor"]
