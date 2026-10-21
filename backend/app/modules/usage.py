"""Verified viewing and playback safety sampling. No byte-flow accounting."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import math
import sqlite3
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


def session_activity_timestamp(session: dict[str, Any], observed_at: float) -> int:
    """Use Emby's activity clock, not the sampler's polling clock.

    ``/Sessions`` includes idle login sessions for a long time.  Refreshing a
    device with every polling tick makes all of those old identities look
    active *now*.  LastActivityDate is the actual device-side activity signal;
    missing, malformed or implausibly future values remain unknown (zero).
    Polling a session is not evidence of new device activity.
    """
    raw = str(session.get("LastActivityDate") or "").strip()
    if raw:
        try:
            value = datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            timestamp = int(value.timestamp())
            if 0 < timestamp <= int(observed_at) + 300:
                return timestamp
        except (ValueError, OverflowError, OSError):
            pass
    return 0


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


WATCH_HEARTBEAT_MAX_AGE = 60


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def watch_observation(session: dict[str, Any], now: float) -> dict[str, Any]:
    """Keep reported evidence, never turn a polling tick into a heartbeat."""
    play = session.get('PlayState') or {}
    position = _number(play.get('PositionTicks'))
    position = position / TICKS_PER_SECOND if position is not None and position >= 0 else None
    rate = _number(play.get('PlaybackRate', session.get('PlaybackRate')))
    runtime = _number((session.get('NowPlayingItem') or {}).get('RunTimeTicks'))
    activity = None
    try:
        stamp = str(session.get('LastActivityDate') or '').strip()
        parsed = datetime.fromisoformat(stamp)
        activity = parsed.replace(tzinfo=UTC).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        pass
    reason = ('idle' if not session.get('NowPlayingItem') else
              'paused' if play.get('IsPaused') is True else
              'pause_unknown' if play.get('IsPaused') is not False else
              'progress_missing' if position is None else
              'progress_invalid' if runtime is not None and runtime > 0 and position > runtime / TICKS_PER_SECOND else
              'rate_unknown' if rate is None or rate <= 0 else
              'heartbeat_missing' if activity is None else
              'heartbeat_stale' if not -2 <= now - activity <= WATCH_HEARTBEAT_MAX_AGE else '')
    return {'position': position, 'activity': activity, 'rate': rate, 'anchor_floor': now,
            'play_id': str(session.get('PlaySessionId') or play.get('PlaySessionId') or ''),
            'device_id': str(session.get('DeviceId') or ''), 'reason': reason}


def verified_watch_interval(before: dict | None, current: dict, observed_gap: float,
                            since: float = 0) -> tuple[float, float, str]:
    """Conservative continuous-play proof; uncertain intervals are not backfilled."""
    if current['reason']:
        return 0, 0, current['reason']
    if not before or before.get('reason'):
        return 0, 0, 'baseline'
    if not 0 < observed_gap <= MAX_BILLABLE_GAP_SECONDS:
        return 0, 0, 'observation_gap'
    if (before.get('device_id') != current['device_id']
            or before.get('play_id') != current['play_id']):
        return 0, 0, 'identity_changed'
    elapsed = current['activity'] - before['activity']
    progress = current['position'] - before['position']
    if not 0 < elapsed <= MAX_BILLABLE_GAP_SECONDS or progress <= 0:
        return 0, 0, 'stalled_or_rewound'
    if before.get('timing_basis') != current.get('timing_basis'):
        return 0, 0, 'baseline'
    if current.get('timing_basis') == 'accepted_progress':
        if elapsed > WATCH_HEARTBEAT_MAX_AGE or progress > max(1, elapsed * 4):
            return 0, 0, 'seek_or_buffering'
        end = current['activity']
        start = max(since, before.get('anchor_floor', before['activity']), end-elapsed)
        return (start, end, '') if end > start else (0, 0, 'before_verification')
    if before['rate'] != current['rate']:
        return 0, 0, 'rate_changed'
    expected = elapsed * current['rate']
    # Accepted reports use arrival time, which includes bounded network jitter.
    # Poll snapshots retain their stricter activity-clock check.
    jitter = current['rate'] * min(2, elapsed * 0.2) if current.get('accepted_report') else 0
    if abs(progress - expected) > max(0.75, expected * 0.05, jitter):
        return 0, 0, 'seek_or_buffering'
    end = current['activity']
    start = max(since, before.get('anchor_floor', before['activity']),
                end - min(elapsed, progress / current['rate']))
    return (start, end, '') if end > start else (0, 0, 'before_verification')


def continuous_watch_interval(before: dict, current: dict, since: float) -> tuple[float, float, str]:
    elapsed = current['activity'] - before['activity']
    progress = current['position'] - before['position']
    if not 0 < elapsed <= WATCH_HEARTBEAT_MAX_AGE:
        return 0, 0, 'seek_or_buffering'
    speed = progress / elapsed
    if not max(1.1 * current['rate'], current['rate'] + 0.1) < speed <= 4:
        return 0, 0, 'seek_or_buffering'
    intervals = [*(before.get('continuous_intervals') or []), (elapsed, progress)][-3:]
    duration = sum(pair[0] for pair in intervals)
    distance = sum(pair[1] for pair in intervals)
    average = distance / duration
    if any(abs(distance - average * seconds) >
           max(0.75, average * seconds * 0.05, average * min(2, seconds * 0.25)) + 0.000001
           for seconds, distance in intervals):
        intervals = [(elapsed, progress)]
        duration = elapsed
    current['continuous_intervals'] = intervals
    if len(intervals) < 3 or duration < 20:
        return 0, 0, 'seek_or_buffering'
    # Repeated advancing reports can establish continuity, not an actual rate.
    # Credit only this interval; never backfill the candidate windows.
    current['credit_basis'] = 'continuous_progress'
    end = current['activity']
    start = max(since, before.get('anchor_floor', before['activity']), end-elapsed)
    return (start, end, '') if end > start else (0, 0, 'before_verification')


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
        epoch = db.one("SELECT value FROM meta WHERE key='watch_verified_since'") or {}
        self._verified_since = float(epoch.get('value') or 0)
        for row in db.query('SELECT session_id,state_json FROM watch_checkpoints'):
            value = json.loads(row['state_json'])
            if (not value.get('stopped_by_report') and value.get('play_id') and value.get('device_id')
                    and value.get('item_id') and value.get('play_id_source') in ('sessions', 'lease', 'issuance')
                    and 0 <= time.time()-float(value.get('last_ts') or 0) <= WATCH_HEARTBEAT_MAX_AGE):
                metadata = {key: value.get(key) for key in
                            ('client', 'item_name', 'item_type', 'series_name', 'runtime_ticks')}
                db.execute('INSERT OR IGNORE INTO watch_playbacks '
                           '(user_id,play_id,session_id,device_id,item_id,issued_at,metadata_json) '
                           'VALUES(?,?,?,?,?,?,?)',
                           (value['user_id'], value['play_id'], row['session_id'], value['device_id'],
                            value['item_id'], float(value.get('started_at') or time.time()), json.dumps(metadata)))
            value['last_ts'] = None
            value['was_playing'] = False
            value['speed_bps'] = 0
            value['watch_observation'] = None
            value['watch_source'] = ''
            value['report_received_at'] = 0
            value['report_signature'] = None
            value.setdefault('verified_seconds', 0.0)
            self._live[row['session_id']] = value
        self._last_tick = 0.0
        self._last_error: str | None = None
        self._ticks = 0
        # Excess concurrent plays collected during a tick, kicked after billing
        # so one over-limit client cannot abort accounting for everyone else.
        # This is a safety net for traffic that never hits the panel edge;
        # admission refusal on the stream path is the actual guarantee.
        self._pending_kicks: list[tuple[str, str]] = []
        self.concurrency_handler: Any = None
        self.session_client_handler: Any = None
        self._concurrency_kicks: set[tuple[str, str]] = set()
        # Clients that ignore Playback/Stop would otherwise be retried every
        # tick (~5s), hammering Emby session writes. Back off per session id.
        self._kick_failures: dict[str, int] = {}
        self._kick_backoff_until: dict[str, float] = {}
        self._tick_lock = asyncio.Lock()
        self._publish_live()

    async def playback_report(self, user_id: str, device: str, payload: dict, *, stopped: bool) -> None:
        """Successful authenticated Stopped closes watch proof immediately.

        Cached /Sessions rows cannot resurrect that same play. Started creates
        a fresh baseline; delayed Stop for an older PlaySessionId is ignored.
        """
        async with self._tick_lock:
            await run_usage_io(self._playback_report, user_id, device, payload, stopped)

    def _playback_report(self, user_id, device, payload, stopped):
        sid = str(payload.get('SessionId') or '')
        play = str(payload.get('PlaySessionId') or '')
        if stopped and play and device:
            self._db.execute('UPDATE watch_playbacks SET closed_at=? WHERE user_id=? AND play_id=? '
                             'AND device_id=?', (time.time(), user_id, play, device))
        for current_sid, state in self._live.items():
            if state['user_id'] != user_id or (sid and current_sid != sid):
                continue
            if device and str(state.get('device_id') or '') != device:
                continue
            if play and str(state.get('play_id') or '') != play:
                continue
            if payload.get('ItemId') and str(payload['ItemId']) != state.get('item_id'):
                continue
            if stopped and state.get('play_id_source') == 'lease':
                # The route already released the current Stop's lease. An
                # extant replacement lease must not be closed by an old Stop.
                lease = self._db.one('SELECT play_id FROM stream_leases WHERE user_id=? AND session_id=?',
                                     (user_id, current_sid))
                if lease and lease['play_id'] != (play or state.get('play_id')):
                    continue
            if not sid and not device and not play:
                continue  # no identity evidence: do not stop unrelated streams
            if state.get('play_id') and state.get('device_id'):
                if stopped:
                    self._db.execute('INSERT INTO watch_playbacks '
                                     '(user_id,play_id,session_id,device_id,item_id,issued_at,closed_at) '
                                     'VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,play_id) '
                                     'DO UPDATE SET closed_at=excluded.closed_at',
                                     (user_id, state['play_id'], current_sid, state['device_id'],
                                      state['item_id'], state.get('started_at') or 0, time.time()))
                else:
                    self._db.execute('UPDATE watch_playbacks SET closed_at=0 WHERE user_id=? AND play_id=? '
                                     'AND session_id=? AND device_id=? AND NOT EXISTS '
                                     '(SELECT 1 FROM watch_playbacks newer WHERE newer.user_id=? '
                                     'AND newer.session_id=? AND newer.issued_at>watch_playbacks.issued_at)',
                                     (user_id, state['play_id'], current_sid, state['device_id'], user_id, current_sid))
            state['stopped_by_report'] = bool(stopped)
            state['watch_observation'] = None
            state['watch_source'] = ''
            state['report_received_at'] = 0
            state['report_signature'] = None
            if not stopped and payload.get('ItemId') == state.get('item_id'):
                state['playback_attempt_at'] = time.time()
            if stopped:
                state['was_playing'] = False
                state['watch_reason'] = 'stopped'
            with self._db.write() as conn:
                conn.execute('INSERT INTO watch_checkpoints VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json',
                             (current_sid, json.dumps(state)))
        if stopped and play and device:
            self._db.execute('INSERT OR IGNORE INTO watch_playbacks '
                             '(user_id,play_id,session_id,device_id,item_id,issued_at,closed_at) '
                             'VALUES(?,?,?,?,?,0,?)',
                             (user_id, play, sid, device, str(payload.get('ItemId') or ''), time.time()))
        self._publish_live()

    async def playback_progress(self, user_id: str, device: str, payload: dict, *,
                                received_at: float | None = None) -> None:
        """Only called after caller authentication AND upstream acceptance."""
        received_at = time.time() if received_at is None else received_at
        async with self._tick_lock:
            await run_usage_io(self._progress, user_id, device, payload, received_at)

    def _diagnose(self, uid: str, observation: dict, now: float, source: str,
                  reason: str, credited: float = 0, attempt_at: float = 0) -> None:
        prior = self._db.one('SELECT summary_json FROM watch_diagnostics WHERE emby_user_id=?', (uid,)) or {}
        previous = json.loads(prior.get('summary_json') or '{}')
        counts = previous.get('counts') or {}
        key = reason or 'verified'
        counts[key] = min(1_000_000_000, int(counts.get(key) or 0)+1)
        if source == 'progress':
            counts['progress_'+key] = min(1_000_000_000, int(counts.get('progress_'+key) or 0)+1)
        summary = {'source': source, 'reason': key, 'binding': observation.get('binding_source'),
                   'position': observation.get('position'),
                   'rate': observation.get('rate'),
                   'timing_basis': observation.get('credit_basis') or observation.get('timing_basis', 'rate_verified'),
                   'credited_seconds': credited,
                   'attempt_at': max(attempt_at, float(previous.get('attempt_at') or 0)),
                   'counts': counts}
        with self._db.write() as conn:
            conn.execute('INSERT INTO watch_diagnostics VALUES(?,?,?) ON CONFLICT(emby_user_id) '
                         'DO UPDATE SET observed_at=excluded.observed_at,summary_json=excluded.summary_json',
                         (uid, now, json.dumps(summary)))
            conn.execute('DELETE FROM watch_diagnostics WHERE observed_at<?', (now-14*86400,))
            conn.execute('DELETE FROM watch_diagnostics WHERE emby_user_id IN '
                         '(SELECT emby_user_id FROM watch_diagnostics ORDER BY observed_at DESC LIMIT -1 OFFSET 5000)')

    def _proof(self, state: dict, observation: dict, gap: float, source: str) -> tuple[float, float, str]:
        before = state.get('watch_observation') if state.get('watch_source', 'poll') == source else None
        start, end, reason = verified_watch_interval(before, observation, gap, self._verified_since)
        if (source == 'progress' and reason == 'seek_or_buffering' and before
                and not before.get('reason') and not observation.get('reason')
                and observation.get('rate') is not None
                and before.get('rate') == observation['rate']):
            start, end, reason = continuous_watch_interval(before, observation, self._verified_since)
        if end > start:
            start = max(start, float(state.get('last_verified_end') or 0))
            if end <= start:
                start, end, reason = 0, 0, 'already_recorded'
            else:
                state['last_verified_end'] = end
                observation['anchor_floor'] = observation['activity']
        state.update(watch_observation=observation, watch_source=source, watch_reason=reason)
        return start, end, reason

    def _report_play_matches(self, sid: str, state: dict, uid: str, play: str) -> bool:
        if not play:
            return False
        binding = self._db.one('SELECT session_id,device_id,item_id,closed_at FROM watch_playbacks '
                               'WHERE user_id=? AND play_id=?', (uid, play))
        if binding:
            return (not binding['closed_at'] and binding['session_id'] == sid
                    and binding['device_id'] == state.get('device_id')
                    and binding['item_id'] == state.get('item_id'))
        if state.get('play_id') and state.get('play_id_source') != 'lease':
            return state['play_id'] == play  # explicit Sessions identity wins
        # Emby native Sessions may omit PlaySessionId. Only PlaybackInfo's
        # existing, already-observed admission lease can supply that binding.
        with contextlib.suppress(sqlite3.OperationalError):
            lease = self._db.one('SELECT observed,snapshot_guard FROM stream_leases '
                                 'WHERE user_id=? AND session_id=? AND play_id=?', (uid, sid, play))
            return bool(lease and lease['observed'] and not lease['snapshot_guard'])
        return False

    def _progress(self, uid: str, device: str, payload: dict, received_at: float) -> None:
        now = time.time()
        sid, play, item = (str(payload.get(k) or '') for k in ('SessionId','PlaySessionId','ItemId'))
        binding = self._db.one('SELECT * FROM watch_playbacks WHERE user_id=? AND play_id=?', (uid, play))
        replacement = None
        if (binding and not binding['closed_at'] and device and device == binding['device_id']
                and item == binding['item_id'] and (not sid or sid == binding['session_id'])):
            key = binding['session_id']
            state = self._live.get(key)
            if not state or state.get('item_id') != item or state.get('play_id') != play:
                metadata = json.loads(binding['metadata_json'])
                replacement = {
                    'user_id': uid, 'username': (self._members.get(uid) or {}).get('username', ''),
                    'item_id': item, 'item_name': metadata.get('item_name', ''),
                    'item_type': metadata.get('item_type', ''), 'series_name': metadata.get('series_name', ''),
                    'client': metadata.get('client', ''), 'play_method': '', 'remote_ip': '',
                    'device_id': device, 'play_id': play, 'play_id_source': 'issuance',
                    'runtime_ticks': metadata.get('runtime_ticks'), 'started_at': int(received_at),
                    'watch_key': uuid.uuid4().hex, 'sampled': True, 'seconds': 0.0,
                    'verified_seconds': 0.0, 'bytes': 0, 'last_ts': now, 'was_playing': False,
                    'watch_observation': None, 'watch_source': '', 'last_verified_end': 0,
                    'report_received_at': 0, 'report_signature': None, 'transcoded': False,
                }
        candidates = {**self._live, binding['session_id']: replacement} if replacement else self._live
        matches = [(key, state) for key, state in candidates.items()
                   if state['user_id'] == uid and (not sid or key == sid)
                   and item and state.get('item_id') == item
                   and device and state.get('device_id') == device
                   and self._report_play_matches(key, state, uid, play)]
        if len(matches) != 1:
            if item and play and self._members.get(uid):
                self._diagnose(uid, {}, now, 'progress', 'session_unresolved', attempt_at=received_at)
            return
        sid, state = matches[0]
        if state.get('stopped_by_report'):
            self._diagnose(uid, {}, now, 'progress', 'stopped')
            return
        if received_at <= float(state.get('report_received_at') or 0):
            self._diagnose(uid, {}, now, 'progress', 'out_of_order_progress')
            return
        reported = payload.get('PlayState') if isinstance(payload.get('PlayState'), dict) else {}
        position = payload.get('PositionTicks', reported.get('PositionTicks'))
        paused = payload.get('IsPaused', reported.get('IsPaused'))
        rate = payload.get('PlaybackRate', reported.get('PlaybackRate'))
        missing_rate = 'PlaybackRate' not in payload and 'PlaybackRate' not in reported
        observation = watch_observation({
            'DeviceId': device, 'PlaySessionId': play,
            'NowPlayingItem': {'Id': item, 'RunTimeTicks': state.get('runtime_ticks')},
            'PlayState': {'PositionTicks': position, 'IsPaused': paused, 'PlaybackRate': rate},
            'LastActivityDate': datetime.fromtimestamp(received_at, UTC).isoformat()}, now)
        observation['accepted_report'] = True
        if missing_rate:
            observation['timing_basis'] = 'accepted_progress'
            if observation['reason'] == 'rate_unknown':
                observation['reason'] = ('' if -2 <= now-received_at <= WATCH_HEARTBEAT_MAX_AGE
                                         else 'heartbeat_stale')
        if payload.get('IsSeeking') is True or str(payload.get('EventName') or '').lower() in ('seek', 'seeking'):
            observation['reason'] = 'seek_or_buffering'
        signature = [observation['position'], paused, observation['rate']]
        if signature == state.get('report_signature') and not observation['reason']:
            if received_at-float(state.get('report_received_at') or received_at) > 2:
                observation['reason'] = 'stalled_or_rewound'
            else:
                self._diagnose(uid, observation, now, 'progress', 'duplicate_progress', attempt_at=received_at)
                return
        before = state.get('watch_observation')
        gap = received_at-float(state.get('report_received_at') or received_at)
        updated = dict(state)
        lease_bound = not binding and (not state.get('play_id') or state.get('play_id_source') == 'lease')
        observation['binding_source'] = 'issuance' if binding else 'lease' if lease_bound else 'sessions'
        updated.update(play_id=play, play_id_source=observation['binding_source'])
        start, end, reason = self._proof(updated, observation, gap, 'progress')
        delta = end-start
        updated.update(report_received_at=received_at, report_signature=signature,
                       playback_attempt_at=received_at, seconds=float(state['seconds'])+delta,
                       verified_seconds=float(state.get('verified_seconds') or 0)+delta)
        # Reporting changes viewing only. It must not bill bytes, traffic or points.
        if not self._record_watch(sid, updated, start if delta else now, end if delta else now,
                                  evidence=(before, observation) if delta else None,
                                  required_lease=play if lease_bound else None,
                                  required_binding=play if binding else None):
            self._diagnose(uid, observation, now, 'progress', 'session_unresolved', attempt_at=received_at)
            return
        if replacement:
            old = self._live.get(sid)
            if old:
                self._finish(sid, old, now)
            self._live[sid] = state
        state.update(updated)
        self._diagnose(uid, observation, now, 'progress', reason, delta, received_at)
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

        now = time.time()  # timestamp the returned snapshot, not request issuance
        result, _billed_users = await run_usage_io(self._sample, sessions, now, node_of)
        if self.session_client_handler:
            try:
                result["session_clients"] = await self.session_client_handler(sessions)
            except Exception:  # noqa: BLE001 - metering continues, failure stays explicit
                result["session_clients"] = {"ok": False, "error": "session client verification unavailable"}
        if self._pending_kicks and self._enforcement:
            result["stream_kicks"] = await self._kick_overflow_streams()
        return result

    def _sample(self, sessions: list[dict[str, Any]], now: float,
                node_of: Any) -> tuple[dict[str, Any], list[str]]:
        """Existing ordered persistence, off the ASGI/event-loop thread."""
        self._ticks += 1
        self._last_error = None
        seen: set[str] = set()
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
            observation = watch_observation(session, now)

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
                    "verified_seconds": 0.0,
                    "watch_observation": observation,
                    "watch_source": 'poll',
                    "report_received_at": 0,
                    "report_signature": None,
                    "last_verified_end": 0,
                    "runtime_ticks": item.get('RunTimeTicks'),
                    "reported_rate": _number((session.get('PlayState') or {}).get('PlaybackRate', session.get('PlaybackRate'))),
                    "reported_rate_at": observation.get('activity') or 0,
                    "playback_attempt_at": 0,
                    "watch_reason": observation['reason'] or 'baseline',
                    "play_id": observation['play_id'],
                    "play_id_source": 'sessions',
                    "last_ts": now,
                    "was_playing": playing,
                    "seconds": 0.0,
                    "bytes": 0,
                    "transcoded": bool(session.get("TranscodingInfo")),
                }
                continue

            # A session that switched title is two plays, not one.
            report_fresh = (state.get('watch_source') == 'progress' and
                            0 <= now-float(state.get('report_received_at') or 0) <= WATCH_HEARTBEAT_MAX_AGE and
                            str(session.get('DeviceId') or '') == state.get('device_id'))
            # A fresh accepted report may be more current than a cached idle row.
            # This retains viewing identity only; `playing`/traffic stay unchanged.
            current_item = state['item_id'] if report_fresh else str(item.get("Id") or '')
            if (user_id != state['user_id'] or current_item != state['item_id']
                    or (not report_fresh and state.get('play_id') and observation['play_id']
                        and state['play_id'] != observation['play_id'])):
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
                    "verified_seconds": 0.0,
                    "watch_observation": observation,
                    "watch_source": 'poll',
                    "report_received_at": 0,
                    "report_signature": None,
                    "last_verified_end": 0,
                    "runtime_ticks": item.get('RunTimeTicks'),
                    "reported_rate": _number((session.get('PlayState') or {}).get('PlaybackRate', session.get('PlaybackRate'))),
                    "reported_rate_at": observation.get('activity') or 0,
                    "playback_attempt_at": 0,
                    "watch_reason": observation['reason'] or 'baseline',
                    "play_id": observation['play_id'],
                    "play_id_source": 'sessions',
                    "stopped_by_report": False,
                    "last_ts": now,
                    "was_playing": playing,
                    "seconds": 0.0,
                    "bytes": 0,
                }
                continue

            if str(item.get('Id') or '') != state['item_id']:
                item = {}
            # Session endpoints/client metadata can change without a new Id.
            # Sharing observes this tick's network, never a stale first address.
            state.update({
                "item_name": item.get('Name') or state.get('item_name') or '',
                "item_type": item.get('Type') or state.get('item_type') or '',
                "series_name": item.get('SeriesName') or state.get('series_name') or '',
                "remote_ip": session.get("RemoteEndPoint") or "",
                "device_id": session.get("DeviceId") or "",
                "client": session.get("Client") or "",
                "play_method": (session.get("PlayState") or {}).get("PlayMethod") or "",
                "runtime_ticks": item.get('RunTimeTicks') or state.get('runtime_ticks'),
                "reported_rate": _number((session.get('PlayState') or {}).get('PlaybackRate', session.get('PlaybackRate'))),
                "reported_rate_at": observation.get('activity') or 0,
            })
            delta = now - float(state['last_ts']) if state.get('last_ts') is not None else 0
            closed = self._db.one('SELECT closed_at FROM watch_playbacks WHERE user_id=? AND play_id=?',
                                  (user_id, state.get('play_id') or ''))
            if state.get('stopped_by_report') or (closed and closed['closed_at']):
                observation['reason'] = 'stopped'
            before = state.get('watch_observation')
            proof_state = dict(state)
            if report_fresh:
                start, end, reason = 0, 0, state.get('watch_reason') or ''
            else:
                start, end, reason = self._proof(proof_state, observation, delta, 'poll')
                state.update({key: proof_state.get(key) for key in
                              ('watch_observation', 'watch_reason', 'watch_source')})
            watch_delta = end - start
            if not report_fresh:
                state['play_id'] = observation['play_id'] or state.get('play_id') or ''
                if observation['play_id']:
                    state['play_id_source'] = 'sessions'
            state["last_ts"] = now
            state['was_playing'] = playing
            if not playing:
                state["speed_bps"] = 0
                continue
            if delta <= 0:
                continue
            # Clamp: a long gap means the sampler was down, not that the user
            # watched continuously through it.
            delta = min(delta, MAX_BILLABLE_GAP_SECONDS)

            # Playback evidence is independent of bytes served. Never estimate traffic.
            # Sample and resumable state commit together: a crash cannot leave
            # totals newer than the corresponding session history.
            updated = {**state, 'last_verified_end': proof_state.get('last_verified_end') or 0,
                       'seconds': float(state['seconds']) + watch_delta,
                       'verified_seconds': float(state.get('verified_seconds') or 0) + watch_delta,
                       'bytes': 0}
            self._record_watch(sid, updated, start if watch_delta else now, end if watch_delta else now,
                               evidence=(before, observation) if watch_delta else None)
            state.update(updated)
            if not report_fresh:
                advanced = (before and before.get('position') is not None and observation['position'] is not None
                            and observation['position'] > before['position'] and observation['activity'] is not None
                            and not observation['reason'] and before.get('play_id') == observation['play_id']
                            and before.get('device_id') == observation['device_id'])
                if advanced:
                    state['playback_attempt_at'] = observation['activity']
                self._diagnose(user_id, observation, now, 'poll', reason, watch_delta,
                               observation['activity'] if advanced else 0)
            state.pop('speed_bps', None)
            if session.get("TranscodingInfo"):
                state["transcoded"] = True
            if node_of:
                state["node"] = node_of(state.get("item_id")) or state.get("node", "")

            # Keep the watched users for expiry/concurrency enforcement, not billing.
            billed_users[user_id] = 0

        self._collect_overflow_kicks(sessions, now)

        # Sessions that vanished have ended.
        for sid in [s for s in self._live if s not in seen]:
            state = self._live[sid]
            if (state.get('watch_source') == 'progress' and not state.get('stopped_by_report')
                    and 0 <= now-float(state.get('report_received_at') or 0) <= WATCH_HEARTBEAT_MAX_AGE):
                state.update(speed_bps=0, was_playing=False)  # no inferred traffic or time
                continue
            self._finish(sid, state, now)
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
            "traffic_tracking": False,
            "users": len(billed_users),
        }
        if self._sharing is not None:
            result["sharing_findings"] = sharing_found
        self._publish_live()
        # Enforcement stays after committed accounting, in the async caller.
        return result, list(billed_users)

    # -- persistence ---------------------------------------------------------
    def _record_watch(self, sid: str, state: dict, start: float, end: float, *,
                      evidence: tuple[dict, dict] | None = None,
                      required_lease: str | None = None,
                      required_binding: str | None = None) -> bool:
        with self._db.write() as conn:
            if required_binding:
                binding = conn.execute('SELECT issued_at FROM watch_playbacks WHERE user_id=? AND session_id=? '
                                       'AND play_id=? AND device_id=? AND item_id=? AND closed_at=0',
                                       (state['user_id'], sid, required_binding, state['device_id'], state['item_id'])).fetchone()
                if not binding:
                    return False
                conn.execute('UPDATE watch_playbacks SET closed_at=? WHERE user_id=? AND session_id=? '
                             'AND play_id<>? AND issued_at<=? AND closed_at=0',
                             (time.time(), state['user_id'], sid, required_binding, binding[0]))
            if required_lease is not None and not conn.execute(
                    'SELECT 1 FROM stream_leases WHERE user_id=? AND session_id=? AND play_id=? '
                    'AND observed=1 AND snapshot_guard=0', (state['user_id'], sid, required_lease)).fetchone():
                return False  # same-transaction replacement/late-report guard
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
            if end > start and evidence is not None:
                before, current = evidence
                added = conn.execute(
                    'INSERT OR IGNORE INTO watch_verified_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key, state['user_id'], state.get('item_id') or '', state.get('item_name') or '',
                     state.get('item_type') or '', state.get('series_name') or '', state.get('client') or '',
                     state.get('play_method') or '', start, end, end-start,
                     before['position'], current['position'],
                     None if current.get('credit_basis') == 'continuous_progress' else current['rate'])).rowcount
                if added:
                    conn.execute('INSERT INTO watch_verified_totals VALUES(?,?,?,?) '
                                 'ON CONFLICT(emby_user_id) DO UPDATE SET seconds=seconds+excluded.seconds,'
                                 'first_at=MIN(first_at,excluded.first_at),last_at=MAX(last_at,excluded.last_at)',
                                 (state['user_id'], end-start, start, end))
            conn.execute('INSERT INTO watch_checkpoints VALUES(?,?) '
                         'ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json',
                         (sid, json.dumps(state)))
        return True

    def _commit(self, per_user: dict[str, int], now: float) -> None:
        """Legacy byte-accounting entry point: retired without touching history."""
        return

    def _publish_live(self) -> None:
        # Readers must never iterate the worker's mutable session dictionary
        # or wait on a mutex held during slow disk I/O.
        self._watch_view = tuple(
            {k: state.get(k) for k in ('user_id', 'username', 'started_at', 'seconds', 'sampled', 'verified_seconds', 'watch_reason', 'watch_source', 'playback_attempt_at')}
            for state in self._live.values())
        self._speed_view = {}

    def live_watch(self) -> list[dict[str, Any]]:
        """Already committed sampled time; querying does not advance clocks."""
        return [dict(row) for row in self._watch_view]

    def live_speeds(self) -> dict[str, int]:
        """Byte-rate sampling is retired; watch sampling continues independently."""
        return {}

    def _finish(self, sid: str, state: dict[str, Any], now: float) -> None:
        seconds = int(state.get("seconds") or 0)
        if seconds < MIN_PLAY_SECONDS:
            self._db.execute('DELETE FROM watch_checkpoints WHERE session_id=? '
                             "AND COALESCE(json_extract(state_json,'$.watch_key'),'')=?",
                             (sid, state.get('watch_key') or ''))
            return
        with self._db.write() as conn:
            conn.execute('DELETE FROM watch_checkpoints WHERE session_id=? '
                         "AND COALESCE(json_extract(state_json,'$.watch_key'),'')=?",
                         (sid, state.get('watch_key') or ''))
            conn.execute(
                "INSERT INTO play_events (emby_user_id,username,item_id,item_name,"
                "item_type,series_name,device_id,client,play_method,node,remote_ip,"
                "bytes,seconds,started_at,ended_at,sampled,watch_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (state["user_id"], state.get("username", ""), state.get("item_id", ""),
                 state.get("item_name", ""), state.get("item_type", ""),
                 state.get("series_name", ""), state.get("device_id", ""),
                 state.get("client", ""), state.get("play_method", ""),
                 state.get("node", ""), state.get("remote_ip", ""),
                 0, seconds,
                 int(state.get("started_at") or now), int(now), int(bool(state.get('sampled'))),
                 state.get('watch_key') or ''))
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
        self._members.register_device(
            user_id, device_id,
            device_name=session.get("DeviceName") or "",
            client=session.get("Client") or "",
            app_version=session.get("ApplicationVersion") or "",
            last_ip=session.get("RemoteEndPoint") or "",
            now=int(now),
            seen_at=session_activity_timestamp(session, now),
        )

    def _collect_overflow_kicks(self, sessions: list[dict[str, Any]], now: float) -> None:
        """Queue newest extra *playing* sessions after the full list is known.

        Stop is advisory. This cannot replace edge admission, and a failed Stop
        must not retry every tick.
        """
        if not self._enforcement:
            return
        from app.modules.streams import overflow_session_ids

        by_user: dict[str, list[dict[str, Any]]] = {}
        for session in sessions:
            uid = str(session.get("UserId") or "")
            if uid:
                by_user.setdefault(uid, []).append(session)
        queued: set[str] = {sid for sid, _ in self._pending_kicks}
        for user_id, rows in by_user.items():
            member = self._members.get(user_id)
            if not member:
                continue
            for session in rows:
                sid = str(session.get("Id") or "")
                device_id = str(session.get("DeviceId") or "")
                if not sid or sid in queued or not is_playing(session):
                    continue
                if device_id and self._members.device_blocked(user_id, device_id):
                    if self._kick_backoff_until.get(sid, 0.0) > now:
                        continue
                    queued.add(sid)
                    self._pending_kicks.append((sid, user_id))
            cap = int(member.get("max_streams") or 0)
            if cap <= 0:
                continue
            for sid in overflow_session_ids(rows, user_id, cap):
                if sid in queued:
                    continue
                if self._kick_backoff_until.get(sid, 0.0) > now:
                    continue
                queued.add(sid)
                self._pending_kicks.append((sid, user_id))
                self._concurrency_kicks.add((sid, user_id))

    # -- enforcement hooks ---------------------------------------------------
    async def _kick_overflow_streams(self) -> int:
        kicked = 0
        now = time.time()
        for sid, user_id in self._pending_kicks:
            try:
                if self.concurrency_handler and (sid, user_id) in self._concurrency_kicks:
                    stopped = await self.concurrency_handler(user_id, sid)
                else:
                    stopped = await self._emby.stop_session(sid, "同时播放路数已达上限")
            except Exception:  # noqa: BLE001 - a flaky node must not abort the rest
                stopped = False
            if stopped:
                kicked += 1
            # Back off even on HTTP success: acceptance does not prove playback
            # stopped, and repeating accepted commands every tick is a storm.
            fails = min(self._kick_failures.get(sid, 0) + 1, 5)
            self._kick_failures[sid] = fails
            self._kick_backoff_until[sid] = now + min(60 * (2 ** (fails - 1)), 900)
            await run_usage_io(self._members.audit,
                               "system", "stream.kick", user_id, f"session={sid}")
        self._pending_kicks = []
        self._concurrency_kicks.clear()
        return kicked

    # -- introspection -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "ticks": self._ticks,
            "last_tick": self._last_tick,
            "tracked_sessions": len(self._live),
            "last_error": self._last_error,
        }
