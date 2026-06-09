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


def format_permissions(datasette, permissions_dict):
    if not permissions_dict:
        return "All permissions"
    abbreviations = {}
    for action in datasette.actions.values():
        if action.abbr:
            abbreviations[action.abbr] = action.name

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
