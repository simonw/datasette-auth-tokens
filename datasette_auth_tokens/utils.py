from datasette.utils import tilde_encode
from typing import Optional
import time

# Per token, retain the larger of {last 5 minutes, newest 200 records}, capped
# at 1000 rows total.
USAGE_RETENTION_SECONDS = 5 * 60
USAGE_RETENTION_MS = USAGE_RETENTION_SECONDS * 1000
USAGE_RETENTION_RECENT_RECORDS = 200
USAGE_RETENTION_MAX_RECORDS = 1000


def prune_token_usage(conn, token_id, now_ms):
    """Trim auth_tokens_usage rows for a single token to the retention policy.

    Keeps the union of {rows within the last 5 minutes} and {the newest 200
    rows}, then caps that union at the newest 1000 rows, deleting the rest.
    """
    conn.execute(
        """
        DELETE FROM auth_tokens_usage
        WHERE token_id = :tid AND id NOT IN (
            SELECT id FROM auth_tokens_usage
            WHERE token_id = :tid AND (
                created_ms >= :five_min_ago
                OR id IN (
                    SELECT id FROM auth_tokens_usage
                    WHERE token_id = :tid ORDER BY id DESC LIMIT :recent
                )
            )
            ORDER BY id DESC LIMIT :cap
        )
        """,
        {
            "tid": token_id,
            "five_min_ago": now_ms - USAGE_RETENTION_MS,
            "recent": USAGE_RETENTION_RECENT_RECORDS,
            "cap": USAGE_RETENTION_MAX_RECORDS,
        },
    )


def pluralize(n, unit):
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def ago_difference(time1: int, time2: Optional[int] = None):
    if time1 is None:
        return ""
    if time2 is None:
        time2 = int(time.time())
    delta = time1 - time2
    future = True
    if delta < 0:
        future = False
        delta = time2 - time1

    days, remainder = divmod(delta, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    days = int(days)
    hours = int(hours)
    minutes = int(minutes)
    seconds = int(seconds)
    parts = []
    if days > 0:
        parts.append(pluralize(days, "day"))
    if hours > 0:
        parts.append(pluralize(hours, "hour"))
    if minutes > 0:
        parts.append(pluralize(minutes, "min"))
    if hours == 0 and seconds > 0:
        parts.append(pluralize(seconds, "sec"))

    combined = " ".join(parts)
    if not combined.strip():
        return ""
    if future:
        return "In {}".format(combined)
    else:
        return "{} ago".format(combined)


def abbr_to_name(datasette):
    """Map each action's abbreviation back to its full name."""
    return {
        action.abbr: action.name
        for action in datasette.actions.values()
        if action.abbr
    }


def checkbox_names_from_permissions(datasette, permissions):
    """Reverse an abbreviated _r permissions dict into the set of checkbox
    `name` strings used by the create/edit token forms, so a form can be
    pre-checked to match a token's current restrictions."""
    if not permissions:
        return set()
    abbreviations = abbr_to_name(datasette)

    def name(code):
        return abbreviations.get(code, code)

    names = set()
    for code in permissions.get("a", []):
        names.add("all:{}".format(name(code)))
    for database, codes in permissions.get("d", {}).items():
        for code in codes:
            names.add("database:{}:{}".format(tilde_encode(database), name(code)))
    for database, tables in permissions.get("r", {}).items():
        for table, codes in tables.items():
            for code in codes:
                names.add(
                    "resource:{}:{}:{}".format(
                        tilde_encode(database), tilde_encode(table), name(code)
                    )
                )
    return names


def _usage_suggestion(action, parent, child):
    """Map a (action, parent, child) usage row to a checkbox name + display."""
    if child is not None:
        return {
            "name": "resource:{}:{}:{}".format(
                tilde_encode(parent), tilde_encode(child), action
            ),
            "display": "{}/{} table: {}".format(parent, child, action),
            "action": action,
            "parent": parent,
            "child": child,
        }
    if parent is not None:
        return {
            "name": "database:{}:{}".format(tilde_encode(parent), action),
            "display": "{} database: {}".format(parent, action),
            "action": action,
            "parent": parent,
            "child": child,
        }
    return {
        "name": "all:{}".format(action),
        "display": "all databases and tables: {}".format(action),
        "action": action,
        "parent": parent,
        "child": child,
    }


async def recent_token_usage(datasette, token_id, within_seconds=USAGE_RETENTION_SECONDS):
    """Distinct actions a token successfully exercised within the time window,
    as form-checkbox suggestions for the "lock it down" feature."""
    from .views import Config

    db = Config(datasette).db
    cutoff_ms = int((time.time() - within_seconds) * 1000)
    rows = (
        await db.execute(
            """
            select distinct action, parent, child
            from auth_tokens_usage
            where token_id = :token_id and result = 1 and created_ms >= :cutoff
            order by action, parent, child
            """,
            {"token_id": token_id, "cutoff": cutoff_ms},
        )
    ).rows
    return [_usage_suggestion(row["action"], row["parent"], row["child"]) for row in rows]


async def recent_token_checks(datasette, token_id, limit=50):
    """Most recent permission checks for a token (including denied ones), for
    display on the token details page."""
    from .views import Config

    db = Config(datasette).db
    rows = (
        await db.execute(
            """
            select action, parent, child, result, when_iso, created_ms
            from auth_tokens_usage
            where token_id = :token_id
            order by id desc limit :limit
            """,
            {"token_id": token_id, "limit": limit},
        )
    ).rows
    return [dict(row) for row in rows]


def format_permissions(datasette, permissions_dict):
    if not permissions_dict:
        return "All permissions"
    abbreviations = abbr_to_name(datasette)

    output = []

    # Format permissions for all databases
    if "a" in permissions_dict:
        output.append("All databases:")
        for code in permissions_dict["a"]:
            output.append(f"- {abbreviations.get(code, code)}")

    # Format permissions for specific databases
    if "d" in permissions_dict:
        for db, codes in permissions_dict["d"].items():
            output.append(f"Database: {db}")
            for code in codes:
                output.append(f"- {abbreviations.get(code, code)}")

    # Format permissions for specific tables in specific databases
    if "r" in permissions_dict:
        for db, tables in permissions_dict["r"].items():
            for table, codes in tables.items():
                output.append(f"Table: {db}/{table}")
                for code in codes:
                    output.append(f"- {abbreviations.get(code, code)}")

    return "\n".join(output)
