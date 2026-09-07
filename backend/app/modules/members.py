"""Members — the link between an Emby account and a user group.

The single most important rule in this file: **an Emby user with no member row
is invisible to the panel.**  This server has hundreds of accounts created long
before the panel existed.  If enforcement iterated over Emby's user list rather
than over member rows, one bad deploy could disable all of them at once.  So
membership is opt-in (or explicit bulk enroll), and every enforcement query
starts from `members`.

The second rule: state is derived, never guessed.  `status` is stored, but
`effective_state()` recomputes it from expiry and quota every time it is asked,
so a member whose group changed or whose month rolled over is correct
immediately rather than at the next sampler tick.

v0.14: plans (products someone buys) are gone.  A member belongs to exactly one
*group* — the operator's billing preset (see groups.py) — plus zero or more
*roles* (admin / uploader), which are job functions, not resource limits.
"""
from __future__ import annotations

import contextlib
import json
import secrets
import string
import time
from datetime import UTC, datetime
from typing import Any

from app.core.db import Database
from app.core.errors import ConfigError
from app.modules.groups import GroupService, needs_duration, needs_traffic

# active    : normal
# suspended : operator disabled by hand; never auto-cleared
# expired   : past expires_at
# exhausted : traffic quota consumed
# pending   : created but not yet activated
MEMBER_STATES = ("active", "suspended", "expired", "exhausted", "pending")

# Operator-set states that automatic enforcement must never overwrite. Losing
# this distinction would mean a suspended user silently comes back the moment
# their quota resets.
MANUAL_STATES = ("suspended", "pending")

# Additive job functions. admin => may log into the panel with their Emby
# credentials; uploader => flagged for the future request-intake pipeline.
ROLES = ("admin", "uploader")

# Keys an operator may put in members.overrides_json. Anything else is rejected
# so a typo cannot silently invent a new limit that enforcement never reads.
OVERRIDE_KEYS = (
    "max_streams",
    "bandwidth_limit_kbps",
    "max_devices",
    "allow_transcode",
    "allow_download",
    "libraries_mode",
    "libraries",
    "expires_at_override",
    "extra_traffic_bytes",
)
LIBRARY_MODES = ("inherit", "replace", "extend")
AUDIT_DETAIL_MAX = 4000

# Panel and nginx speak different units. The stored column stays kbps so Emby
# RemoteClientBitrateLimit (bits/s = kbps * 1000) and historical rows keep
# working. The operator reads and types MB/s, matching the live speed column.
# nginx limit_rate wants bytes/second: kbps * 125.
BYTES_PER_KBPS = 125


def rate_bytes_per_sec(kbps: int) -> int:
    """Convert stored kbps to nginx limit_rate bytes/second.

    0 stays 0 (uncapped). The panel displays MB/s; this is the unit the
    node actually enforces.
    """
    kbps = int(kbps or 0)
    if kbps <= 0:
        return 0
    return kbps * BYTES_PER_KBPS


def random_password(length: int = 12) -> str:
    """For operator-reset accounts, so nobody reuses '123456'."""
    pool = string.ascii_letters + string.digits
    return "".join(secrets.choice(pool) for _ in range(length))


def period_start(now: int) -> int:
    """Start of the current calendar month (UTC), in epoch seconds.

    Traffic always resets on month boundaries (owner decision 2026-08-30) so
    every member's window is the same and support is never guesswork.
    """
    dt = datetime.fromtimestamp(now, UTC)
    return int(dt.replace(day=1, hour=0, minute=0, second=0,
                          microsecond=0).timestamp())


