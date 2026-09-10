"""Member operations — observation, action envelopes, and safe remote delete.

Membership rows and Emby accounts are different facts. A member can be
entitled (state=active) while the Emby account is missing, drifted, or
never applied. Callers must not collapse those into a single "enabled"
label, and a remote failure must never be reported as a local success.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

from app.core.errors import ConfigError, ConflictError
from app.modules.enforcement import MANAGED_KEYS, _normalise, desired_policy, fingerprint
from app.modules.groups import needs_duration, needs_traffic

# Never persist secrets into last_remote_error / audit / task tables.
_SECRET_RE = re.compile(
    r'''(?i)(password|passwd|pwd|secret|token|api[_-]?key)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s&,}]+)'''
)
_ABSENT_RE = re.compile(r"\b(404|not\s*found|no such user|user not found)\b", re.IGNORECASE)

def activity_timestamp(member: dict[str, Any]) -> int:
    """Sort by the same Emby activity timestamp displayed in the member row."""
    raw = member.get("last_activity")
    if raw:
        try:
            stamp = datetime.fromisoformat(str(raw))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return int(stamp.timestamp())
        except (ValueError, OverflowError):
            pass
    return int(member.get("last_activity_ts") or member.get("last_seen_at") or 0)


SORTS = {
    "username": lambda m: (m.get("username") or "").lower(),
    "group": lambda m: (m.get("group_name") or "").lower(),
    "expires": lambda m: int(m.get("expires_at_effective", m.get("expires_at")) or 0),
    "state": lambda m: m.get("entitlement_state") or m.get("state") or "",
    "emby": lambda m: m.get("emby_status") or "",
    "sync": lambda m: m.get("sync_status") or "",
    "used": lambda m: int(m.get("traffic_used_bytes") or 0),
    "traffic": lambda m: int(m.get('measured_used_bytes') if m.get('measured_used_bytes') is not None else -1),
    "edge30": lambda m: int((m.get("edge") or {}).get("bytes_30d") or 0),
    "last_seen": activity_timestamp,
}


def redact(text: Any) -> str:
    raw = str(text or "")
    # URLs can carry bot tokens, API keys, signed links or userinfo, even
    # without a key=value label. Never retain credential-bearing locations.
    raw = re.sub(r'https?://[^\s<>]+', '[remote URL]', raw, flags=re.IGNORECASE)
    raw = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer ***', raw)
    return _SECRET_RE.sub(r"\1=***", raw)[:240]


def is_absent_error(exc: BaseException | str) -> bool:
    return bool(_ABSENT_RE.search(str(exc or "")))


def action_result(*, local_ok: bool = True, remote_ok: bool | None = None,
                  error: str = "", errors: list[dict[str, Any]] | None = None,
                  retryable: bool = False, **extra: Any) -> dict[str, Any]:
    """Flattened envelope mixed onto member / delete payloads.

    remote_ok is None when the remote side was not attempted (enforcement
    off, skipped admin, etc.). ok is true only when local succeeded and the
    remote side did not fail.
    """
    err_list = list(errors or [])
    if error and not err_list:
        err_list = [{"target": "", "stage": "remote", "error": redact(error),
                     "retryable": retryable}]
    ok = bool(local_ok) and (remote_ok is not False)
    return {
        "ok": ok,
        "local_ok": bool(local_ok),
        "remote_ok": remote_ok,
        "retryable": bool(retryable),
        "error": redact(error),
        "errors": err_list,
        **extra,
    }


def merge_action(payload: dict[str, Any] | None, result: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload or {})
    for key in ("ok", "local_ok", "remote_ok", "retryable", "error", "errors"):
        if key in result:
            out[key] = result[key]
    return out


def record_remote(members: Any, user_id: str, action: str, *, ok: bool,
                  error: str = "", actor: str = "system") -> None:
    now = int(time.time())
    members._db.execute(
        "UPDATE members SET last_remote_action=?,last_remote_ok=?,"
        "last_remote_error=?,last_remote_at=?,updated_at=? WHERE emby_user_id=?",
        (str(action or "")[:40], 1 if ok else 0, redact(error), now, now, user_id))
    if not ok:
        members.audit(actor, f"{action}.fail" if action else "remote.fail",
                      user_id, redact(error) or "remote failed", ok=False)


def known_member_ids(members: Any) -> set[str]:
    return {str(r["emby_user_id"]) for r in members._db.query(
        "SELECT emby_user_id FROM members")}


def observe_one(member: dict[str, Any], emby_user: dict[str, Any] | None,
                *, emby_available: bool) -> dict[str, Any]:
    """Split entitlement / Emby presence / policy sync into separate fields."""
    out = dict(member)
    out["entitlement_state"] = out.get("state") or "active"
    last_ok = out.get("last_remote_ok")
    last_action = str(out.get("last_remote_action") or "")
    last_err = redact(out.get("last_remote_error") or "")
    out["last_remote_error"] = last_err
    out["retryable"] = (last_ok == 0 or last_ok is False) and bool(last_action)
    # A current observation and an historical write receipt are independent.
    # Neither a matching remote policy nor a GET creates an apply timestamp.
    out["sync_recorded"] = bool(out.get("applied_fingerprint"))
    out["policy_matches"] = None

    if not emby_available:
        out["emby_status"] = "unknown"
        out["emby_disabled"] = None
        out["emby_is_admin"] = None
        out["sync_status"] = "unknown"
        return out

    if emby_user is None:
        out["emby_status"] = "missing"
        out["emby_disabled"] = None
        out["emby_is_admin"] = None
        out["sync_status"] = "emby_missing"
        return out

    policy = emby_user.get("Policy")
    policy = policy if isinstance(policy, dict) else {}
    out["emby_status"] = "present"
    out["emby_disabled"] = (bool(policy['IsDisabled'])
                            if policy.get('IsDisabled') is not None else None)
    out["emby_is_admin"] = (bool(policy['IsAdministrator'])
                            if policy.get('IsAdministrator') is not None else None)
    if out['emby_is_admin']:
        out["sync_status"] = "skipped_admin"
        return out
    want = desired_policy(out)
    if any(k not in policy or policy[k] is None for k in want):
        # Presence is known, but an absent/partial Policy cannot prove its
        # effective limits, nor prove that the account is enabled.
        out["sync_status"] = "unknown"
        return out
    out["policy_matches"] = not any(
        _normalise(policy[k]) != _normalise(want[k])
        for k in MANAGED_KEYS if k in want)
    if last_action == 'enforce' and (last_ok == 0 or last_ok is False):
        out["sync_status"] = "failed"
    elif out["policy_matches"]:
        out["sync_status"] = ('in_sync' if fingerprint(want) == out.get('applied_fingerprint')
                              else 'policy_match')
    else:
        out["sync_status"] = 'drift' if out['sync_recorded'] else 'never_applied'
    return out


def attach_observation(rows: list[dict[str, Any]],
                       emby_users: dict[str, dict[str, Any]] | None,
                       *, emby_error: str | None = None) -> list[dict[str, Any]]:
    available = emby_users is not None and not emby_error
    observed = []
    for row in rows:
        uid = str(row.get("emby_user_id") or "")
        emby_user = (emby_users or {}).get(uid) if available else None
        observed.append(observe_one(row, emby_user, emby_available=available))
    return observed


def counts_for(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total": len(rows),
        "active": 0, "expired": 0, "exhausted": 0, "suspended": 0, "pending": 0,
        "emby_missing": 0, "sync_drift": 0, "sync_failed": 0,
    }
    for row in rows:
        state = str(row.get("entitlement_state") or row.get("state") or "")
        if state in counts:
            counts[state] += 1
        if row.get("emby_status") == "missing":
            counts["emby_missing"] += 1
        if row.get("sync_status") == "drift":
            counts["sync_drift"] += 1
        if row.get("sync_status") == "failed":
            counts["sync_failed"] += 1
    return counts


def apply_list_filters(rows: list[dict[str, Any]], *,
                       tg: str | None = None,
                       expiring: str | None = None,
                       emby_status: str | None = None,
                       sync_status: str | None = None,
                       now: int | None = None) -> list[dict[str, Any]]:
    now = now or int(time.time())
    out = rows
    if tg == "bound":
        out = [r for r in out if r.get("tg_user_id")]
    elif tg == "unbound":
        out = [r for r in out if not r.get("tg_user_id")]
    if expiring == "soon":
        out = [r for r in out
               if r.get("days_remaining") is not None
               and 0 <= int(r["days_remaining"]) <= 7
               and r.get("state") != "expired"]
    elif expiring == "gone":
        out = [r for r in out if r.get("state") == "expired"]
    if emby_status:
        out = [r for r in out if r.get("emby_status") == emby_status]
    if sync_status:
        out = [r for r in out if r.get("sync_status") == sync_status]
    return out


def sort_rows(rows: list[dict[str, Any]], sort: str | None,
              order: str | None) -> list[dict[str, Any]]:
    key = SORTS.get(str(sort or "username"), SORTS["username"])
    reverse = str(order or "asc").lower() == "desc"
    return sorted(rows, key=key, reverse=reverse)


def paginate(rows: list[dict[str, Any]], *, page: int, page_size: int
             ) -> tuple[list[dict[str, Any]], int, int]:
    page_size = max(1, min(int(page_size or 50), 200))
    page = max(1, int(page or 1))
    offset = (page - 1) * page_size
    return rows[offset:offset + page_size], page, offset


def preview_objects(preview: dict[str, Any]) -> list[str]:
    return [str(o.get("emby_user_id") or "")
            for o in (preview.get("objects") or []) if o.get("emby_user_id")]


def validate_confirm_ids(preview: dict[str, Any], confirm_ids: Any,
                         *, cascade: bool) -> None:
    expected = preview_objects(preview)
    if cascade:
        if not isinstance(confirm_ids, list) or not confirm_ids:
            raise ConflictError("连带删除必须提供完整 confirm_ids，且与预览对象完全一致")
        got = [str(x) for x in confirm_ids]
        if sorted(got) != sorted(expected):
            raise ConflictError("confirm_ids 与预览对象不一致，拒绝扩大删除范围")
        return
    if confirm_ids is None:
        return
    if not isinstance(confirm_ids, list):
        raise ConflictError("confirm_ids 必须是用户 id 列表")
    got = [str(x) for x in confirm_ids]
    if sorted(got) != sorted(expected):
        raise ConflictError("confirm_ids 与预览对象不一致，拒绝扩大删除范围")


def group_preview(members: Any, user_id: str, group_id: str) -> dict[str, Any]:
    member = members.get(user_id)
    if not member:
        raise KeyError(user_id)
    to_group = members._groups.get(group_id)
    if not to_group:
        raise ConfigError(f"用户组不存在: {group_id}")
    from_group = member.get("group") or {}
    current_effective = member.get("expires_at_effective")
    current_is_permanent = current_effective in (None, "", 0)
    to_timed = needs_duration(to_group.get("billing_mode") or "")
    now = int(time.time())
    apply_at = (now + int(to_group.get("duration_days") or 0) * 86400
                if to_timed else None)
    decision_required = current_is_permanent and to_timed
    warnings: list[str] = []
    if decision_required:
        warnings.append("永久/无到期账号切到计时组，必须明确 keep 或 apply_group")
    if current_effective and to_timed and apply_at and int(current_effective) > apply_at + 60:
        warnings.append("切到目标组若套用 duration_days 会缩短现有有效期；默认保留")
    if not to_timed:
        warnings.append("目标组不计时：到期改为不限，并清除个人到期覆盖")
    if not needs_traffic(to_group.get('billing_mode') or ''):
        warnings.append("目标组不计流量：有效配额不限，历史用量保留")
    return {
        "from_group": {
            "id": from_group.get("id") or member.get("group_id"),
            "name": from_group.get("name") or member.get("group_name"),
            "billing_mode": from_group.get("billing_mode") or member.get("billing_mode"),
        },
        "to_group": {
            "id": to_group.get("id"),
            "name": to_group.get("name"),
            "billing_mode": to_group.get("billing_mode"),
            "duration_days": to_group.get("duration_days"),
        },
        "current_expires_at": member.get("expires_at"),
        "current_expires_at_effective": current_effective,
        "current_is_permanent": current_is_permanent,
        "overrides_kept": to_timed or 'expires_at_override' not in (member.get('overrides') or {}),
        "non_expiry_overrides_kept": True,
        "decision_required": decision_required,
        "default_policy": "keep",
        "policies": {
            "keep": {
                "expires_at": current_effective if to_timed else None,
                "expires_at_override": "unchanged" if to_timed else "cleared",
                "note": "保留实际有效期" if to_timed else "不计时组改为不限期",
            },
            "apply_group": {
                "expires_at": apply_at,
                "note": "按目标组 duration_days" if to_timed else "目标组不计时，将清空到期",
            },
            "clear": {"expires_at": None},
        },
        "warnings": warnings,
    }


def renew_preview(members: Any, user_id: str, days: int | None) -> dict[str, Any]:
    member = members.get(user_id)
    if not member:
        raise KeyError(user_id)
    group = member.get("group") or {}
    default_days = int(group.get("duration_days") or 0)
    add_days = int(days if days is not None else default_days)
    effective = member.get("expires_at_effective")
    is_permanent = effective in (None, "", 0)
    now = int(time.time())
    new_expiry = None if is_permanent or add_days <= 0 else (
        max(now, int(effective)) + add_days * 86400)
    writes_override = "expires_at_override" in (member.get("overrides") or {})
    return {
        "days": add_days,
        "current_expires_at": member.get("expires_at"),
        "current_expires_at_effective": effective,
        "current_is_permanent": is_permanent,
        "writes_override": writes_override,
        "new_expires_at": new_expiry,
        "allowed": not is_permanent and add_days > 0,
        "warnings": (["永久用户不能暗变有限期"] if is_permanent else []),
    }


async def delete_emby_one(emby: Any, user_id: str,
                          live_ids: set[str] | None) -> dict[str, Any]:
    """Delete one Emby account. 404 / already-absent is success."""
    try:
        ok = bool(await emby.delete_user(user_id))
    except Exception as exc:  # noqa: BLE001
        if is_absent_error(exc):
            return {"status": "already_gone", "error": ""}
        return {"status": "failed", "error": redact(exc), "retryable": True}
    if ok:
        return {"status": "deleted", "error": ""}
    if live_ids is not None and user_id not in live_ids:
        return {"status": "already_gone", "error": ""}
    if live_ids is None:
        # Could not list users to confirm absence; treat False as retryable.
        return {"status": "failed",
                "error": "Emby 删除未确认成功", "retryable": True}
    return {"status": "failed", "error": "Emby 账号仍存在", "retryable": True}


async def execute_delete(members: Any, emby: Any | None, user_id: str, *,
                         actor: str, cascade: bool = False,
                         delete_emby: bool = True,
                         confirm_ids: Any = None, authorize: Any = None) -> dict[str, Any]:
    preview = members.delete_preview(user_id, cascade=cascade)
    validate_confirm_ids(preview, confirm_ids, cascade=cascade)
    objects = list(preview.get("objects") or [])
    live_ids: set[str] | None = None
    if delete_emby and emby is not None:
        try:
            users = await emby.list_users()
            # Empty/partial-unreadable listings are not affirmative evidence
            # that a failed deletion actually removed the account.
            live_ids = {str(u['Id']) for u in users if u.get('Id')} or None
        except Exception:  # noqa: BLE001
            live_ids = None

    emby_deleted: list[str] = []
    emby_already_gone: list[str] = []
    emby_failed: list[dict[str, Any]] = []
    removed: list[str] = []
    retained: list[str] = []
    errors: list[dict[str, Any]] = []

    for obj in objects:
        uid = str(obj.get("emby_user_id") or "")
        if not uid:
            continue
        role = str(obj.get("role") or "target")
        if authorize is not None and not authorize():
            err = '管理员身份或权限已变化，未执行删除'
            retained.append(uid)
            emby_failed.append({'user_id': uid, 'error': err, 'retryable': False})
            errors.append({'target': uid, 'stage': 'authority', 'error': err, 'retryable': False})
            continue
        if delete_emby and emby is not None:
            remote = await delete_emby_one(emby, uid, live_ids)
            if remote["status"] == "deleted":
                emby_deleted.append(uid)
                record_remote(members, uid, "delete_emby", ok=True, actor=actor)
                members.audit(actor, "member.delete_emby", uid,
                              "Emby account deleted")
            elif remote["status"] == "already_gone":
                emby_already_gone.append(uid)
                record_remote(members, uid, "delete_emby", ok=True, actor=actor)
                members.audit(actor, "member.delete_emby", uid,
                              "Emby account already gone")
            else:
                err = remote.get("error") or "Emby delete failed"
                emby_failed.append({"user_id": uid, "error": err,
                                    "retryable": True})
                errors.append({"target": uid, "stage": "emby", "error": err,
                               "retryable": True})
                record_remote(members, uid, "delete_emby", ok=False,
                              error=err, actor=actor)
                members.audit(actor, "member.delete_emby", uid, err, ok=False)
                retained.append(uid)
                continue
        elif delete_emby and emby is None:
            err = "Emby 未配置"
            emby_failed.append({"user_id": uid, "error": err, "retryable": True})
            errors.append({"target": uid, "stage": "emby", "error": err,
                           "retryable": True})
            retained.append(uid)
            continue

        if members.get(uid):
            members.delete(uid, actor=actor, cascade=False,
                           audit_action=("member.delete.cascade"
                                         if role == "inviter" else "member.delete"),
                           audit_detail=(f"cascaded from {user_id}"
                                         if role == "inviter" else "membership removed"))
        removed.append(uid)

    remote_ok: bool | None
    if not delete_emby:
        remote_ok = None
    else:
        remote_ok = not emby_failed
    result = action_result(
        local_ok=not retained, remote_ok=remote_ok,
        error=emby_failed[0]["error"] if emby_failed else "",
        errors=errors, retryable=bool(emby_failed),
        deleted=not retained and bool(removed),
        removed=removed, retained=retained,
        emby_deleted=emby_deleted,
        emby_already_gone=emby_already_gone,
        emby_failed=emby_failed,
        **preview,
    )
    # deleted only means local rows are gone; ok reflects remote too.
    result["deleted"] = not retained and user_id in removed
    return result


def live_pulse(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact SSE snapshot so the members page can patch without a rebuild."""
    counts = counts_for(rows)
    pulse = [{
        "id": r.get("emby_user_id"),
        "state": r.get("entitlement_state") or r.get("state"),
        "emby_status": r.get("emby_status"),
        "sync_status": r.get("sync_status"),
        "last_seen_at": r.get("last_seen_at"),
        "traffic_used_bytes": r.get("traffic_used_bytes") or 0,
        "measured_used_bytes": r.get("measured_used_bytes")
        if "measured_used_bytes" in r
        else ((r.get("metering") or {}).get("measured_used_bytes")
              if isinstance(r.get("metering"), dict) else None),
        "quota_source": r.get("quota_source"),
        "metering_source": (r.get("metering") or {}).get("source")
        if isinstance(r.get("metering"), dict) else None,
        "edge_30d": int((r.get("edge") or {}).get("bytes_30d") or 0),
        "last_remote_ok": r.get("last_remote_ok"),
        "last_remote_at": r.get("last_remote_at"),
        "retryable": bool(r.get("retryable")),
    } for r in rows]
    return {"total": counts["total"], "counts": counts, "pulse": pulse}


def policy_drifted(member: dict[str, Any], current: dict[str, Any]) -> bool:
    want = desired_policy(member)
    return any(
        _normalise(current.get(k)) != _normalise(want.get(k))
        for k in MANAGED_KEYS if k in want)


def enforce_skip_reason(member: dict[str, Any] | None,
                        emby_user: dict[str, Any] | None) -> str | None:
    if not member:
        return "unenrolled"
    if not emby_user:
        return "emby_user_missing"
    if (emby_user.get("Policy") or {}).get("IsAdministrator"):
        return "administrator"
    return None
