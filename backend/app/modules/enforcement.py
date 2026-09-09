"""Enforcement — project member state onto the Emby account.

A group default is only a promise until something writes it into Emby.  This
module is that something.  It maps every effective limit onto the Emby policy
field that actually enforces it, so limits hold even when the panel is down:

    max_streams          -> SimultaneousStreamLimit
    bandwidth_limit_kbps -> RemoteClientBitrateLimit (Emby's per-user
                            remote *bandwidth* cap, bits/second)
    allow_transcode      -> Enable{Video,Audio}PlaybackTranscoding + Remuxing
    allow_download       -> EnableContentDownloading + sync conversion
    libraries            -> EnableAllFolders / EnabledFolders
    expired/exhausted    -> IsDisabled

Three rules keep this safe on a server with hundreds of pre-existing accounts:

**Membership is opt-in.**  Reconciliation iterates member rows, never Emby's
user list.  An account the operator never enrolled is untouched, so no bug here
can disable the whole server.

**Administrators are never disabled.**  Locking the operator out of their own
server via a quota rounding error is not an acceptable failure mode.

**Writes are fingerprinted.**  The desired policy is hashed; if it matches what
was last applied, nothing is sent.  Otherwise a nightly reconcile would rewrite
hundreds of policies and bury real changes in noise.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from app.core.db import Database
from app.modules.members import MemberService

# States in which the account must not be able to play.
BLOCKING_STATES = ("expired", "exhausted", "suspended", "pending")

# Policy keys this module owns. Anything outside this set is left exactly as
# the operator configured it in Emby -- the panel manages access, not every
# preference on the account.
MANAGED_KEYS = (
    "IsDisabled",
    "SimultaneousStreamLimit",
    "RemoteClientBitrateLimit",
    "EnableVideoPlaybackTranscoding",
    "EnableAudioPlaybackTranscoding",
    "EnablePlaybackRemuxing",
    "EnableContentDownloading",
    "EnableSyncTranscoding",
    "EnableMediaConversion",
    "EnableAllFolders",
    "EnabledFolders",
)


def desired_policy(member: dict[str, Any]) -> dict[str, Any]:
    """The Emby policy fields implied by this member's effective limits."""
    group = member.get("group") or {}
    state = member.get("state", "active")
    blocked = state in BLOCKING_STATES
    effective = member.get("effective") or {}

    policy: dict[str, Any] = {"IsDisabled": blocked}

    if not group:
        # Enrolled but not grouped: manage nothing except the block flag, so
        # an operator can park an account without inheriting limits.
        return policy

    # Prefer the merged overlay; fall back to the group so a caller that only
    # passed the raw member+group still produces the same mapping.
    streams = effective.get("max_streams", group.get("max_streams") or 0)
    bandwidth = effective.get(
        "bandwidth_limit_kbps", group.get("bandwidth_limit_kbps") or 0)
    transcode = effective.get("allow_transcode", group.get("allow_transcode"))
    download = effective.get("allow_download", group.get("allow_download"))
    libraries = list(effective.get("libraries") or [])

    # Emby treats 0 as "no limit" for both fields, matching our convention.
    policy["SimultaneousStreamLimit"] = int(streams or 0)
    policy["RemoteClientBitrateLimit"] = int(bandwidth or 0) * 1000

    transcode = bool(transcode)
    policy["EnableVideoPlaybackTranscoding"] = transcode
    policy["EnableAudioPlaybackTranscoding"] = transcode
    policy["EnablePlaybackRemuxing"] = transcode

    download = bool(download)
    policy["EnableContentDownloading"] = download
    # Offline sync is part of the download permission in the group model.
    policy["EnableSyncTranscoding"] = download
    policy["EnableMediaConversion"] = download

    if libraries:
        policy["EnableAllFolders"] = False
        policy["EnabledFolders"] = libraries
    else:
        policy["EnableAllFolders"] = True
        policy["EnabledFolders"] = []
    return policy


