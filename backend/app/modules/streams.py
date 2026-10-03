"""Fail-closed, session (not device) admission shared by playback entrances.

A lease belongs to an authenticated Emby Session Id. Pending starts and pauses
retain their seat while Emby reports playback (including pause). Unobserved
starts have a bounded grace period, checked against a fresh snapshot before
release. Already issued cloud URLs cannot be revoked by releasing a seat.
Billing continues to exclude pauses; lease deadlines survive Deck restarts.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any

from app.modules.usage import is_playing

PENDING_START_SECONDS = 120


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str
    # user_id is supplied only after caller authentication by the HTTP layer.
    user_id: str = ""
    session_id: str = ""
    device_id: str = ""
    cap: int = 0
    playing_count: int = 0
    paused_count: int = 0
    pending_count: int = 0
    punishable: bool = False
    snapshot_at: float = 0.0
    live_session_ids: tuple[str, ...] = ()

    @property
    def live_count(self) -> int:
        return self.playing_count + self.paused_count


def stream_key(session: dict[str, Any] | None = None, **_: Any) -> str:
    sid = str((session or {}).get("Id") or "")
    return "sid:" + sid if sid else ""


def playing_sessions(sessions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [s for s in (sessions or []) if isinstance(s, dict) and is_playing(s)]


def occupied_keys(sessions: list[dict[str, Any]], user_id: str) -> set[str]:
    # NowPlayingItem includes paused sessions. Idle login sessions do not count.
    return {stream_key(s) for s in sessions
            if s.get("NowPlayingItem") and str(s.get("UserId") or "") == user_id
            and stream_key(s)}


def overflow_session_ids(sessions: list[dict[str, Any]], user_id: str,
                         cap: int) -> list[str]:
    """Never infer start order from last activity or list order.

    Stop is only an advisory fallback when ALL actual start times are known.
    Most Emby versions do not expose them; admission, not polling, enforces cap.
    """
    rows = [s for s in playing_sessions(sessions) if str(s.get("UserId")) == user_id]
    if cap <= 0 or len(rows) <= cap or not all(s.get("PlayStartTime") for s in rows):
        return []
    ordered = sorted(rows, key=lambda s: str(s["PlayStartTime"]))
    if len({s["PlayStartTime"] for s in ordered}) != len(ordered):
        return []
    return [str(s["Id"]) for s in ordered[cap:] if s.get("Id")]


def matching_sessions(rows: list[dict[str, Any]], uid: str, device: str = "",
                      sid: str = "", profile: dict[str, str] | None = None,
                      ) -> list[dict[str, Any]]:
    """Resolve one session without merging same-device playback identities.

    An explicit SessionId remains authoritative.  Without one, DeviceId is the
    primary key.  If Emby has duplicate sessions for that device, exact
    non-secret client-profile fields may narrow them to one.  Zero or multiple
    exact matches remain unresolved: never guess, pick by list order, or merge
    two sessions into one playback seat.
    """
    matches = [s for s in rows if str(s.get("UserId")) == uid
               and (not device or str(s.get("DeviceId")) == device)
               and (not sid or str(s.get("Id")) == sid)]
    if sid or len(matches) <= 1:
        return matches
    hints = {key: str(value).strip().casefold()
             for key, value in (profile or {}).items()
             if key in {"Client", "ApplicationVersion", "DeviceName"}
             and str(value).strip()}
    if not hints:
        return matches
    return [session for session in matches
            if all(str(session.get(key) or "").strip().casefold() == expected
                   for key, expected in hints.items())]


class StreamAdmission:
    """Single Deck worker; serialized fresh snapshots and durable SQLite leases.

    A pending lease ends on a successful client Stopped report or disappearance
    of the Emby session. An observed play also ends when Emby reports it idle.
    Start/progress acknowledgements observe the bound play, even when no later
    admission happens during playback. Abandoned idle starts expire after two
    minutes; an unavailable snapshot never counts as proof of idleness.
    """

    def __init__(self, members: Any, emby: Any) -> None:
        self._members = members
        self._emby = emby
        self._db = members._db
        self._lock = asyncio.Lock()
        self._db.execute("CREATE TABLE IF NOT EXISTS stream_leases ("
                         "user_id TEXT NOT NULL, session_id TEXT NOT NULL, "
                         "observed INTEGER NOT NULL DEFAULT 0, "
                         "PRIMARY KEY(user_id, session_id))")
        self._db._ensure_column("stream_leases", "play_id", "TEXT NOT NULL DEFAULT ''")
        self._db._ensure_column("stream_leases", "pending_at", "REAL NOT NULL DEFAULT 0")
        # A replacement must not be observed by its predecessor's snapshot.
        # Persist this guard so a Deck restart cannot reintroduce that race.
        self._db._ensure_column("stream_leases", "snapshot_guard", "INTEGER NOT NULL DEFAULT 0")
        # Legacy rows have no trustworthy issuance time. Give them one grace
        # period, not an immediate wipe; never reset deadlines on a restart.
        self._db.execute("UPDATE stream_leases SET pending_at=? WHERE pending_at=0",
                         (time.time(),))

    @staticmethod
    def _validate_snapshot(rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list) or any(not isinstance(s, dict) for s in rows):
            raise ValueError("invalid session list")
        seen: set[str] = set()
        for session in rows:
            sid = str(session.get("Id") or "")
            if sid and sid in seen:
                raise ValueError("duplicate session identity")
            if sid:
                seen.add(sid)
            item = session.get("NowPlayingItem")
            if item:
                if not sid or not session.get("UserId"):
                    raise ValueError("playing session has no identity")
                if not isinstance(item, dict) or not item.get("Id"):
                    raise ValueError("invalid playing item")
                state = session.get("PlayState")
                if state is not None and not isinstance(state, dict):
                    raise ValueError("invalid play state")
                if state and "IsPaused" in state and not isinstance(state["IsPaused"], bool):
                    raise ValueError("invalid pause state")
        return rows

    async def _snapshot(self) -> list[dict[str, Any]]:
        return self._validate_snapshot(await self._emby.active_sessions_raw())

    @staticmethod
    def _is_admin(member: dict[str, Any]) -> bool:
        return "admin" in (member.get("roles") or [])

    async def _native_admin(self, uid: str) -> bool | None:
        """Fresh trusted user policy; None means no safe punishment proof."""
        try:
            users = await self._emby.list_users()
            if not isinstance(users, list) or any(not isinstance(user, dict) for user in users):
                return None
            matches = [user for user in users if str(user.get("Id") or "") == uid]
            if len(matches) != 1 or not isinstance(matches[0].get("Policy"), dict):
                return None
            flag = matches[0]["Policy"].get("IsAdministrator", False)
            return flag if isinstance(flag, bool) else None
        except Exception:  # noqa: BLE001 - unknown policy is not a license to punish
            return None

    async def _native_admin_exemption(self, admission: Admission) -> Admission:
        # Do not read all users for ordinary grants/no-member/unlimited paths,
        # unresolved identity or device blocks. Only concurrency is exempted.
        if admission.allowed or admission.reason != "over-limit":
            return admission
        admin = await self._native_admin(admission.user_id)
        if admin is True:
            return replace(admission, allowed=True, reason="admin-exempt", punishable=False)
        if admin is None:
            return replace(admission, punishable=False)
        return admission

    @staticmethod
    def _snapshot_play_id(session: dict[str, Any]) -> str:
        return str(session.get("PlaySessionId")
                   or (session.get("PlayState") or {}).get("PlaySessionId") or "")

    async def inspect(self, user_id: str | None, device_id: str = "",
                      token: str = "", session_id: str = "", play_id: str = "",
                      session_profile: dict[str, str] | None = None) -> Admission:
        async with self._lock:
            try:
                sessions = await self._snapshot()
            except Exception:  # noqa: BLE001 - unavailable is never an empty snapshot
                return Admission(False, "sessions-unavailable")
            result = self._admit(user_id, device_id, session_id, sessions, play_id,
                                 session_profile)
            return await self._native_admin_exemption(result)

    async def admit(self, *, user_id: str | None, device_id: str = "",
                    token: str = "", session_id: str = "",
                    sessions: list[dict[str, Any]] | None = None,
                    session_profile: dict[str, str] | None = None) -> Admission:
        """Explicit snapshot seam for deterministic tests; HTTP uses inspect."""
        async with self._lock:
            try:
                sessions = self._validate_snapshot(sessions)
            except ValueError:
                return Admission(False, "sessions-unavailable")
            return self._admit(user_id, device_id, session_id, sessions,
                               session_profile=session_profile)

    def _admit(self, user_id: str | None, device: str, session_id: str,
               sessions: list[dict[str, Any]], play_id: str = "",
               session_profile: dict[str, str] | None = None) -> Admission:
        uid = str(user_id or "").strip()
        if not uid:
            return Admission(False, "unresolved")
        member = self._members.get(uid)
        if not member:
            return Admission(True, "no-member", user_id=uid)
        cap = int(member.get("max_streams") or 0)
        rows = [s for s in sessions if str(s.get("UserId") or "") == uid and s.get("Id")]
        by_sid = {str(s["Id"]): s for s in rows}
        live = {key for key, row in by_sid.items() if row.get("NowPlayingItem")}
        paused = {key for key in live if (by_sid[key].get("PlayState") or {}).get("IsPaused")}
        now = time.time()
        facts = {"user_id": uid, "cap": cap, "playing_count": len(live - paused),
                 "paused_count": len(paused), "snapshot_at": now,
                 "live_session_ids": tuple(sorted(live))}
        if device and self._members.device_blocked(uid, device):
            return Admission(False, "device-blocked", **facts)
        if self._is_admin(member):
            return Admission(True, "admin-exempt", **facts)
        if cap <= 0:
            return Admission(True, "unlimited", **facts)
        bound_sid = ""
        if play_id:
            bound = self._db.query("SELECT session_id FROM stream_leases "
                                   "WHERE user_id=? AND play_id=?", (uid, play_id))
            if len(bound) == 1:
                bound_sid = str(bound[0]["session_id"])
                if session_id and session_id != bound_sid:
                    return Admission(False, "session-unresolved", **facts)
                session_id = bound_sid
        # SessionId alone is a caller assertion, not evidence of its device.
        # HLS may omit DeviceId only with an existing user-scoped play binding.
        if not device and not bound_sid:
            return Admission(False, "session-unresolved", **facts)
        matches = matching_sessions(rows, uid, device, session_id, session_profile)
        if len(matches) != 1:
            return Admission(False, "session-unresolved", **facts)
        sid = str(matches[0]["Id"])
        actual_device = str(matches[0].get("DeviceId") or "")
        facts.update(session_id=sid, device_id=actual_device)
        if actual_device and self._members.device_blocked(uid, actual_device):
            return Admission(False, "device-blocked", **facts)
        with self._db.write() as conn:
            leases = conn.execute("SELECT session_id,observed,play_id,pending_at,snapshot_guard "
                                  "FROM stream_leases WHERE user_id=?", (uid,)).fetchall()
            for lease in leases:
                key, observed = lease[0], lease[1]
                pending_expired = not observed and now - lease[3] >= PENDING_START_SECONDS
                if key not in by_sid or (key not in live and (observed or pending_expired)):
                    conn.execute("DELETE FROM stream_leases WHERE user_id=? AND session_id=?", (uid, key))
            held = {r[0] for r in conn.execute(
                "SELECT session_id FROM stream_leases WHERE user_id=?", (uid,)).fetchall()}
            prior = {r[0]: r for r in leases}
            for key in live:
                if key in held:
                    lease = prior[key]
                    # An old/unidentified NowPlayingItem cannot observe a new
                    # replacement. Current-play reports or an exact snapshot
                    # PlaySessionId remove the durable replacement guard.
                    if not lease[4] or (lease[2] and self._snapshot_play_id(by_sid[key]) == lease[2]):
                        conn.execute("UPDATE stream_leases SET observed=1,snapshot_guard=0 "
                                     "WHERE user_id=? AND session_id=?", (uid, key))
                elif len(live | held) <= cap:
                    conn.execute("INSERT INTO stream_leases (user_id,session_id,observed,pending_at) "
                                 "VALUES (?,?,1,?)", (uid, key, now))
            occupied = {r[0] for r in conn.execute(
                "SELECT session_id FROM stream_leases WHERE user_id=?", (uid,)).fetchall()}
            facts["pending_count"] = len(occupied - live)
            if sid in occupied:
                return Admission(True, "same-play", **facts)
            if len(occupied | live) >= cap:
                # Only an idle, unadmitted newcomer against a fully known real
                # baseline is actionable. Pending seats and cold over-cap
                # snapshots refuse URLs but never constitute punishment proof.
                punishable = bool(device and actual_device and sid not in live
                                  and len(live) == cap and live <= occupied)
                return Admission(False, "over-limit", punishable=punishable, **facts)
            conn.execute("INSERT INTO stream_leases (user_id,session_id,observed,pending_at) VALUES (?,?,0,?)",
                         (uid, sid, now))
            facts["pending_count"] += 1
            return Admission(True, "granted", **facts)

    async def confirm_violation(self, admission: Admission) -> dict[str, Any] | None:
        """Fresh, non-mutating proof for the caller's post-admission action.

        Never create a seat, execute a Stop, or send a notification here. The
        action owner must deduplicate its incident and target only session_id.
        A dependency/identity change or an original occupant's exit cancels it.
        """
        if (admission.allowed or admission.reason != "over-limit" or not admission.punishable
                or not admission.user_id or not admission.session_id or not admission.device_id
                or admission.cap <= 0 or len(admission.live_session_ids) != admission.cap):
            return None
        async with self._lock:
            try:
                uid, sid = admission.user_id, admission.session_id
                member = self._members.get(uid)
                if (not member or self._is_admin(member)
                        or int(member.get("max_streams") or 0) != admission.cap
                        or self._members.device_blocked(uid, admission.device_id)):
                    return None
                if await self._native_admin(uid) is not False:
                    return None
                # Fetch playback after the user-policy network read: an exit
                # during that read must cancel rather than reuse an old snapshot.
                rows = await self._snapshot()
                member = self._members.get(uid)
                if (not member or self._is_admin(member)
                        or int(member.get("max_streams") or 0) != admission.cap
                        or self._members.device_blocked(uid, admission.device_id)):
                    return None
                matches = self._matching(rows, uid, admission.device_id, sid)
                if len(matches) != 1:
                    return None
                # It may have legitimately acquired a seat after the rejection.
                if self._db.one("SELECT session_id FROM stream_leases WHERE user_id=? AND session_id=?", (uid, sid)):
                    return None
                own = {str(s["Id"]): s for s in rows if str(s.get("UserId") or "") == uid and s.get("Id")}
                live = {key for key, row in own.items() if row.get("NowPlayingItem")}
                others = live - {sid}
                if len(others) < admission.cap or not set(admission.live_session_ids) <= others:
                    return None
                paused = {key for key in live if (own[key].get("PlayState") or {}).get("IsPaused")}
                now = time.time()
                leases = self._db.query("SELECT session_id,observed,pending_at FROM stream_leases WHERE user_id=?", (uid,))
                pending = sum(1 for lease in leases if lease["session_id"] in own
                              and lease["session_id"] not in live and not lease["observed"]
                              and now - lease["pending_at"] < PENDING_START_SECONDS)
                return {"user_id": uid, "session_id": sid, "device_id": admission.device_id,
                        "playing": bool(matches[0].get("NowPlayingItem")),
                        "reason": "over-limit", "punishable": True, "cap": admission.cap,
                        "playing_count": len(live - paused), "paused_count": len(paused),
                        "live_count": len(live), "other_live_count": len(others), "pending_count": pending,
                        "live_session_ids": sorted(live), "snapshot_at": now}
            except Exception:  # noqa: BLE001 - uncertainty never authorizes punishment
                return None

    def _matching(self, rows: list[dict[str, Any]], uid: str, device: str,
                  sid: str = "", profile: dict[str, str] | None = None,
                  ) -> list[dict[str, Any]]:
        return matching_sessions(rows, uid, device, sid, profile)

    async def issue_info(self, uid: str, device: str, session_id: str,
                         issue: Any,
                         session_profile: dict[str, str] | None = None,
                         ) -> tuple[Admission, int, dict[str, Any]]:
        """Keep admission + upstream issuance + PlaySessionId binding atomic.

        The metadata proxy is necessary: nginx auth_request cannot see the
        PlaySessionId in PlaybackInfo's response. Without binding it a delayed
        Stopped report from an old play could release the replacement's seat.
        """
        async with self._lock:
            rows = await self._snapshot()
            result = self._admit(uid, device, session_id, rows,
                                 session_profile=session_profile)
            result = await self._native_admin_exemption(result)
            if not result.allowed:
                return result, 403, {}
            matches = self._matching(rows, uid, device, session_id, session_profile)
            def release_failed_start():
                # Only this request's newly acquired seat; never release a
                # previous play when replacement metadata fails.
                if result.reason == "granted" and len(matches) == 1:
                    self._db.execute("DELETE FROM stream_leases WHERE user_id=? AND session_id=?",
                                     (uid, str(matches[0]["Id"])))
            try:
                code, data = await issue()
            except BaseException:
                release_failed_start()
                raise
            if not 200 <= code < 300:
                release_failed_start()
            if 200 <= code < 300 and data.get("PlaySessionId") and len(matches) == 1:
                # The replacement play has not been observed yet. Do not let
                # the previous play's observed flag release this pending start
                # when an old Stopped report makes the session briefly idle.
                sid = str(matches[0]["Id"])
                previous = self._db.one("SELECT play_id FROM stream_leases WHERE user_id=? AND session_id=?", (uid, sid))
                replacement = bool(previous and (previous["play_id"] or matches[0].get("NowPlayingItem")))
                self._db.execute("UPDATE stream_leases SET "
                                 "observed=CASE WHEN play_id=? THEN observed ELSE 0 END, "
                                 "snapshot_guard=CASE WHEN play_id=? THEN snapshot_guard ELSE ? END, "
                                 "pending_at=CASE WHEN play_id=? THEN pending_at ELSE ? END, play_id=? "
                                 "WHERE user_id=? AND session_id=?",
                                 (str(data["PlaySessionId"]), str(data["PlaySessionId"]), int(replacement),
                                  str(data["PlaySessionId"]), time.time(), str(data["PlaySessionId"]), uid, sid))
            return result, code, data

    async def report_activity(self, uid: str, payload: dict[str, Any], issue: Any) -> int:
        """Observe only an existing bound play AFTER Emby accepts its event.

        Do not hold the admission lock during progress network I/O. The SQL
        compare against the current play_id makes late events harmless, and
        progress cannot create seats or renew abandoned-start deadlines.
        """
        code = await issue()
        play_id = str(payload.get("PlaySessionId") or "")
        if not 200 <= code < 300 or not play_id:
            return code
        async with self._lock:
            rows = self._db.query("SELECT session_id FROM stream_leases "
                                  "WHERE user_id=? AND play_id=?", (uid, play_id))
            if len(rows) == 1:
                sid = str(rows[0]["session_id"])
                if payload.get("SessionId") and str(payload["SessionId"]) != sid:
                    return code
                self._db.execute("UPDATE stream_leases SET observed=1,snapshot_guard=0 "
                                 "WHERE user_id=? AND session_id=? AND play_id=?",
                                 (uid, sid, play_id))
        return code

    async def report_stopped(self, uid: str, device: str, token: str,
                             payload: dict[str, Any]) -> int:
        async with self._lock:
            rows = await self._snapshot()
            sid = str(payload.get("SessionId") or "")
            play_id = str(payload.get("PlaySessionId") or "")
            if play_id and not sid:
                bound = self._db.query("SELECT session_id FROM stream_leases WHERE user_id=? AND play_id=?", (uid, play_id))
                if len(bound) == 1:
                    sid = str(bound[0]["session_id"])
            matches = self._matching(rows, uid, device, sid)
            code = await self._emby.report_stopped(token, device, payload)
            if 200 <= code < 300 and len(matches) == 1:
                sid = str(matches[0]["Id"])
                lease = self._db.one("SELECT play_id FROM stream_leases "
                                     "WHERE user_id=? AND session_id=?", (uid, sid))
                # A delayed old Stop must not release a newer PlaybackInfo lease.
                if lease and str(lease["play_id"]) == str(payload.get("PlaySessionId") or ""):
                    self._db.execute("DELETE FROM stream_leases WHERE user_id=? AND session_id=?",
                                     (uid, sid))
            return code
