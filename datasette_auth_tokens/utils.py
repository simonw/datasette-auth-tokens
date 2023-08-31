from typing import Optional
import time


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
    for permission in datasette.permissions.values():
        if permission.abbr:
            abbreviations[permission.abbr] = permission.name

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