def fingerprint(policy: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


class EnforcementService:
    def __init__(self, db: Database, members: MemberService, emby: Any) -> None:
        self._db = db
        self._members = members
        self._emby = emby
        self._apply_lock = asyncio.Lock()

    async def reconcile(self, apply: bool = False, user_id: str | None = None,
                        force: bool = False) -> dict[str, Any]:
        async with self._apply_lock:
            return await self._reconcile(apply, user_id, force)

    async def _reconcile(self, apply: bool = False, user_id: str | None = None,
                        force: bool = False) -> dict[str, Any]:
        """Bring Emby in line with member state.

        Defaults to dry-run: this writes to live accounts, so producing the
        plan and applying it are separate decisions.
        """
        rows = ([self._members.get(user_id)] if user_id
                else self._members.list(limit=5000))
        rows = [r for r in rows if r]

        # One Emby read for the whole pass rather than one per member: on a
        # 300-account server that is the difference between a second and a
        # minute, and reconcile runs on a timer.
        try:
            emby_users = {u["Id"]: u for u in await self._emby.list_users()}
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "无法读取 Emby 用户列表",
                    "planned": 0, "applied": 0}

        planned, applied, skipped, errors = [], 0, [], []
        for member in rows:
            uid = member["emby_user_id"]
            member = self._members.get(uid)
            if not member:
                skipped.append({'user_id': uid, 'reason': 'unenrolled'})
                continue
            emby_user = emby_users.get(uid)
            if not emby_user:
                # The Emby account is gone; flag it rather than deleting the
                # member row, so the operator decides what happened.
                skipped.append({"user_id": uid, "reason": "emby_user_missing",
                                "username": member.get("username")})
                continue

            current = emby_user.get("Policy") or {}
            if current.get("IsAdministrator"):
                skipped.append({"user_id": uid, "reason": "administrator",
                                "username": member.get("username")})
                continue

            want = desired_policy(member)
            fp = fingerprint(want)
            # Already applied and unchanged. Still verify every managed field,
            # not just IsDisabled: an operator (or another tool) can drift
            # streams/libraries/download flags without flipping the disable bit.
            unchanged = not force and fp == member.get("applied_fingerprint")
            if unchanged:
                drifted = any(
                    _normalise(current.get(k)) != _normalise(want.get(k))
                    for k in MANAGED_KEYS if k in want)
                if not drifted:
                    continue

            diff = {k: v for k, v in want.items()
                    if _normalise(current.get(k)) != _normalise(v)}
            if not diff and not force:
                if apply and fp != member.get("applied_fingerprint"):
                    self._db.execute(
                        "UPDATE members SET applied_fingerprint=?,applied_at=? "
                        "WHERE emby_user_id=?", (fp, int(time.time()), uid))
                continue

            planned.append({
                "user_id": uid,
                "username": member.get("username"),
                "state": member.get("state"),
                "group": member.get("group_name"),
                "changes": diff,
            })

            if not apply:
                continue

            try:
                outcome = await self._emby.apply_member_policy(uid, want)
                if outcome['status'] == 'skipped_admin':
                    skipped.append({'user_id': uid, 'reason': 'administrator',
                                    'username': member.get('username')})
                    continue  # no remote success record or applied fingerprint
                ok = outcome['status'] == 'applied'
            except Exception as exc:  # noqa: BLE001
                ok = False
                from app.modules.member_ops import redact
                errors.append({"user_id": uid, "error": redact(exc)})
            if ok:
                applied += 1
                self._db.execute(
                    "UPDATE members SET applied_fingerprint=?,applied_at=? "
                    "WHERE emby_user_id=?", (fp, int(time.time()), uid))
                self._members.audit(
                    "system", "enforce.apply", uid,
                    f"state={member.get('state')} changes={sorted(diff)}")
                self._record_remote(uid, "enforce", ok=True)
            else:
                if not any(e.get("user_id") == uid for e in errors):
                    errors.append({"user_id": uid,
                                   "error": "apply_member_policy failed"})
                self._members.audit(
                    "system", "enforce.fail", uid,
                    f"state={member.get('state')}", ok=False)
                self._record_remote(
                    uid, "enforce", ok=False,
                    error=next((e["error"] for e in errors
                                if e.get("user_id") == uid), "enforce failed"))

        return {
            "ok": not errors,
            "dry_run": not apply,
            "considered": len(rows),
            "planned": len(planned),
            "applied": applied,
            "changes": planned[:200],
            "skipped": skipped[:100],
            "errors": errors[:50],
            "local_ok": True,
            "remote_ok": (not errors) if apply else None,
            "retryable": bool(errors),
            "error": (errors[0].get("error") if errors else ""),
        }

    def _record_remote(self, user_id: str, action: str, *, ok: bool,
                       error: str = "") -> None:
        from app.modules.member_ops import redact
        now = int(time.time())
        self._db.execute(
            "UPDATE members SET last_remote_action=?,last_remote_ok=?,"
            "last_remote_error=?,last_remote_at=?,updated_at=? "
            "WHERE emby_user_id=?",
            (action[:40], 1 if ok else 0, redact(error), now, now, user_id))

    async def enforce_now(self, user_id: str, reason: str = "") -> dict[str, Any]:
        # A slower old disable must not overwrite a newer renewal/enable.
        async with self._apply_lock:
            return await self._enforce_now(user_id, reason)

    async def _enforce_now(self, user_id: str, reason: str = "") -> dict[str, Any]:
        """Apply one member immediately.

        Used when quota runs out mid-stream: waiting for the next timed pass
        would let a user keep watching well past their limit. Unenrolled
        accounts and Emby administrators are skipped here the same way
        reconcile skips them.
        """
        member = self._members.get(user_id)
        if not member:
            return {"ok": True, "skipped": "unenrolled", "local_ok": True,
                    "remote_ok": None, "retryable": False, "error": "",
                    "errors": []}
        emby_user = None
        try:
            for user in await self._emby.list_users():
                if str(user.get("Id")) == str(user_id):
                    emby_user = user
                    break
        except Exception as exc:  # noqa: BLE001
            from app.modules.member_ops import redact
            err = redact(exc)
            self._record_remote(user_id, "enforce", ok=False, error=err)
            self._members.audit("system", "enforce.fail", user_id,
                                reason or err, ok=False)
            return {"ok": False, "local_ok": True, "remote_ok": False,
                    "retryable": True, "error": err,
                    "errors": [{"target": user_id, "stage": "emby",
                                "error": err, "retryable": True}]}
        member = self._members.get(user_id)
        if not member:
            return {"ok": True, "skipped": "unenrolled", "local_ok": True,
                    "remote_ok": None, "retryable": False, "error": "", "errors": []}
        if not emby_user:
            self._record_remote(user_id, "enforce", ok=False,
                                error="emby_user_missing")
            return {"ok": False, "skipped": "emby_user_missing",
                    "local_ok": True, "remote_ok": False, "retryable": True,
                    "error": "Emby 账号不存在",
                    "errors": [{"target": user_id, "stage": "emby",
                                "error": "Emby 账号不存在", "retryable": True}]}
        if (emby_user.get("Policy") or {}).get("IsAdministrator"):
            return {"ok": True, "skipped": "administrator", "local_ok": True,
                    "remote_ok": None, "retryable": False, "error": "",
                    "errors": []}
        want = desired_policy(member)
        try:
            outcome = await self._emby.apply_member_policy(user_id, want)
            if outcome['status'] == 'skipped_admin':
                return {'ok': True, 'skipped': 'administrator', 'local_ok': True,
                        'remote_ok': None, 'retryable': False, 'error': '', 'errors': []}
            ok = outcome['status'] == 'applied'
        except Exception as exc:  # noqa: BLE001
            ok = False
            from app.modules.member_ops import redact
            err = redact(exc)
        else:
            err = "" if ok else "apply_member_policy failed"
        if ok:
            self._db.execute(
                "UPDATE members SET applied_fingerprint=?,applied_at=? "
                "WHERE emby_user_id=?",
                (fingerprint(want), int(time.time()), user_id))
        self._record_remote(user_id, "enforce", ok=ok, error=err)
        self._members.audit("system", "enforce.immediate", user_id,
                            reason or member.get("state", ""), ok=ok)
        return {
            "ok": bool(ok),
            "local_ok": True,
            "remote_ok": bool(ok),
            "retryable": not ok,
            "error": err,
            "errors": ([] if ok else [{"target": user_id, "stage": "enforce",
                                       "error": err, "retryable": True}]),
        }

    async def terminate_sessions(self, user_id: str, reason: str = "配额已用尽"
                                 ) -> int:
        """Stop this member's active playback.

        Disabling an account does not end sessions that already hold a stream,
        so a user who exhausts their quota would otherwise finish the film.
        The same path is used when a rate cap changes: the signed URL carries
        the old r= for up to six hours, so the client must be told to start
        again and pick up a freshly signed cap.
        """
        return await self.terminate_users({user_id}, reason)

    async def terminate_users(self, user_ids: set[str], reason: str = "", *,
                              strict: bool = False) -> int:
        """Stop every active session belonging to any of these members.

        One Emby session list, then stop matching rows. Calling terminate per
        member would re-fetch the whole fleet hundreds of times on a group
        rate change.
        """
        wanted = {str(uid) for uid in user_ids if uid}
        if not wanted:
            return 0
        try:
            sessions = await self._emby.active_sessions_raw()
        except Exception:  # noqa: BLE001
            if strict:
                raise RuntimeError('播放会话读取未确认') from None
            return 0
        stopped_by: dict[str, int] = {}
        failures_by: dict[str, int] = {}
        for session in sessions:
            uid = str(session.get("UserId") or "")
            if uid not in wanted:
                continue
            try:
                if await self._emby.stop_session(session.get("Id"), reason):
                    stopped_by[uid] = stopped_by.get(uid, 0) + 1
                elif strict:
                    failures_by[uid] = failures_by.get(uid, 0) + 1
            except Exception:  # noqa: BLE001
                failures_by[uid] = failures_by.get(uid, 0) + 1
        for uid, count in failures_by.items():
            self._members.audit(
                "system", "enforce.terminate_partial", uid,
                f"{count} session(s) could not be stopped", ok=False)
        for uid, count in stopped_by.items():
            self._members.audit(
                "system", "enforce.terminate", uid,
                f"{count} session(s): {reason}")
        if strict and failures_by:
            raise RuntimeError('部分播放会话终止未确认')
        return sum(stopped_by.values())


def _normalise(value: Any) -> Any:
    """Compare policy values the way Emby stores them.

    Emby returns lists in arbitrary order and bools sometimes as ints; without
    this every reconcile would report spurious differences and rewrite policies
    that are already correct.
    """
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if value is None:
        return None
    return str(value)
