"""Usage sampling — turn live Emby sessions into billable traffic.

How traffic is measured, and why this way
-----------------------------------------
Emby does not report bytes served per user, so consumption has to be inferred.
Three approaches were possible:

1. **Position delta x bitrate.**  Wrong: seeking forward advances position
   without transferring the skipped bytes, so a user who skips an intro gets
   billed for it.
2. **Node access logs.**  Most accurate, but the signed URLs handed to clients
   deliberately carry no user identity, and adding one would leak who is
   watching what into every node's log file.
3. **Wall-clock playing time x bitrate.**  What this uses.  While a session is
   actually playing, bytes leave the server at roughly the media bitrate, and
   pausing stops the transfer once the client buffer fills.

So: sample every N seconds, and for each session that is playing, bill
`bitrate/8 x seconds_since_last_sample`.

The accuracy caveats are deliberate and bounded:

* A paused session still fills its buffer for a few seconds; undercounting
  there is preferred over charging a user for time they did not watch.
* Client-side buffering means bytes can be pulled ahead of playback; over a
  whole session this averages out.
* A restart makes the gap since the last sample unbounded, so deltas are
  clamped: a panel outage must never produce a surprise bill.

Everything here is idempotent per sample tick and safe to run concurrently with
readers, because writes go through the shared SQLite connection lock.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import time
import uuid
from datetime import UTC, datetime
from functools import partial
from typing import Any

from app.core.db import Database
from app.modules.members import MemberService

# Longest gap that may be billed in one sample. Anything larger means the
# sampler was not running (deploy, crash, host reboot), and charging for that
# window would invent traffic the user never used.
MAX_BILLABLE_GAP_SECONDS = 120

# A session must be seen playing at least this long before it counts as a play
# event, so a mis-tap that starts and stops does not pollute "top titles".
MIN_PLAY_SECONDS = 20

TICKS_PER_SECOND = 10_000_000


def day_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), UTC).strftime("%Y-%m-%d")


def session_bitrate(session: dict[str, Any]) -> int:
    """Bits per second for this session.

    Transcoding bitrate wins when present: that is what actually leaves the
    server, and it is usually lower than the source. Falling back to the source
    bitrate keeps direct play accurate.
    """
    transcoding = session.get("TranscodingInfo") or {}
    item = session.get("NowPlayingItem") or {}
    for value in (transcoding.get("Bitrate"), item.get("Bitrate")):
        try:
            rate = int(value or 0)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            return rate
    # Nothing reported: assume a conservative 4 Mbps rather than zero, so an
    # unreported stream is not silently free.
    return 4_000_000


def is_playing(session: dict[str, Any]) -> bool:
    if not session.get("NowPlayingItem"):
        return False
    return not bool((session.get("PlayState") or {}).get("IsPaused"))


async def run_usage_io(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a usage/housekeeping DB step without abandoning it on shutdown."""
    worker = asyncio.get_running_loop().run_in_executor(
        None, partial(contextvars.copy_context().run, fn, *args, **kwargs))
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as exc:
            # SQLite cannot cancel a blocking call. Join the writer before
            # releasing the tick lock or letting shutdown close the database.
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    return result


