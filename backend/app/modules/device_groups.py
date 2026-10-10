"""Observational device-name groups, never hardware identity or enforcement keys.

Within one account, exact names (after trimming surrounding whitespace) merge across
clients. Names remain case-sensitive; no IP/model/fuzzy matching is attempted.
Empty names and generic unknown labels fall back to each original DeviceId.
Only groups with at least one blocked=0 row count. Stored rows are never changed.
"""
from __future__ import annotations

from typing import Any

from app.core.db import Database

# Python's Unicode whitespace set, also supplied verbatim to SQLite TRIM.
_NAME_WHITESPACE = " \t\r\n\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680" + (
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
_UNKNOWN_NAMES = ("", "unknown", "unknown device", "未知", "未知设备")
# SQLite and Python deliberately use the same trimming and sentinel rules.
_NAME_SQL = "TRIM(COALESCE(device_name,''), char(" + ",".join(
    str(ord(c)) for c in _NAME_WHITESPACE) + "))"
_UNKNOWN_SQL = f"LOWER({_NAME_SQL}) IN ({','.join(repr(n) for n in _UNKNOWN_NAMES)})"


def count_device_groups(db: Database, user_id: str | None = None) -> int:
    """Count active groups in SQL without loading raw device history into memory."""
    scope = " AND emby_user_id=?" if user_id is not None else ""
    row = db.one(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM devices WHERE blocked=0"
        + scope
        + f" GROUP BY emby_user_id, CASE WHEN {_UNKNOWN_SQL} THEN 1 ELSE 0 END,"
        + f" CASE WHEN {_UNKNOWN_SQL} THEN device_id ELSE {_NAME_SQL} END)",
        (user_id,) if user_id is not None else (),
    )
    return int(row["n"])


def group_device_records(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add an expandable view of all original rows, including blocked IDs.

    Preserve input order (most recently seen first in MemberService.devices) and
    never mutate the original records. Account and key kind avoid collisions.
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for device in devices:
        name = str(device.get("device_name") or "").strip(_NAME_WHITESPACE)
        by_id = name.lower() in _UNKNOWN_NAMES
        kind = "device_id" if by_id else "name"
        value = str(device["device_id"]) if by_id else name
        key = (str(device["emby_user_id"]), kind, value)
        if key not in groups:
            groups[key] = {
                "device_name": name,
                "grouping": kind,
                "record_count": 0,
                "unblocked_count": 0,
                "devices": [],
            }
        group = groups[key]
        group["devices"].append(device)
        group["record_count"] += 1
        group["unblocked_count"] += int(device.get("blocked") == 0)
    return list(groups.values())