def parse_overrides(raw: Any) -> dict[str, Any]:
    """Decode stored JSON without raising on a corrupt row."""
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_roles(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").split(",")
    out = []
    for item in items:
        role = str(item).strip().lower()
        if role in ROLES and role not in out:
            out.append(role)
    return out


def _as_int(raw: Any, label: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{label}必须是整数") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{label}必须在 {lo}–{hi} 之间")
    return value


def validate_overrides(payload: Any) -> dict[str, Any]:
    """Whitelist + type/range check. Empty object means inherit everything."""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ConfigError("覆盖层必须是对象")
    unknown = [k for k in payload if k not in OVERRIDE_KEYS]
    if unknown:
        raise ConfigError(f"不支持的覆盖字段: {', '.join(sorted(unknown))}")

    out: dict[str, Any] = {}
    if "max_streams" in payload:
        out["max_streams"] = _as_int(payload["max_streams"], "并发路数", 0, 100)
    if "bandwidth_limit_kbps" in payload:
        out["bandwidth_limit_kbps"] = _as_int(
            payload["bandwidth_limit_kbps"], "带宽限速", 0, 10_000_000)
    if "max_devices" in payload:
        out["max_devices"] = _as_int(payload["max_devices"], "设备数上限", 0, 100)
    for flag in ("allow_transcode", "allow_download"):
        if flag in payload:
            out[flag] = 1 if payload[flag] else 0
    if "libraries_mode" in payload:
        mode = str(payload["libraries_mode"] or "inherit")
        if mode not in LIBRARY_MODES:
            raise ConfigError(f"媒体库模式必须是 {'/'.join(LIBRARY_MODES)} 之一")
        out["libraries_mode"] = mode
    if "libraries" in payload:
        libraries = payload["libraries"]
        if not isinstance(libraries, list):
            raise ConfigError("媒体库列表格式错误")
        out["libraries"] = [str(x) for x in libraries if str(x).strip()]
    if "expires_at_override" in payload:
        raw = payload["expires_at_override"]
        if raw is None or raw == "":
            out["expires_at_override"] = None
        else:
            out["expires_at_override"] = _as_int(
                raw, "到期时间覆盖", 0, 4_102_444_800)
    if "extra_traffic_bytes" in payload:
        out["extra_traffic_bytes"] = _as_int(
            payload["extra_traffic_bytes"], "额外流量", 0, 1 << 60)
    # libraries without an explicit mode is treated as replace: the operator
    # listed libraries, so they meant those libraries, not "also inherit".
    if "libraries" in out and "libraries_mode" not in out:
        out["libraries_mode"] = "replace"
    return out


def merge_effective(group: dict[str, Any] | None, overrides: dict[str, Any] | None,
                    member: dict[str, Any] | None = None) -> dict[str, Any]:
    """Group defaults with per-member overrides applied field-by-field.

    A key that is absent from overrides is inherited. Presence — even of a
    value equal to the group default — is an override, so the UI can show it
    as such.
    """
    group = group or {}
    ov = overrides or {}
    streams = ov["max_streams"] if "max_streams" in ov else int(
        group.get("max_streams") or 0)
    bandwidth = ov["bandwidth_limit_kbps"] if "bandwidth_limit_kbps" in ov else int(
        group.get("bandwidth_limit_kbps") or 0)
    devices = ov["max_devices"] if "max_devices" in ov else int(
        group.get("max_devices") or 0)
    transcode = ov["allow_transcode"] if "allow_transcode" in ov else group.get(
        "allow_transcode", 1)
    download = ov["allow_download"] if "allow_download" in ov else group.get(
        "allow_download", 0)

    # Groups carry no library restriction; per-member overrides may.
    ov_libs = list(ov.get("libraries") or [])
    mode = ov.get("libraries_mode") or "inherit"
    libraries = ov_libs if mode in ("replace", "extend") else []

    stored_expires = (member or {}).get("expires_at")
    if "expires_at_override" in ov:
        expires_at = ov.get("expires_at_override")
    else:
        expires_at = stored_expires

    extra = int(ov.get("extra_traffic_bytes") or 0)
    base_quota = int(group.get("traffic_quota_bytes") or 0)
    if group and needs_traffic(group.get("billing_mode") or "") and (
            base_quota or extra):
        quota = base_quota + extra
    else:
        quota = 0

    overridden = [k for k in OVERRIDE_KEYS if k in ov]
    return {
        "max_streams": int(streams),
        "bandwidth_limit_kbps": int(bandwidth),
        "max_devices": int(devices),
        "allow_transcode": bool(transcode),
        "allow_download": bool(download),
        "libraries": libraries,
        "expires_at": expires_at,
        "traffic_quota_bytes": quota,
        "overridden_keys": overridden,
    }


def audit_diff(before: dict[str, Any] | None, after: dict[str, Any] | None
               ) -> dict[str, dict[str, Any]]:
    """`{field: {from, to}}` for fields that actually changed."""
    before = before or {}
    after = after or {}
    keys = sorted(set(before) | set(after))
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        left, right = before.get(key, None), after.get(key, None)
        if left != right:
            out[key] = {"from": left, "to": right}
    return out


def encode_audit_detail(diff: dict[str, Any] | None, extra: str = "") -> str:
    payload: dict[str, Any] = {}
    if diff:
        payload.update(diff)
    if extra:
        payload["note"] = extra[:200]
    if not payload:
        return ""
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    return raw[:AUDIT_DETAIL_MAX]


class MemberService:
    def __init__(self, db: Database, groups: GroupService) -> None:
        self._db = db
        self._groups = groups
        self._metering = None
        self._metering_cutover = None

    def bind_metering(self, metering: Any,
                      cutover: Any = None) -> None:
        """Optional measured ledger. cutover() -> bool; off by default."""
        self._metering = metering
        self._metering_cutover = cutover

    # -- read ----------------------------------------------------------------
    def get(self, user_id: str) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM members WHERE emby_user_id=?", (user_id,))
        if not row:
            return None
        decorated = self._decorate(row)
        # Same shape as a list row: a caller that reads invitee_count off the
        # table and then off the detail view must not get a KeyError from one
        # of them.
        self._attach_tree([decorated])
        return decorated

    def list(self, status: str | None = None, group_id: str | None = None,
             role: str | None = None, search: str | None = None,
             limit: int = 500, register_via: str | None = None,
             inviter_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM members"
        clauses, params = [], []
        if group_id:
            clauses.append("group_id=?")
            params.append(group_id)
        if register_via:
            clauses.append("register_via=?")
            params.append(str(register_via))
        if inviter_id:
            clauses.append("inviter_id=?")
            params.append(str(inviter_id))
        if search:
            clauses.append("(username LIKE ? OR note LIKE ? OR contact LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY username COLLATE NOCASE ASC LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        rows = [self._decorate(r) for r in self._db.query(sql, tuple(params))]
        self._attach_tree(rows)
        # Status/role are filtered after decoration: the *effective* state is
        # time-dependent and roles live in a comma-separated column.
        if status:
            rows = [r for r in rows if r["state"] == status]
        if role:
            rows = [r for r in rows if role in r["roles"]]
        return rows

    def _attach_tree(self, rows: list[dict[str, Any]]) -> None:
        """Add invitee counts and inviter names in two queries, not 2N.

        The list renders hundreds of rows; asking per row is how a member page
        starts taking seconds.
        """
        if not rows:
            return
        counts = {
            str(r["inviter_id"]): int(r["n"] or 0) for r in self._db.query(
                "SELECT inviter_id, COUNT(*) AS n FROM members"
                " WHERE inviter_id <> '' GROUP BY inviter_id")
        }
        wanted = {str(r.get("inviter_id") or "") for r in rows if r.get("inviter_id")}
        names: dict[str, str] = {}
        if wanted:
            placeholders = ",".join("?" * len(wanted))
            names = {
                str(r["emby_user_id"]): str(r["username"] or "")
                for r in self._db.query(
                    "SELECT emby_user_id, username FROM members"
                    f" WHERE emby_user_id IN ({placeholders})", tuple(wanted))
            }
        for row in rows:
            uid = str(row.get("emby_user_id") or "")
            inviter_id = str(row.get("inviter_id") or "")
            row["invitee_count"] = counts.get(uid, 0)
            # An inviter whose account is gone still gets a label: '—' would
            # read as "nobody invited them", which is a different fact.
            row["inviter_name"] = (
                names.get(inviter_id, "(已删除)") if inviter_id else "")

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        group = self._groups.get(out.get("group_id") or "")
        out["group"] = group
        out["group_name"] = group["name"] if group else "(未分组)"
        out["billing_mode"] = group["billing_mode"] if group else "none"
        out["roles"] = parse_roles(out.pop("roles", ""))
        overrides = parse_overrides(out.pop("overrides_json", None))
        out["overrides"] = overrides
        effective = merge_effective(group, overrides, out)
        out["effective"] = effective
        out["overridden_keys"] = list(effective["overridden_keys"])
        # Flatten the fields the rest of the panel reads.
        out["max_streams"] = effective["max_streams"]
        out["bandwidth_limit_kbps"] = effective["bandwidth_limit_kbps"]
        out["max_devices"] = effective["max_devices"]
        out["allow_transcode"] = effective["allow_transcode"]
        out["allow_download"] = effective["allow_download"]
        out["libraries"] = list(effective["libraries"])
        out["expires_at_effective"] = effective["expires_at"]

        snap = None
        if self._metering is not None:
            with contextlib.suppress(Exception):
                snap = self._metering.snapshot(out["emby_user_id"])
        out["metering"] = snap
        cutover = bool(self._metering_cutover() if callable(self._metering_cutover)
                       else self._metering_cutover)
        if cutover and snap is not None:
            out["measured_used_bytes"] = snap.get("measured_used_bytes")

        state, reason = self.effective_state(out, group)
        out["state"] = state
        out["state_reason"] = reason

        quota = int(effective.get("traffic_quota_bytes") or 0)
        used = int(out.get("traffic_used_bytes") or 0)
        if cutover and snap is not None and snap.get("measured_used_bytes") is not None:
            used = int(snap["measured_used_bytes"])
        out["traffic_quota_bytes"] = quota
        out["traffic_remaining_bytes"] = max(0, quota - used) if quota else None
        out["traffic_percent"] = round(used / quota * 100, 1) if quota else None

        expires = effective.get("expires_at")
        out["days_remaining"] = (
            max(0, int((expires - time.time()) // 86400)) if expires else None)
        out["device_count"] = self._db.one(
            "SELECT COUNT(*) AS n FROM devices WHERE emby_user_id=? AND blocked=0",
            (out["emby_user_id"],))["n"]
        out["register_via"] = str(out.get("register_via") or "legacy")
        out["inviter_id"] = str(out.get("inviter_id") or "")
        out["invite_quota"] = int(out.get("invite_quota") or 0)
        out["entitlement_state"] = state
        out["last_remote_action"] = str(out.get("last_remote_action") or "")
        last_ok = out.get("last_remote_ok")
        if last_ok is None:
            out["last_remote_ok"] = None
        else:
            out["last_remote_ok"] = bool(last_ok)
        out["last_remote_error"] = str(out.get("last_remote_error") or "")
        out["last_remote_at"] = out.get("last_remote_at")
        out["retryable"] = (out["last_remote_ok"] is False
                            and bool(out["last_remote_action"]))
        return out

    def detail(self, user_id: str, *, audit_limit: int = 50) -> dict[str, Any] | None:
        """Drawer payload: member + devices + that member's audit trail."""
        member = self.get(user_id)
        if not member:
            return None
        return {
            "member": member,
            "devices": self.devices(user_id),
            "audit": self.audit_log(audit_limit, subject=user_id),
        }

    @staticmethod
    def effective_state(member: dict[str, Any], group: dict[str, Any] | None,
                        now: int | None = None) -> tuple[str, str]:
        """Recompute state from the facts, not from the stored label."""
        now = now or int(time.time())
        stored = str(member.get("status") or "active")
        if stored in MANUAL_STATES:
            return stored, "管理员手动设置"
        if not group:
            return "active", "未分组，不做计费"

        effective = member.get("effective")
        if not isinstance(effective, dict):
            effective = merge_effective(
                group, member.get("overrides") or parse_overrides(
                    member.get("overrides_json")), member)

        mode = str(group.get("billing_mode") or "none")
        expires = effective.get("expires_at", member.get("expires_at"))
        has_expiry_override = "expires_at_override" in (member.get("overrides") or {})
        if expires and now >= expires and (
                has_expiry_override or needs_duration(mode)):
            return "expired", "已过期"

        quota = int(effective.get("traffic_quota_bytes") or 0)
        if "measured_used_bytes" in member:
            used_raw = member.get("measured_used_bytes")
            if used_raw is None:
                return "active", "正常"
            used = int(used_raw or 0)
        else:
            used = int(member.get("traffic_used_bytes") or 0)
        if needs_traffic(mode) and quota and used >= quota:
            return "exhausted", "本月流量已用尽"
        return "active", "正常"

    # -- write ---------------------------------------------------------------
    def upsert(self, user_id: str, username: str, payload: dict[str, Any],
               actor: str = "system") -> dict[str, Any]:
        if not user_id:
            raise ConfigError("缺少 Emby 用户 ID")
        now = int(time.time())
        existing = self._db.one(
            "SELECT * FROM members WHERE emby_user_id=?", (user_id,))

        group_id = payload.get(
            "group_id", existing["group_id"] if existing else None)
        if group_id is not None:
            group_id = str(group_id) or None
        group = None
        if group_id:
            group = self._groups.get(group_id)
            if not group:
                raise ConfigError(f"用户组不存在: {group_id}")

        status = str(payload.get(
            "status", existing["status"] if existing else "active"))
        if status not in MEMBER_STATES:
            raise ConfigError(f"状态必须是 {'/'.join(MEMBER_STATES)} 之一")

        roles = payload.get("roles", "__keep__")
        if roles == "__keep__":
            roles_csv = existing["roles"] if existing else ""
        else:
            roles_csv = ",".join(parse_roles(roles))

        # Expiry: an explicit value always wins. Switching groups defaults to
        # *keeping* the current term and per-member overrides so a 180-day
        # account is never silently cut to the new group's 30 days. New members
        # still arm the group's duration. Permanent -> timed requires an
        # explicit expiry_policy (keep/apply_group/clear/set) rather than a
        # guess.
        expires_at = payload.get("expires_at", "__keep__")
        expiry_policy = str(payload.get("expiry_policy") or "keep")
        if expiry_policy not in ("keep", "apply_group", "clear", "set"):
            raise ConfigError("expiry_policy 必须是 keep/apply_group/clear/set")
        if expires_at == "__keep__":
            expires_at = existing["expires_at"] if existing else None
            if not existing:
                if group and needs_duration(group["billing_mode"]):
                    expires_at = now + int(group["duration_days"]) * 86400
            elif expiry_policy == "apply_group":
                if group and needs_duration(group["billing_mode"]):
                    expires_at = now + int(group["duration_days"]) * 86400
                else:
                    expires_at = None
            elif expiry_policy == "clear":
                expires_at = None
            elif expiry_policy == "set":
                raise ConfigError("expiry_policy=set 时必须提供 expires_at")
            # keep: leave the stored term (and overrides) even if the group changed
        elif expires_at is not None:
            expires_at = int(expires_at)

        p_start = existing["traffic_period_start"] if existing else 0
        if not p_start:
            p_start = period_start(now)
        used = int(payload.get(
            "traffic_used_bytes",
            existing["traffic_used_bytes"] if existing else 0))

        # Provenance is written once, at creation. An update that omitted these
        # would quietly relabel where a member came from, and the invite tree
        # is only worth having if it cannot be rewritten by a later edit.
        register_via = str(payload.get(
            "register_via",
            existing["register_via"] if existing else "legacy") or "legacy")
        inviter_id = str(payload.get(
            "inviter_id", existing["inviter_id"] if existing else "") or "")
        if inviter_id == user_id:
            inviter_id = ""  # nobody invites themselves
        register_at = payload.get(
            "register_at", existing["register_at"] if existing else None)
        register_at = int(register_at) if register_at else None

        row = (
            username or (existing["username"] if existing else ""),
            group_id,
            roles_csv,
            status,
            expires_at,
            max(0, used),
            p_start,
            str(payload.get("note", existing["note"] if existing else ""))[:500],
            str(payload.get("contact", existing["contact"] if existing else ""))[:200],
            now,
        )
        if existing:
            self._db.execute(
                "UPDATE members SET username=?,group_id=?,roles=?,status=?,"
                "expires_at=?,traffic_used_bytes=?,traffic_period_start=?,"
                "note=?,contact=?,updated_at=? WHERE emby_user_id=?",
                row + (user_id,))
            self.audit(actor, "member.update", user_id, encode_audit_detail(audit_diff(
                {"group_id": existing.get("group_id"),
                 "roles": existing.get("roles"),
                 "status": existing.get("status"),
                 "expires_at": existing.get("expires_at")},
                {"group_id": group_id, "roles": roles_csv,
                 "status": status, "expires_at": expires_at},
            )))
        else:
            self._db.execute(
                "INSERT INTO members (username,group_id,roles,status,expires_at,"
                "traffic_used_bytes,traffic_period_start,note,contact,updated_at,"
                "emby_user_id,created_at,overrides_json,register_via,inviter_id,"
                "register_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row + (user_id, now, "{}", register_via, inviter_id, register_at))
            self.audit(actor, "member.create", user_id, encode_audit_detail({
                "group_id": {"from": None, "to": group_id},
                "status": {"from": None, "to": status},
                "expires_at": {"from": None, "to": expires_at},
                "register_via": {"from": None, "to": register_via},
                "inviter_id": {"from": None, "to": inviter_id},
            }))
        return self.get(user_id)  # type: ignore[return-value]

    def enroll_defaults(self, emby_users: list[dict[str, Any]],
                        actor: str = "operator") -> int:
        """Give every unmanaged Emby account a member row in the default group.

        Explicitly operator-triggered (never automatic on list) so that one
        accidental page load cannot mass-create hundreds of billed accounts.
        """
        default_group = self._groups.default_group_id()
        if not default_group:
            raise ConfigError("没有默认用户组，先在用户组页设置一个")
        enrolled = 0
        for user in emby_users:
            user_id = str(user.get("Id") or "")
            if not user_id:
                continue
            if self._db.one("SELECT 1 AS x FROM members WHERE emby_user_id=?",
                            (user_id,)):
                continue
            self.upsert(user_id, str(user.get("Name") or ""),
                        {"group_id": default_group}, actor=actor)
            enrolled += 1
        return enrolled

    def sync_emby(self, emby_users: list[dict[str, Any]], apply: bool = False,
                  enroll_new: bool = False, actor: str = "system") -> dict[str, Any]:
        """Reconcile member rows against the accounts Emby actually has.

        Membership rows outlive the Emby account: they carry the traffic
        ledger, plan, expiry and operator notes. So a member whose Emby
        account is gone is *flagged*, never deleted -- an accidental mass
        delete in Emby (or a partial API response) must not silently destroy
        billing history that the operator may need to restore from.

        An empty user list is treated as "could not read Emby", not "Emby has
        no users": marking every member an orphan on one failed poll is worse
        than skipping a cycle.
        """
        if not isinstance(emby_users, list):
            raise ConfigError("Emby 用户列表无效")
        live: dict[str, str] = {}
        for user in emby_users:
            user_id = str(user.get("Id") or "")
            if user_id:
                live[user_id] = str(user.get("Name") or "")
        rows = self._db.query(
            "SELECT emby_user_id, username, emby_missing_since FROM members")
        known = {str(r["emby_user_id"]) for r in rows}

        missing = [r for r in rows if str(r["emby_user_id"]) not in live]
        returned = [r for r in rows
                    if str(r["emby_user_id"]) in live and r["emby_missing_since"]]
        unmanaged = [uid for uid in live if uid not in known]
        renamed = [r for r in rows
                   if str(r["emby_user_id"]) in live
                   and live[str(r["emby_user_id"])] != str(r["username"] or "")]

        result = {
            "checked": len(rows),
            "emby_users": len(live),
            "missing": [{"emby_user_id": str(r["emby_user_id"]),
                         "username": str(r["username"] or ""),
                         "since": r["emby_missing_since"]} for r in missing],
            "returned": [str(r["emby_user_id"]) for r in returned],
            "unmanaged": [{"emby_user_id": uid, "username": live[uid]}
                          for uid in unmanaged],
            "renamed": len(renamed),
            "enrolled": 0,
            "applied": False,
        }
        if not live:
            # Fail closed: no readable users means no verdict about anybody.
            result["skipped"] = "emby-user-list-empty"
            return result
        if not apply:
            return result

        now = int(time.time())
        with self._db.write() as conn:
            for row in missing:
                if not row["emby_missing_since"]:
                    conn.execute(
                        "UPDATE members SET emby_missing_since=?,updated_at=?"
                        " WHERE emby_user_id=?",
                        (now, now, row["emby_user_id"]))
            for row in returned:
                # The account exists again: clear the flag rather than leaving
                # a stale warning on a member who is demonstrably back.
                conn.execute(
                    "UPDATE members SET emby_missing_since=NULL,updated_at=?"
                    " WHERE emby_user_id=?", (now, row["emby_user_id"]))
            for row in renamed:
                conn.execute(
                    "UPDATE members SET username=?,updated_at=? WHERE emby_user_id=?",
                    (live[str(row["emby_user_id"])], now, row["emby_user_id"]))
        result["applied"] = True

        if enroll_new and unmanaged:
            result["enrolled"] = self.enroll_defaults(
                [{"Id": u["emby_user_id"], "Name": u["username"]}
                 for u in result["unmanaged"]], actor=actor)
        return result

    def purge_orphans(self, user_ids: list[str], actor: str = "operator") -> int:
        """Delete member rows whose Emby account is confirmed gone.

        Only rows already flagged by sync_emby can be purged: an operator
        clicking through a stale page must not be able to delete a member whose
        account is present right now.
        """
        removed = 0
        for user_id in user_ids:
            row = self._db.one(
                "SELECT emby_missing_since FROM members WHERE emby_user_id=?",
                (str(user_id),))
            if not row or not row["emby_missing_since"]:
                continue
            self.delete(str(user_id), actor=actor, cascade=False)
            removed += 1
        return removed

    def set_roles(self, user_id: str, roles: Any,
                  actor: str = "operator") -> dict[str, Any]:
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        cleaned = parse_roles(roles)
        before = ",".join(member.get("roles") or [])
        after = ",".join(cleaned)
        if before != after:
            self._db.execute(
                "UPDATE members SET roles=?,updated_at=? WHERE emby_user_id=?",
                (after, int(time.time()), user_id))
            self.audit(actor, "member.roles", user_id, encode_audit_detail({
                "roles": {"from": before, "to": after},
            }))
        return self.get(user_id)  # type: ignore[return-value]

    # -- telegram linkage -----------------------------------------------------

    def find_by_telegram(self, tg_user_id: str) -> dict[str, Any] | None:
        """The member a Telegram chat currently answers for, if any."""
        tg_user_id = str(tg_user_id or "").strip()
        if not tg_user_id:
            return None
        row = self._db.one(
            "SELECT * FROM members WHERE tg_user_id=?", (tg_user_id,))
        return self._decorate(row) if row else None

    def find_by_username(self, username: str) -> dict[str, Any] | None:
        """Look a member up by their Emby login name.

        Case-insensitive: someone claiming an old account types the name from
        memory, and rejecting them over capitalisation would send a real owner
        to the operator for nothing.
        """
        username = str(username or "").strip()
        if not username:
            return None
        row = self._db.one(
            "SELECT * FROM members WHERE username=? COLLATE NOCASE", (username,))
        return self._decorate(row) if row else None

    def linked_telegram(self) -> list[dict[str, Any]]:
        """Every member with a linked chat, unpaginated.

        Deliberately not ``list()``: that caps at 500 rows, so a group audit
        built on it would quietly skip everyone past the cap and still report
        "all present". An audit that under-reports is worse than none, because
        it is believed.
        """
        rows = self._db.query(
            "SELECT * FROM members WHERE tg_user_id <> '' ORDER BY username")
        return [self._decorate(r) for r in rows]

    def bind_telegram(self, user_id: str, tg_user_id: str,
                      tg_username: str = "", actor: str = "operator") -> dict[str, Any]:
        """Link a chat to a member.

        A chat may only speak for one account. Rebinding is allowed, but it
        detaches the previous holder first and says so in the audit trail --
        silently moving a link would leave the old member believing they still
        get notifications.
        """
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        tg_user_id = str(tg_user_id or "").strip()
        if not tg_user_id:
            raise ValueError("tg_user_id 不能为空")

        previous = self.find_by_telegram(tg_user_id)
        now = int(time.time())
        if previous and previous["emby_user_id"] != user_id:
            self._db.execute(
                "UPDATE members SET tg_user_id='',tg_username='',tg_bound_at=NULL,"
                "updated_at=? WHERE emby_user_id=?",
                (now, previous["emby_user_id"]))
            self.audit(actor, "member.telegram.unbind", previous["emby_user_id"],
                       "chat rebound to another member")

        self._db.execute(
            "UPDATE members SET tg_user_id=?,tg_username=?,tg_bound_at=?,"
            "updated_at=? WHERE emby_user_id=?",
            (tg_user_id, str(tg_username or "").strip(), now, now, user_id))
        # The numeric chat id is an identifier, not a secret, but there is no
        # reason to spill it into the log either.
        self.audit(actor, "member.telegram.bind", user_id,
                   f"linked to @{tg_username}" if tg_username else "linked")
        return self.get(user_id)  # type: ignore[return-value]

    def unbind_telegram(self, user_id: str, actor: str = "operator") -> dict[str, Any]:
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        if member.get("tg_user_id"):
            self._db.execute(
                "UPDATE members SET tg_user_id='',tg_username='',tg_bound_at=NULL,"
                "updated_at=? WHERE emby_user_id=?",
                (int(time.time()), user_id))
            self.audit(actor, "member.telegram.unbind", user_id, "unlinked")
        return self.get(user_id)  # type: ignore[return-value]

    def expiring_within(self, days: int = 7) -> list[dict[str, Any]]:
        """Members whose access ends inside the window, soonest first.

        Already-expired members are excluded: they need a different message
        than "expiring soon", and reminding them daily forever is noise.
        """
        now = int(time.time())
        until = now + max(1, days) * 86400
        rows = self._db.query(
            "SELECT * FROM members WHERE expires_at IS NOT NULL "
            "AND expires_at > ? AND expires_at <= ? ORDER BY expires_at ASC",
            (now, until))
        return [self._decorate(r) for r in rows]

    def inviter_of(self, user_id: str) -> dict[str, Any] | None:
        """The member who vouched for this one, if they still exist."""
        row = self._db.one(
            "SELECT inviter_id FROM members WHERE emby_user_id=?", (str(user_id),))
        inviter_id = str((row or {}).get("inviter_id") or "")
        if not inviter_id or inviter_id == str(user_id):
            return None
        return self.get(inviter_id)

    def invitees_of(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM members WHERE inviter_id=? ORDER BY username",
            (str(user_id),))
        return [self._decorate(r) for r in rows]

    def delete_preview(self, user_id: str, cascade: bool = False) -> dict[str, Any]:
        """Exactly who a delete would remove, for the confirmation dialog.

        Default is the named account only. Cascade is a separate explicit
        choice: available_cascade always lists the inviter so the UI can ask,
        while cascade/objects reflect this request.
        """
        target = self.get(user_id)
        if not target:
            raise KeyError(user_id)
        inviter = self.inviter_of(user_id)
        available = [{
            "emby_user_id": inviter["emby_user_id"],
            "username": inviter.get("username") or "",
            "reason": "邀请人连坐",
        }] if inviter else []
        cascade_rows = available if cascade else []
        objects = [{
            "emby_user_id": target["emby_user_id"],
            "username": target.get("username") or "",
            "role": "target",
        }]
        for row in cascade_rows:
            objects.append({
                "emby_user_id": row["emby_user_id"],
                "username": row.get("username") or "",
                "role": "inviter",
            })
        return {
            "target": {
                "emby_user_id": target["emby_user_id"],
                "username": target.get("username") or "",
                "register_via": target.get("register_via") or "legacy",
            },
            "cascade": cascade_rows,
            "cascade_requested": bool(cascade),
            "default_cascade": False,
            "available_cascade": available,
            "objects": objects,
        }

    def _remove_rows(self, user_id: str) -> None:
        self._db.execute("DELETE FROM members WHERE emby_user_id=?", (user_id,))
        self._db.execute("DELETE FROM devices WHERE emby_user_id=?", (user_id,))

    def delete(self, user_id: str, actor: str = "system",
               cascade: bool = False, *,
               audit_action: str | None = None,
               audit_detail: str | None = None) -> dict[str, Any]:
        """Delete a member. Default is that member only.

        Cascade of the direct inviter is a separate explicit action and still
        stops at one level: walking the whole chain would let a single bad
        account take out an arbitrarily long line of members above it.

        Returns the ids removed rather than a bare True, because with cascade
        the caller cannot otherwise know what it just did.
        """
        if not self._db.one("SELECT 1 AS x FROM members WHERE emby_user_id=?",
                            (user_id,)):
            raise KeyError(user_id)
        target = self.get(user_id) or {}
        inviter = self.inviter_of(user_id) if cascade else None

        self._remove_rows(user_id)
        self.audit(actor, audit_action or "member.delete", user_id,
                   audit_detail or ("membership removed" + (
                       "; cascading to inviter" if inviter else "")))
        deleted = [str(user_id)]

        if inviter:
            inviter_id = str(inviter["emby_user_id"])
            self._remove_rows(inviter_id)
            # A distinct action, not another member.delete: the operator never
            # asked for this row, and the audit trail has to say whose deletion
            # took it with them.
            self.audit(actor, "member.delete.cascade", inviter_id,
                       f"cascaded from {target.get('username') or user_id}")
            deleted.append(inviter_id)
            # Orphaned invitees keep their history: blanking inviter_id would
            # erase who brought them in, which is the one fact the tree exists
            # to record.
        return {"deleted": deleted, "emby_deleted": []}

    def register_device(self, user_id: str, device_id: str, *,
                        device_name: str = "", client: str = "",
                        app_version: str = "", last_ip: str = "",
                        now: int | None = None) -> bool:
        """Record a device, refusing new ones once the member's cap is hit.

        Existing devices always refresh: kicking someone off a phone they
        already use because they opened a second app on it would be wrong.
        The cap only applies to *new* device ids.
        """
        if not device_id:
            return True
        now = now or int(time.time())
        existing = self._db.one(
            "SELECT 1 AS x FROM devices WHERE emby_user_id=? AND device_id=?",
            (user_id, device_id))
        if existing:
            blocked = self._db.one(
                "SELECT blocked FROM devices WHERE emby_user_id=? AND device_id=?",
                (user_id, device_id))
            self._db.execute(
                "UPDATE devices SET device_name=?,client=?,app_version=?,"
                "last_ip=?,last_seen_at=? WHERE emby_user_id=? AND device_id=?",
                (device_name, client, app_version, last_ip, now, user_id, device_id))
            if blocked and blocked.get("blocked"):
                self.audit("system", "device.blocked", user_id,
                           encode_audit_detail({
                               "device_id": {"from": device_id, "to": device_id},
                           }), ok=False)
                return False
            return True

        member = self.get(user_id)
        cap = int((member or {}).get("max_devices") or 0)
        if cap > 0:
            count = self._db.one(
                "SELECT COUNT(*) AS n FROM devices WHERE emby_user_id=? AND blocked=0",
                (user_id,))["n"]
            if count >= cap:
                self.audit("system", "device.refused", user_id,
                           f"device={device_id} cap={cap}", ok=False)
                return False

        self._db.execute(
            "INSERT INTO devices (emby_user_id,device_id,device_name,client,"
            "app_version,last_ip,first_seen_at,last_seen_at,blocked) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (user_id, device_id, device_name, client, app_version, last_ip, now, now))
        return True

    # -- lifecycle actions ---------------------------------------------------
    def renew(self, user_id: str, days: int | None = None,
              actor: str = "operator") -> dict[str, Any]:
        """Extend the term from the *effective* expiry.

        If a personal expires_at_override is set, the new date is written to
        that overlay; otherwise the stored expires_at is updated. A permanent
        account (no effective expiry) is refused rather than silently becoming
        time-limited.
        """
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        group = member.get("group")
        default_days = int(group["duration_days"]) if group else 0
        add_days = int(days if days is not None else default_days)
        if add_days <= 0:
            raise ConfigError("续期天数必须大于 0")
        effective = member.get("expires_at_effective")
        if not effective:
            raise ConfigError("永久用户不能通过续期变为有限期，请先明确设置到期时间")
        now = int(time.time())
        base = max(now, int(effective))
        new_expiry = base + add_days * 86400
        ov = dict(member.get("overrides") or {})
        writes_override = "expires_at_override" in ov
        override_before = ov.get("expires_at_override") if writes_override else None
        if writes_override:
            ov["expires_at_override"] = new_expiry
            self._db.execute(
                "UPDATE members SET overrides_json=?,status=CASE WHEN status IN "
                "('expired','exhausted') THEN 'active' ELSE status END,updated_at=? "
                "WHERE emby_user_id=?",
                (json.dumps(ov, ensure_ascii=False, sort_keys=True), now, user_id))
        else:
            self._db.execute(
                "UPDATE members SET expires_at=?,status=CASE WHEN status IN "
                "('expired','exhausted') THEN 'active' ELSE status END,updated_at=? "
                "WHERE emby_user_id=?", (new_expiry, now, user_id))
        self.audit(actor, "member.renew", user_id, encode_audit_detail({
            "expires_at": {"from": member.get("expires_at"), "to": (
                member.get("expires_at") if writes_override else new_expiry)},
            "expires_at_effective": {
                "from": effective, "to": new_expiry},
            "expires_at_override": {
                "from": override_before,
                "to": new_expiry if writes_override else None},
            "days": {"from": None, "to": add_days},
        }))
        return self.get(user_id)  # type: ignore[return-value]

    def reset_traffic(self, user_id: str, actor: str = "operator") -> dict[str, Any]:
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        now = int(time.time())
        used_before = int(member.get("traffic_used_bytes") or 0)
        self._db.execute(
            "UPDATE members SET traffic_used_bytes=0,traffic_period_start=?,"
            "status=CASE WHEN status='exhausted' THEN 'active' ELSE status END,"
            "updated_at=? WHERE emby_user_id=?",
            (period_start(now), now, user_id))
        if self._metering is not None:
            with contextlib.suppress(Exception):
                self._metering.reset_credit(user_id, now=now)
        self.audit(actor, "member.reset_traffic", user_id, encode_audit_detail({
            "traffic_used_bytes": {"from": used_before, "to": 0},
        }))
        return self.get(user_id)  # type: ignore[return-value]

    def set_status(self, user_id: str, status: str, actor: str = "operator"
                   ) -> dict[str, Any]:
        if status not in MEMBER_STATES:
            raise ConfigError(f"状态必须是 {'/'.join(MEMBER_STATES)} 之一")
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        before = member.get("status")
        self._db.execute(
            "UPDATE members SET status=?,updated_at=? WHERE emby_user_id=?",
            (status, int(time.time()), user_id))
        self.audit(actor, "member.status", user_id, encode_audit_detail({
            "status": {"from": before, "to": status},
        }))
        return self.get(user_id)  # type: ignore[return-value]

    def roll_periods(self, now: int | None = None) -> int:
        """Monthly reset: zero usage, drop one-off extra traffic, unblock
        exhausted members. Manual states are never touched."""
        now = now or int(time.time())
        rolled = 0
        current = period_start(now)
        for member in self.list(limit=5000):
            group = member.get("group")
            if not group or not needs_traffic(group["billing_mode"]):
                continue
            if int(member.get("traffic_period_start") or 0) >= current:
                continue
            ov = dict(member.get("overrides") or {})
            extra_before = int(ov.get("extra_traffic_bytes") or 0)
            if extra_before:
                ov.pop("extra_traffic_bytes", None)
            self._db.execute(
                "UPDATE members SET traffic_used_bytes=0,traffic_period_start=?,"
                "status=CASE WHEN status='exhausted' THEN 'active' ELSE status END,"
                "overrides_json=?,updated_at=? WHERE emby_user_id=?",
                (current, json.dumps(ov, ensure_ascii=False, sort_keys=True),
                 now, member["emby_user_id"]))
            if self._metering is not None:
                with contextlib.suppress(Exception):
                    self._metering.reset_credit(member["emby_user_id"], now=now)
            self.audit("system", "member.period_roll", member["emby_user_id"],
                       encode_audit_detail({
                           "extra_traffic_bytes": {"from": extra_before, "to": 0},
                       }))
            rolled += 1
        return rolled

    def add_traffic(self, user_id: str, delta_bytes: int) -> None:
        if delta_bytes <= 0:
            return
        self._db.execute(
            "UPDATE members SET traffic_used_bytes=traffic_used_bytes+?,"
            "last_seen_at=?,updated_at=? WHERE emby_user_id=?",
            (int(delta_bytes), int(time.time()), int(time.time()), user_id))

    def set_overrides(self, user_id: str, payload: dict[str, Any] | None,
                      actor: str = "operator") -> dict[str, Any]:
        """Replace the overlay wholesale. Empty object restores full inheritance."""
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        cleaned = validate_overrides(payload or {})
        before = member.get("overrides") or {}
        self._db.execute(
            "UPDATE members SET overrides_json=?,updated_at=? WHERE emby_user_id=?",
            (json.dumps(cleaned, ensure_ascii=False, sort_keys=True),
             int(time.time()), user_id))
        self.audit(actor, "member.overrides", user_id,
                   encode_audit_detail(audit_diff(before, cleaned)))
        return self.get(user_id)  # type: ignore[return-value]

    def add_extra_traffic(self, user_id: str, delta_bytes: int,
                          actor: str = "operator") -> dict[str, Any]:
        """Accumulate extra_traffic_bytes on the overlay (current month)."""
        if delta_bytes < 0:
            raise ConfigError("额外流量必须大于等于 0")
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        ov = dict(member.get("overrides") or {})
        before = int(ov.get("extra_traffic_bytes") or 0)
        ov["extra_traffic_bytes"] = before + int(delta_bytes)
        return self.set_overrides(user_id, ov, actor=actor)

    def set_device_blocked(self, user_id: str, device_id: str, blocked: bool,
                           actor: str = "operator") -> dict[str, Any]:
        row = self._db.one(
            "SELECT * FROM devices WHERE emby_user_id=? AND device_id=?",
            (user_id, device_id))
        if not row:
            raise KeyError(device_id)
        was = bool(row.get("blocked"))
        want = bool(blocked)
        if was != want:
            self._db.execute(
                "UPDATE devices SET blocked=? WHERE emby_user_id=? AND device_id=?",
                (1 if want else 0, user_id, device_id))
            self.audit(actor, "device.block" if want else "device.unblock",
                       user_id, encode_audit_detail({
                           "device_id": {"from": device_id, "to": device_id},
                           "blocked": {"from": was, "to": want},
                       }))
        return self._db.one(
            "SELECT * FROM devices WHERE emby_user_id=? AND device_id=?",
            (user_id, device_id)) or {}

    def devices(self, user_id: str) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT * FROM devices WHERE emby_user_id=? ORDER BY last_seen_at DESC",
            (user_id,))

    # -- audit ---------------------------------------------------------------
    def audit(self, actor: str, action: str, subject: str = "",
              detail: str = "", ok: bool = True) -> None:
        self._db.execute(
            "INSERT INTO audit_log (ts,actor,action,subject,detail,ok) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), actor[:60], action[:60], subject[:80],
             detail[:AUDIT_DETAIL_MAX], 1 if ok else 0))

    def audit_log(self, limit: int = 100, offset: int = 0,
                  subject: str | None = None, actor: str | None = None,
                  action: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if subject:
            clauses.append("subject=?")
            params.append(subject)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if action:
            clauses.append("action=?")
            params.append(action)
        sql = "SELECT * FROM audit_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.append(max(1, min(limit, 1000)))
        params.append(max(0, int(offset)))
        return self._db.query(sql, tuple(params))

    def audit_count(self, subject: str | None = None, actor: str | None = None,
                    action: str | None = None) -> int:
        clauses, params = [], []
        if subject:
            clauses.append("subject=?")
            params.append(subject)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if action:
            clauses.append("action=?")
            params.append(action)
        sql = "SELECT COUNT(*) AS n FROM audit_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = self._db.one(sql, tuple(params))
        return int((row or {}).get("n") or 0)