class UsageSampler:
    """Stateful sampler. One instance, ticked on a timer."""

    def __init__(self, db: Database, members: MemberService, emby: Any,
                 enforcement: Any = None, sharing: Any = None) -> None:
        self._db = db
        self._members = members
        self._emby = emby
        self._enforcement = enforcement
        # Optional: the sampler already holds the only live view of who is
        # playing from where, so sharing detection rides along rather than
        # polling Emby a second time for the same answer.
        self._sharing = sharing
        # Restore sampled time, but never charge the interval while this process was down.
        self._live: dict[str, dict[str, Any]] = {}
        for row in db.query('SELECT session_id,state_json FROM watch_checkpoints'):
            value = json.loads(row['state_json'])
            value['last_ts'] = None
            value['was_playing'] = False
            value['speed_bps'] = 0
            self._live[row['session_id']] = value
        self._last_tick = 0.0
        self._last_error: str | None = None
        self._ticks = 0
        # Device-cap refusals collected during a tick, kicked after billing so
        # one over-limit client cannot abort accounting for everyone else.
        self._pending_kicks: list[tuple[str, str]] = []
        self._tick_lock = asyncio.Lock()
        self._publish_live()

    # -- main loop -----------------------------------------------------------
    async def tick(self, node_of: Any = None) -> dict[str, Any]:
        # One tick owns session state until its worker commits, even if its
        # caller is cancelled. A second tick must not overlap that write.
        async with self._tick_lock:
            return await self._tick(node_of)

    async def _tick(self, node_of: Any) -> dict[str, Any]:
        now = time.time()
        try:
            sessions = await self._emby.active_sessions_raw()
        except Exception as exc:  # noqa: BLE001 - a flaky Emby must not kill the loop
            self._last_error = str(exc)[:200]
            return {"ok": False, "error": self._last_error}

        result, billed_users = await run_usage_io(self._sample, sessions, now, node_of)
        if billed_users and self._enforcement:
            result["enforced"] = await self._enforce_exhausted(billed_users)
        if self._pending_kicks and self._enforcement:
            result["device_kicks"] = await self._kick_refused_devices()
        return result

    def _sample(self, sessions: list[dict[str, Any]], now: float,
                node_of: Any) -> tuple[dict[str, Any], list[str]]:
        """Existing ordered persistence, off the ASGI/event-loop thread."""
        self._ticks += 1
        self._last_error = None
        seen: set[str] = set()
        billed_bytes = 0
        billed_users: dict[str, int] = {}
        self._pending_kicks = []

        for session in sessions:
            sid = str(session.get("Id") or "")
            if not sid:
                continue
            user_id = str(session.get("UserId") or "")
            if not user_id:
                continue
            seen.add(sid)

            self._track_device(session, user_id, now)

            state = self._live.get(sid)
            playing = is_playing(session)
            item = session.get("NowPlayingItem") or {}

            if state is None:
                # New session: start the clock but bill nothing yet. Billing a
                # full interval on first sight would charge for playback that
                # started an instant ago.
                self._live[sid] = {
                    "user_id": user_id,
                    "username": session.get("UserName") or "",
                    "item_id": str(item.get("Id") or ""),
                    "item_name": item.get("Name") or "",
                    "item_type": item.get("Type") or "",
                    "series_name": item.get("SeriesName") or "",
                    "device_id": session.get("DeviceId") or "",
                    "client": session.get("Client") or "",
                    "play_method": (session.get("PlayState") or {}).get("PlayMethod") or "",
                    "remote_ip": session.get("RemoteEndPoint") or "",
                    "started_at": int(now),
                    "watch_key": uuid.uuid4().hex,
                    "sampled": True,
                    "last_ts": now,
                    "was_playing": playing,
                    "seconds": 0.0,
                    "bytes": 0,
                    "transcoded": bool(session.get("TranscodingInfo")),
                }
                continue

            # A session that switched title is two plays, not one.
            current_item = str(item.get("Id") or "")
            if user_id != state['user_id'] or current_item != state['item_id']:
                self._finish(sid, state, now)
                self._live[sid] = {
                    **state,
                    "user_id": user_id,
                    "username": session.get('UserName') or '',
                    "remote_ip": session.get("RemoteEndPoint") or "",
                    "device_id": session.get("DeviceId") or "",
                    "client": session.get("Client") or "",
                    "play_method": (session.get("PlayState") or {}).get("PlayMethod") or "",
                    "node": "",
                    "transcoded": bool(session.get('TranscodingInfo')),
                    "speed_bps": 0,
                    "item_id": current_item,
                    "item_name": item.get("Name") or "",
                    "item_type": item.get("Type") or "",
                    "series_name": item.get("SeriesName") or "",
                    "started_at": int(now),
                    "watch_key": uuid.uuid4().hex,
                    "sampled": True,
                    "last_ts": now,
                    "was_playing": playing,
                    "seconds": 0.0,
                    "bytes": 0,
                }
                continue

            # Session endpoints/client metadata can change without a new Id.
            # Sharing observes this tick's network, never a stale first address.
            state.update({
                "remote_ip": session.get("RemoteEndPoint") or "",
                "device_id": session.get("DeviceId") or "",
                "client": session.get("Client") or "",
                "play_method": (session.get("PlayState") or {}).get("PlayMethod") or "",
            })
            delta = now - float(state['last_ts']) if state.get('last_ts') is not None else 0
            was_playing = state.get('was_playing', False)
            state["last_ts"] = now
            state['was_playing'] = playing
            if not playing:
                state["speed_bps"] = 0
                continue
            if delta <= 0:
                continue
            # Clamp: a long gap means the sampler was down, not that the user
            # watched continuously through it.
            watch_delta = delta if was_playing and delta <= MAX_BILLABLE_GAP_SECONDS else 0
            delta = min(delta, MAX_BILLABLE_GAP_SECONDS)

            rate = session_bitrate(session)
            chunk = int(rate / 8 * delta)
            # Sample and resumable state commit together: a crash cannot leave
            # totals newer than the corresponding session history.
            updated = {**state, 'seconds': float(state['seconds']) + watch_delta,
                       'bytes': int(state['bytes']) + chunk}
            self._record_watch(sid, updated, now - watch_delta, now)
            state.update(updated)
            # Bytes/second over the last sampled window: what the dashboard
            # shows as the session's live bandwidth.
            state["speed_bps"] = int(chunk / delta) if delta else 0
            if session.get("TranscodingInfo"):
                state["transcoded"] = True
            if node_of:
                state["node"] = node_of(state.get("item_id")) or state.get("node", "")

            billed_bytes += chunk
            billed_users[user_id] = billed_users.get(user_id, 0) + chunk

        # Sessions that vanished have ended.
        for sid in [s for s in self._live if s not in seen]:
            self._finish(sid, self._live[sid], now)
            self._live.pop(sid)

        if billed_users:
            self._commit(billed_users, now)

        sharing_found = 0
        if self._sharing is not None:
            # Only sessions actually playing count: an idle client parked on a
            # second network is not a second viewer. Detection must never be
            # able to abort a billing tick, so failures are swallowed here.
            with contextlib.suppress(Exception):
                findings = self._sharing.observe(
                    [s for s in self._live.values() if s.get("was_playing")], now)
                for finding in findings:
                    member = self._members.get(finding["user_id"])
                    self._sharing.record(
                        finding, str((member or {}).get("username") or ""))
                sharing_found = len(findings)

        with self._db.write() as conn:
            for sid, checkpoint in self._live.items():
                if checkpoint.get('item_id'):
                    conn.execute('INSERT INTO watch_checkpoints VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json', (sid, json.dumps(checkpoint)))
        self._last_tick = now
        result = {
            "ok": True,
            "sessions": len(seen),
            "playing": sum(1 for s in self._live.values() if s.get("was_playing")),
            "billed_bytes": billed_bytes,
            "users": len(billed_users),
        }
        if self._sharing is not None:
            result["sharing_findings"] = sharing_found
        self._publish_live()
        # Enforcement stays after committed accounting, in the async caller.
        return result, list(billed_users)

    # -- persistence ---------------------------------------------------------
    def _record_watch(self, sid: str, state: dict, start: float, end: float) -> None:
        with self._db.write() as conn:
            key = state.setdefault('watch_key', uuid.uuid4().hex)
            if end > start:
                added = conn.execute(
                    'INSERT OR IGNORE INTO watch_samples VALUES(?,?,?,?,?)',
                    (key, state['user_id'], start, end, end-start)).rowcount
                if added:
                    conn.execute(
                        'INSERT INTO watch_sample_totals VALUES(?,?,?,?) '
                        'ON CONFLICT(emby_user_id) DO UPDATE SET seconds=seconds+excluded.seconds,'
                        'first_at=MIN(first_at,excluded.first_at),last_at=MAX(last_at,excluded.last_at)',
                        (state['user_id'], end-start, start, end))
            conn.execute('INSERT INTO watch_checkpoints VALUES(?,?) '
                         'ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json',
                         (sid, json.dumps(state)))

    def _commit(self, per_user: dict[str, int], now: float) -> None:
        day = day_key(now)
        with self._db.write() as conn:
            for user_id, chunk in per_user.items():
                conn.execute(
                    "INSERT INTO usage_daily (day,emby_user_id,bytes,seconds,plays,"
                    "transcodes) VALUES (?,?,?,0,0,0) "
                    "ON CONFLICT(day,emby_user_id) DO UPDATE SET bytes=bytes+?",
                    (day, user_id, chunk, chunk))
                # Only members are metered; non-members are still recorded in
                # usage_daily for statistics, but never billed.
                conn.execute(
                    "UPDATE members SET traffic_used_bytes=traffic_used_bytes+?,"
                    "last_seen_at=? WHERE emby_user_id=?",
                    (chunk, int(now), user_id))

    def _publish_live(self) -> None:
        # Readers must never iterate the worker's mutable session dictionary
        # or wait on a mutex held during slow disk I/O.
        self._watch_view = tuple(
            {k: state.get(k) for k in ('user_id', 'username', 'started_at', 'seconds', 'sampled')}
            for state in self._live.values())
        self._speed_view = {sid: int(s.get("speed_bps") or 0) for sid, s in self._live.items()}

    def live_watch(self) -> list[dict[str, Any]]:
        """Already committed sampled time; querying does not advance clocks."""
        return [dict(row) for row in self._watch_view]

    def live_speeds(self) -> dict[str, int]:
        """session id -> bytes/second over the last committed sample window."""
        return dict(self._speed_view)

    def _finish(self, sid: str, state: dict[str, Any], now: float) -> None:
        seconds = int(state.get("seconds") or 0)
        if seconds < MIN_PLAY_SECONDS:
            self._db.execute('DELETE FROM watch_checkpoints WHERE session_id=?', (sid,))
            return
        with self._db.write() as conn:
            conn.execute('DELETE FROM watch_checkpoints WHERE session_id=?', (sid,))
            conn.execute(
                "INSERT INTO play_events (emby_user_id,username,item_id,item_name,"
                "item_type,series_name,device_id,client,play_method,node,remote_ip,"
                "bytes,seconds,started_at,ended_at,sampled) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (state["user_id"], state.get("username", ""), state.get("item_id", ""),
                 state.get("item_name", ""), state.get("item_type", ""),
                 state.get("series_name", ""), state.get("device_id", ""),
                 state.get("client", ""), state.get("play_method", ""),
                 state.get("node", ""), state.get("remote_ip", ""),
                 int(state.get("bytes") or 0), seconds,
                 int(state.get("started_at") or now), int(now), int(bool(state.get('sampled')))))
            conn.execute(
                "INSERT INTO usage_daily (day,emby_user_id,bytes,seconds,plays,transcodes)"
                " VALUES (?,?,0,?,1,?) ON CONFLICT(day,emby_user_id) DO UPDATE SET "
                "seconds=seconds+?, plays=plays+1, transcodes=transcodes+?",
                (day_key(state.get("started_at") or now), state["user_id"],
                 seconds, 1 if state.get("transcoded") else 0,
                 seconds, 1 if state.get("transcoded") else 0))

    def _track_device(self, session: dict[str, Any], user_id: str, now: float) -> None:
        device_id = str(session.get("DeviceId") or "")
        if not device_id:
            return
        accepted = self._members.register_device(
            user_id, device_id,
            device_name=session.get("DeviceName") or "",
            client=session.get("Client") or "",
            app_version=session.get("ApplicationVersion") or "",
            last_ip=session.get("RemoteEndPoint") or "",
            now=int(now),
        )
        if not accepted and self._enforcement:
            sid = str(session.get("Id") or "")
            if sid:
                self._pending_kicks.append((sid, user_id))

    # -- enforcement hooks ---------------------------------------------------
    async def _kick_refused_devices(self) -> int:
        kicked = 0
        for sid, user_id in self._pending_kicks:
            try:
                stopped = await self._emby.stop_session(sid, "设备数已达上限")
            except Exception:  # noqa: BLE001 - a flaky node must not abort the rest
                stopped = False
            if stopped:
                kicked += 1
            await run_usage_io(self._members.audit,
                               "system", "device.kick", user_id, f"session={sid}")
        self._pending_kicks = []
        return kicked

    async def _enforce_exhausted(self, user_ids: list[str]) -> int:
        """Disable and disconnect members who just ran out.

        Checked here rather than on a slow timer because the gap between
        "quota reached" and "playback stops" is exactly the amount of traffic
        given away for free.
        """
        acted = 0
        for user_id in user_ids:
            member = await run_usage_io(self._members.get, user_id)
            if not member or member["state"] != "exhausted":
                continue
            # Already enforced recently; avoid hammering Emby every tick.
            applied_at = member.get("applied_at")
            if applied_at and int(time.time()) - int(applied_at) < 300:
                continue
            await self._enforcement.enforce_now(user_id, "quota exhausted")
            await self._enforcement.terminate_sessions(user_id, "流量配额已用尽")
            acted += 1
        return acted

    # -- introspection -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "ticks": self._ticks,
            "last_tick": self._last_tick,
            "tracked_sessions": len(self._live),
            "last_error": self._last_error,
        }
