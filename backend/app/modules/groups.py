"""User groups — billing templates, not products.

A group answers one question: *how is this account billed and limited by
default?*  It is not something a user buys (that concept is gone); it is the
operator's preset.  Every member belongs to exactly one group, and per-member
overrides still win field by field, so moving someone between groups never
wipes hand-tuned limits.

billing_mode decides which meters are armed:
  time    : account expires; traffic is recorded but never enforced
  traffic : monthly quota enforced; account never expires
  both    : expiry AND monthly quota
  none    : record usage only, enforce nothing

Traffic always resets on calendar-month boundaries (owner decision
2026-08-30); the column stays so a future mode can change it per group.
"""
from __future__ import annotations

import re
import time
from typing import Any

from app.core.db import Database
from app.core.errors import ConfigError

GIB = 1024 ** 3
BILLING_MODES = ("time", "traffic", "both", "none")

# Media requests a member may open per calendar month unless their group says
# otherwise. Small on purpose: the cost of a request is somebody's evening.
DEFAULT_REQUEST_QUOTA = 3

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def needs_traffic(mode: str) -> bool:
    return mode in ("traffic", "both")


def needs_duration(mode: str) -> bool:
    return mode in ("time", "both")


DEFAULT_GROUPS: list[dict[str, Any]] = [
    {
        "id": "standard", "name": "普通用户",
        "description": "有到期时间，按月流量计费",
        "billing_mode": "both",
        "duration_days": 30, "traffic_quota_bytes": 1024 * GIB,
        "bandwidth_limit_kbps": 0, "max_streams": 2, "max_devices": 3,
        "allow_download": 0, "allow_transcode": 1,
        "is_default": 1,
    },
    {
        "id": "vip", "name": "白名单 VIP",
        "description": "永不过期，按月流量计费",
        "billing_mode": "traffic",
        "duration_days": 0, "traffic_quota_bytes": 2048 * GIB,
        "bandwidth_limit_kbps": 0, "max_streams": 4, "max_devices": 5,
        "allow_download": 1, "allow_transcode": 1,
        "is_default": 0,
    },
]

# The group the /prouser command moves someone into. Kept out of
# DEFAULT_GROUPS because those seed only into an empty table: an operator who
# curated their own groups would otherwise never get a target for /prouser,
# and the command would fail on a database that is working exactly as intended.
WHITELIST_GROUP_ID = "whitelist"

WHITELIST_GROUP: dict[str, Any] = {
    "id": WHITELIST_GROUP_ID, "name": "白名单",
    "description": "管理员手动放行的账号：永不过期，限制放宽",
    # duration_days 0 with billing_mode none is what "never expires" means
    # here; a time-billed group with 0 days would be rejected by _validate.
    "billing_mode": "none",
    "duration_days": 0, "traffic_quota_bytes": 0,
    "bandwidth_limit_kbps": 0, "max_streams": 10, "max_devices": 10,
    "allow_download": 1, "allow_transcode": 1,
    "is_default": 0, "request_quota": 0,
}

_INT_FIELDS = (
    # key, label, lo, hi
    ("duration_days", "默认时长(天)", 0, 36500),
    ("traffic_quota_bytes", "月流量额度", 0, 1 << 62),
    ("bandwidth_limit_kbps", "带宽限速 kbps", 0, 10_000_000),
    ("max_streams", "并发路数", 0, 100),
    ("max_devices", "设备数上限", 0, 100),
    ("request_quota", "每月求片次数", 0, 10_000),
)


