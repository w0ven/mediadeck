"""Read-only member activity: verified progress only, never access or traffic.

The 30-day rolling window ends at one supplied ``now``. Intervals are unioned
per user (parallel sessions cannot inflate activity), then split at Beijing
midnight. This deliberately does not change the existing watch/traffic ledger.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from app.core.db import Database

DAY = 86400
WINDOW = 30 * DAY
BEIJING_OFFSET = 8 * 3600


def _timestamp(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _day_seconds(intervals: list[tuple[float, float]]) -> dict[int, float]:
    """Already ordered verified intervals; count their union, not session sum."""
    days: dict[int, float] = defaultdict(float)

    def add(start: float, end: float) -> None:
        while start < end:
            day = math.floor((start + BEIJING_OFFSET) / DAY)
            stop = min(end, (day + 1) * DAY - BEIJING_OFFSET)
            days[day] += stop - start
            start = stop

    merged_start = merged_end = None
    for start, end in intervals:
        if merged_end is not None and start <= merged_end:
            merged_end = max(merged_end, end)
        else:
            if merged_end is not None:
                add(merged_start, merged_end)
            merged_start, merged_end = start, end
    if merged_end is not None:
        add(merged_start, merged_end)
    return dict(days)


def member_activity(db: Database, members: list[dict[str, Any]], *, now: float,
                    live_watch: list[dict[str, Any]] | None = None,
                    sampling_status: dict[str, Any] | None = None,
                    source_failed: bool = False) -> dict[str, dict[str, Any]]:
    """Batch projection; query failures remain unknown rather than zero scores."""
    if not members:
        return {}
    window_since = now - WINDOW
    wanted = {str(m['emby_user_id']) for m in members}
    last: dict[str, float] = {}
    last_available = True
    try:
        for row in db.query('SELECT emby_user_id,last_at FROM watch_verified_totals'):
            uid = str(row['emby_user_id'])
            stamp = _timestamp(row['last_at'])
            if uid in wanted:
                if stamp is None:
                    raise ValueError('invalid verified last timestamp')
                last[uid] = stamp
    except Exception:  # noqa: BLE001 - Optional read failures remain unknown, not list failures.
        last_available = False
        last = {}

    verification_since = None
    intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
    invalid: set[str] = set()
    samples_available = True
    try:
        epoch = db.one("SELECT value FROM meta WHERE key='watch_verified_since'") or {}
        verification_since = _timestamp(epoch.get('value'))
        # The sampler writes seconds=end-start for every verified interval;
        # older wall-clock samples and imports are intentionally not consulted.
        for row in db.query(
                'SELECT emby_user_id,started_at,ended_at FROM watch_verified_samples '
                'WHERE ended_at>? AND started_at<? ORDER BY emby_user_id,started_at,ended_at',
                (max(window_since, verification_since or window_since), now)):
            uid = str(row['emby_user_id'])
            if uid not in wanted:
                continue
            start, end = _timestamp(row['started_at']), _timestamp(row['ended_at'])
            if start is None or end is None or end <= start:
                invalid.add(uid)
                continue
            if last_available:
                last[uid] = max(last.get(uid, 0), end)
            intervals[uid].append((start, end))
    except Exception:  # noqa: BLE001 - Any source read failure must suppress scoring, not become zero.
        samples_available = False

    # These are current authoritative sampler facts, not invented historical
    # uptime/coverage. Uncertain current playback cannot prove a dormant user.
    uncertain = {str(r.get('user_id')) for r in live_watch or []
                 if r.get('watch_reason') not in ('', None, 'idle', 'paused', 'stopped')}
    sampling_failed = source_failed or bool((sampling_status or {}).get('last_error'))
    result = {}
    for member in members:
        uid = str(member['emby_user_id'])
        stamp = last.get(uid) if last_available else None
        distance = max(0.0, (now - stamp) / DAY) if stamp is not None else None
        starts = [verification_since] if verification_since is not None else []
        # Member creation/registration is known; applied_at/updated_at/expiry
        # are NOT activation dates. No activation date is manufactured.
        for key in ('created_at', 'register_at', 'activated_at'):
            known = _timestamp(member.get(key))
            if known is not None:
                starts.append(known)
        observation_since = max(starts) if verification_since is not None else None
        observed_days = max(0.0, (now - observation_since) / DAY) if observation_since is not None else None
        complete = observed_days is not None and observed_days >= 30
        activity = {
            'status': 'unavailable', 'reason': '', 'score': None,
            'days_since_played': distance, 'watch_days_30d': None,
            'watch_hours_30d': None, 'observed_days': observed_days,
            'observation_since': observation_since, 'observation_complete': complete,
            'as_of': now, 'window_since': window_since, 'window_until': now,
            'source': 'verified_playback_progress',
        }
        state = member.get('entitlement_state') or member.get('state') or member.get('status')
        if state == 'pending':
            activity.update(status='pending', reason='未开通')
        elif not last_available or not samples_available or uid in invalid:
            activity['reason'] = '核验数据不可用'
        elif verification_since is None:
            activity['reason'] = '核验起点缺失'
        elif sampling_failed:
            activity['reason'] = '播放采样不可用'
        elif uid in uncertain:
            activity['reason'] = '当前播放待核验'
        else:
            since = max(window_since, observation_since)
            clipped = [(max(start, since), min(end, now))
                       for start, end in intervals.get(uid, [])
                       if min(end, now) > max(start, since)]
            days = _day_seconds(clipped)
            active_days = sum(seconds >= 300 for seconds in days.values())
            hours = sum(days.values()) / 3600
            activity.update(watch_days_30d=active_days, watch_hours_30d=hours)
            # No timestamp and insufficient coverage is unknown, not R=0.
            if distance is not None or complete:
                recency = 2 ** (-distance / 7) if distance is not None else 0
                score = round(60 * recency + 25 * min(active_days / 8, 1)
                              + 15 * min(hours / 10, 1))
                activity['score'] = max(0, min(100, score))
            if not complete:
                activity.update(status='observing', reason='观察不足30天')
            elif (distance is None or distance >= 14) and activity['score'] < 30:
                activity.update(status='inactive', reason='不活跃参考候选')
            else:
                activity.update(status='ready', reason='已完整观察')
        result[uid] = {'last_played_at': stamp, 'last_played_available': last_available,
                       'playback_activity': activity}
    return result
