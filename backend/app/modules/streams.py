"""Fail-closed, session (not device) admission shared by playback entrances.

A lease belongs to an authenticated Emby Session Id. Pending starts and pauses
retain their seat: releasing a paused 302 client would let its normal resume
compete with a newly admitted client without requesting another URL. Billing
continues to exclude pauses. Leases survive Deck restarts and have no blind TTL.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.modules.usage import is_playing


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str


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


class StreamAdmission:
    """Single Deck worker; serialized fresh snapshots and durable SQLite leases.

    A pending lease ends on a successful client Stopped report or disappearance
    of the Emby session. An observed play also ends when Emby reports it idle.
    An abandoned client that remains logged in without reporting Stopped keeps
    its seat rather than silently allowing an unobserved late start over cap.
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

    async def _snapshot(self) -> list[dict[str, Any]]:
        rows = await self._emby.active_sessions_raw()
        if not isinstance(rows, list) or any(not isinstance(s, dict) for s in rows):
            raise ValueError("invalid session list")
        if any(s.get("NowPlayingItem") and (not s.get("Id") or not s.get("UserId")) for s in rows):
            raise ValueError("playing session has no identity")
        return rows

    async def inspect(self, user_id: str | None, device_id: str = "",
                      token: str = "", session_id: str = "", play_id: str = "") -> Admission:
        async with self._lock:
            try:
                sessions = await self._snapshot()
            except Exception:  # noqa: BLE001 - unavailable is never an empty snapshot
                return Admission(False, "sessions-unavailable")
            return self._admit(user_id, device_id, session_id, sessions, play_id)

    async def admit(self, *, user_id: str | None, device_id: str = "",
                    token: str = "", session_id: str = "",
                    sessions: list[dict[str, Any]] | None = None) -> Admission:
        """Explicit snapshot seam for deterministic tests; HTTP uses inspect."""
        async with self._lock:
            if sessions is None:
                return Admission(False, "sessions-unavailable")
            return self._admit(user_id, device_id, session_id, sessions)

    def _admit(self, user_id: str | None, device: str, session_id: str,
               sessions: list[dict[str, Any]], play_id: str = "") -> Admission:
        uid = str(user_id or "").strip()
        if not uid:
            return Admission(False, "unresolved")
        member = self._members.get(uid)
        if not member:
            return Admission(True, "no-member")
        if device and self._members.device_blocked(uid, device):
            return Admission(False, "device-blocked")
        cap = int(member.get("max_streams") or 0)
        if cap <= 0:
            return Admission(True, "unlimited")
        rows = [s for s in sessions if str(s.get("UserId") or "") == uid and s.get("Id")]
        if play_id and not session_id:
            bound = self._db.query("SELECT session_id FROM stream_leases "
                                   "WHERE user_id=? AND play_id=?", (uid, play_id))
            if len(bound) == 1:
                session_id = bound[0]["session_id"]
        matches = [s for s in rows if (not device or str(s.get("DeviceId")) == device)
                   and (not session_id or str(s["Id"]) == session_id)]
        if len(matches) != 1:
            return Admission(False, "session-unresolved")
        sid = str(matches[0]["Id"])
        actual_device = str(matches[0].get("DeviceId") or "")
        if actual_device and self._members.device_blocked(uid, actual_device):
            return Admission(False, "device-blocked")
        current = {str(s["Id"]): bool(s.get("NowPlayingItem")) for s in rows}
        with self._db.write() as conn:
            leases = conn.execute("SELECT session_id,observed,play_id FROM stream_leases WHERE user_id=?",
                                  (uid,)).fetchall()
            for lease in leases:
                key, observed = lease[0], lease[1]
                if key not in current or (observed and not current[key] and not lease[2]):
                    conn.execute("DELETE FROM stream_leases WHERE user_id=? AND session_id=?",
                                 (uid, key))
            held = {r[0] for r in conn.execute(
                "SELECT session_id FROM stream_leases WHERE user_id=?", (uid,)).fetchall()}
            live = {key for key, active in current.items() if active}
            for key, active in current.items():
                if active and (key in held or len(live | held) <= cap):
                    conn.execute("INSERT INTO stream_leases (user_id,session_id,observed) VALUES (?,?,1) "
                                 "ON CONFLICT(user_id,session_id) DO UPDATE SET observed=1",
                                 (uid, key))
            occupied = {r[0] for r in conn.execute(
                "SELECT session_id FROM stream_leases WHERE user_id=?", (uid,)).fetchall()}
            if sid in occupied:
                return Admission(True, "same-play")
            if len(occupied | live) >= cap:
                return Admission(False, "over-limit")
            conn.execute("INSERT INTO stream_leases (user_id,session_id,observed) VALUES (?,?,0)",
                         (uid, sid))
            return Admission(True, "granted")

    def _matching(self, rows: list[dict[str, Any]], uid: str, device: str,
                  sid: str = "") -> list[dict[str, Any]]:
        return [s for s in rows if str(s.get("UserId")) == uid
                and (not device or str(s.get("DeviceId")) == device)
                and (not sid or str(s.get("Id")) == sid)]

    async def issue_info(self, uid: str, device: str, session_id: str,
                         issue: Any) -> tuple[Admission, int, dict[str, Any]]:
        """Keep admission + upstream issuance + PlaySessionId binding atomic.

        The metadata proxy is necessary: nginx auth_request cannot see the
        PlaySessionId in PlaybackInfo's response. Without binding it a delayed
        Stopped report from an old play could release the replacement's seat.
        """
        async with self._lock:
            rows = await self._snapshot()
            result = self._admit(uid, device, session_id, rows)
            if not result.allowed:
                return result, 403, {}
            code, data = await issue()
            matches = self._matching(rows, uid, device, session_id)
            if 200 <= code < 300 and data.get("PlaySessionId") and len(matches) == 1:
                self._db.execute("UPDATE stream_leases SET play_id=? "
                                 "WHERE user_id=? AND session_id=?",
                                 (str(data["PlaySessionId"]), uid, str(matches[0]["Id"])))
            return result, code, data

    async def report_stopped(self, uid: str, device: str, token: str,
                             payload: dict[str, Any]) -> int:
        async with self._lock:
            rows = await self._snapshot()
            matches = self._matching(rows, uid, device, str(payload.get("SessionId") or ""))
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