class GroupService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- seeding -------------------------------------------------------------
    def seed_defaults(self) -> int:
        """Create the two starter groups exactly once.

        Same contract as the old plan seeding: if the operator deletes one it
        must not resurrect on restart, so only an empty table is seeded.
        """
        if self._db.one("SELECT COUNT(*) AS n FROM groups")["n"]:
            return self.ensure_whitelist()
        now = int(time.time())
        for spec in DEFAULT_GROUPS:
            self.create(dict(spec), now=now)
        return len(DEFAULT_GROUPS) + self.ensure_whitelist()

    def ensure_whitelist(self) -> int:
        """Guarantee the group /prouser promotes into. Idempotent.

        Unlike the starter groups this one is re-created if missing, because a
        command that names a specific group is not allowed to depend on the
        operator never having tidied their group list. Deliberately does not
        overwrite an existing row: if they renamed it or loosened its limits,
        that is their decision.
        """
        if self.get(WHITELIST_GROUP_ID):
            return 0
        self.create(dict(WHITELIST_GROUP))
        return 1

    # -- validation ----------------------------------------------------------
    @staticmethod
    def _validate(payload: dict[str, Any],
                  existing: dict[str, Any] | None = None) -> dict[str, Any]:
        src = dict(existing or {})
        src.update({k: v for k, v in payload.items() if v is not None})

        gid = str(src.get("id") or "").strip()
        if not existing and not _ID_RE.match(gid):
            raise ConfigError("组 ID 需为小写字母开头的 2-32 位标识")
        name = str(src.get("name") or "").strip()
        if not name:
            raise ConfigError("组名称不能为空")

        mode = str(src.get("billing_mode") or "both")
        if mode not in BILLING_MODES:
            raise ConfigError(f"计费模式必须是 {'/'.join(BILLING_MODES)}")

        # Absent means "the default allowance", not "unlimited": a form posted
        # by a UI that predates this field must not silently uncap requests.
        if src.get("request_quota") is None:
            src["request_quota"] = DEFAULT_REQUEST_QUOTA

        out: dict[str, Any] = {
            "id": gid, "name": name,
            "description": str(src.get("description") or ""),
            "billing_mode": mode,
            "allow_download": 1 if src.get("allow_download") else 0,
            "allow_transcode": 1 if src.get("allow_transcode") else 0,
            "is_default": 1 if src.get("is_default") else 0,
        }
        for key, label, lo, hi in _INT_FIELDS:
            try:
                val = int(src.get(key) if src.get(key) is not None else 0)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{label} 必须是整数") from exc
            if not lo <= val <= hi:
                raise ConfigError(f"{label} 超出范围")
            out[key] = val

        if needs_duration(mode) and out["duration_days"] <= 0:
            raise ConfigError("计时组必须设置默认时长")
        if needs_traffic(mode) and out["traffic_quota_bytes"] <= 0:
            raise ConfigError("计流量组必须设置月流量额度")
        return out

    # -- read ----------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM groups ORDER BY is_default DESC, name COLLATE NOCASE")
        for row in rows:
            row["is_builtin"] = row["id"] == WHITELIST_GROUP_ID
            row["member_count"] = self._db.one(
                "SELECT COUNT(*) AS n FROM members WHERE group_id=?",
                (row["id"],))["n"]
        return rows

    def get(self, group_id: str) -> dict[str, Any] | None:
        if not group_id:
            return None
        return self._db.one("SELECT * FROM groups WHERE id=?", (group_id,))

    def default_group_id(self) -> str | None:
        row = self._db.one(
            "SELECT id FROM groups WHERE is_default=1 ORDER BY id LIMIT 1")
        return row["id"] if row else None

    # -- write ---------------------------------------------------------------
    def create(self, payload: dict[str, Any],
               now: int | None = None) -> dict[str, Any]:
        group = self._validate(payload)
        if self.get(group["id"]):
            raise ConfigError("组 ID 已存在")
        now = now or int(time.time())
        if group["is_default"]:
            self._db.execute("UPDATE groups SET is_default=0")
        self._db.execute(
            "INSERT INTO groups (id,name,description,billing_mode,duration_days,"
            "traffic_quota_bytes,bandwidth_limit_kbps,max_streams,max_devices,"
            "allow_download,allow_transcode,is_default,request_quota,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (group["id"], group["name"], group["description"],
             group["billing_mode"], group["duration_days"],
             group["traffic_quota_bytes"], group["bandwidth_limit_kbps"],
             group["max_streams"], group["max_devices"],
             group["allow_download"], group["allow_transcode"],
             group["is_default"], group["request_quota"], now, now))
        return self.get(group["id"])  # type: ignore[return-value]

    def update(self, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.get(group_id)
        if not existing:
            raise ConfigError("组不存在")
        payload = dict(payload)
        payload["id"] = group_id
        group = self._validate(payload, existing)
        if group["is_default"]:
            self._db.execute("UPDATE groups SET is_default=0 WHERE id != ?",
                             (group_id,))
        self._db.execute(
            "UPDATE groups SET name=?,description=?,billing_mode=?,"
            "duration_days=?,traffic_quota_bytes=?,bandwidth_limit_kbps=?,"
            "max_streams=?,max_devices=?,allow_download=?,allow_transcode=?,"
            "is_default=?,request_quota=?,updated_at=? WHERE id=?",
            (group["name"], group["description"], group["billing_mode"],
             group["duration_days"], group["traffic_quota_bytes"],
             group["bandwidth_limit_kbps"], group["max_streams"],
             group["max_devices"], group["allow_download"],
             group["allow_transcode"], group["is_default"],
             group["request_quota"], int(time.time()), group_id))
        return self.get(group_id)  # type: ignore[return-value]

    def delete(self, group_id: str) -> bool:
        if group_id == WHITELIST_GROUP_ID:
            raise ConfigError("白名单是固定系统分组，不能删除；可调整该组限制或迁移成员")
        used = self._db.one(
            "SELECT COUNT(*) AS n FROM members WHERE group_id=?", (group_id,))["n"]
        if used:
            raise ConfigError(f"仍有 {used} 个用户在该组，先迁移再删除")
        self._db.execute("DELETE FROM groups WHERE id=?", (group_id,))
        return True
