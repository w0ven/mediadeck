"""Read existing viewing records once; never migrate them into proof or money.

Completed sampled events and verified intervals duplicate the sample ledger.
The legacy import is bounded before Deck's first record. Scalar totals survive
pruning but are never distributed into invented dates, sessions or titles.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

BEIJING = timezone(timedelta(hours=8))


def previous_week(now: float) -> dict[str, Any]:
    local = datetime.fromtimestamp(now, BEIJING)
    end = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    return {'since': start.timestamp(), 'until': end.timestamp(),
            'timezone': 'Asia/Shanghai',
            'label': f'{start:%Y-%m-%d} 00:00 至 {end:%Y-%m-%d} 00:00（北京时间）'}


def lifetime(db: Any, uid: str) -> dict[str, Any]:
    values = []
    for table in ('watch_legacy_baselines', 'watch_totals',
                  'watch_sample_totals', 'watch_verified_totals'):
        values.append(db.one(f'SELECT seconds,first_at FROM {table} WHERE emby_user_id=?',
                             (uid,)) or {})
    legacy, old, sampled, verified = values
    # Every verified increment is committed to the sample umbrella too. MAX
    # also preserves a verified-only installation; it is NOT a second addition.
    seconds = (float(legacy.get('seconds') or 0) + float(old.get('seconds') or 0) +
               max(float(sampled.get('seconds') or 0), float(verified.get('seconds') or 0)))
    starts = [float(row['first_at']) for row in values if row.get('first_at')]
    return {'seconds': seconds, 'first_at': min(starts) if starts else None}


def records(db: Any, uid: str, since: float, until: float) -> list[dict[str, Any]]:
    """Timestamp-backed records, with one authoritative row per source identity."""
    meta = """WITH info AS (
        SELECT watch_key,MAX(item_id) AS item_id,MAX(item_name) AS item_name,
               MAX(item_type) AS item_type,MAX(series_name) AS series_name,
               MAX(client) AS client,MAX(play_method) AS play_method,MAX(node) AS node
        FROM play_events WHERE emby_user_id=? AND sampled=1 AND watch_key<>'' GROUP BY watch_key
    ) """
    rows = db.query(meta + """
        SELECT v.run_key,v.item_id,v.item_name,v.item_type,v.series_name,v.client,v.play_method,
               COALESCE(i.node,'') AS node,
               v.started_at,v.ended_at,v.seconds,1 AS exact,'progress' AS source
        FROM watch_verified_samples v LEFT JOIN info i ON i.watch_key=v.run_key
        WHERE v.emby_user_id=? AND v.ended_at>? AND v.started_at<?
        UNION ALL
        SELECT s.session_key,COALESCE(i.item_id,''),COALESCE(i.item_name,''),
               COALESCE(i.item_type,''),COALESCE(i.series_name,''),COALESCE(i.client,''),
               COALESCE(i.play_method,''),COALESCE(i.node,''),
               s.started_at,s.ended_at,s.seconds,1,'sample'
        FROM watch_samples s LEFT JOIN info i ON i.watch_key=s.session_key
        WHERE s.emby_user_id=? AND s.ended_at>? AND s.started_at<? AND NOT EXISTS (
            SELECT 1 FROM watch_verified_samples v WHERE v.run_key=s.session_key
            AND v.emby_user_id=s.emby_user_id AND v.ended_at=s.ended_at)
        UNION ALL
        SELECT 'event:'||id,item_id,item_name,item_type,series_name,client,play_method,node,
               started_at,ended_at,seconds,seconds=ended_at-started_at,'event'
        FROM play_events WHERE emby_user_id=? AND sampled=0 AND ended_at>? AND started_at<?
        UNION ALL
        SELECT 'legacy:'||event_id,'','','','','','','',
               started_at,ended_at,seconds,seconds=ended_at-started_at,'import'
        FROM watch_legacy_events WHERE emby_user_id=? AND ended_at>? AND started_at<?
        ORDER BY started_at,ended_at
        """, (uid, *(uid, since, until)*4))
    return rows


def _credit(row: dict[str, Any], since: float, until: float) -> tuple[float, bool]:
    start, end = float(row['started_at']), float(row['ended_at'])
    if end <= since or start >= until:
        return 0.0, False
    if row['exact']:
        return max(0.0, min(end, until) - max(start, since)), False
    if since <= start and end <= until:
        return max(0.0, float(row['seconds'])), False
    # Old completed records may include pauses inside a long wall-clock span.
    # Splitting their seconds by elapsed-time proportion would invent precision.
    return 0.0, True


def window(rows: list[dict[str, Any]], since: float, until: float, *,
           tz: timezone = UTC) -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = {}
    uncertain = 0
    for row in rows:
        seconds, incomplete = _credit(row, since, until)
        if not seconds and not incomplete:
            continue
        uncertain += int(incomplete)
        key = row['run_key']
        if key not in runs:
            runs[key] = {**row, 'seconds': 0.0,
                         'started_at': max(since, float(row['started_at'])),
                         'ended_at': min(until, float(row['ended_at'])), 'incomplete': False}
        run = runs[key]
        run['seconds'] += seconds
        run['started_at'] = min(run['started_at'], max(since, float(row['started_at'])))
        run['ended_at'] = max(run['ended_at'], min(until, float(row['ended_at'])))
        run['incomplete'] |= incomplete
        # A newer verified row can supply real metadata absent from old samples.
        for field in ('item_id', 'item_name', 'item_type', 'series_name', 'client', 'play_method'):
            if not run.get(field) and row.get(field):
                run[field] = row[field]
    plays = sorted(runs.values(), key=lambda r: r['ended_at'], reverse=True)
    titles: dict[tuple[str, str], dict[str, Any]] = {}
    unknown_titles = 0
    first_plays: dict[str, int] = defaultdict(int)
    for run in plays:
        first_plays[datetime.fromtimestamp(run['started_at'], tz).strftime('%Y-%m-%d')] += 1
        kind = str(run.get('item_type') or '').lower()
        name = str((run.get('item_name') if kind == 'movie' else
                    run.get('series_name') or run.get('item_name')) or '')
        family = 'show' if kind in ('episode', 'series') else kind
        if name:
            title = titles.setdefault((family, name), {'title': name, 'plays': 0,
                                                      'item_id': run.get('item_id') or ''})
            title['plays'] += 1
        else:
            unknown_titles += 1
    daily = []
    day = datetime.fromtimestamp(since, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() < until:
        next_day = day + timedelta(days=1)
        start, end = max(since, day.timestamp()), min(until, next_day.timestamp())
        credits = [_credit(row, start, end) for row in rows]
        seconds = sum(pair[0] for pair in credits)
        stamp = day.strftime('%Y-%m-%d')
        daily.append({'day': stamp, 'seconds': seconds, 'hours': seconds/3600,
                      'plays': first_plays.get(stamp, 0),
                      'incomplete': any(pair[1] for pair in credits)})
        day = next_day
    total_seconds = sum(r['seconds'] for r in plays)
    # Full-window old records can be known without a precise midnight split.
    # Keep the remainder explicit instead of fabricating daily allocation.
    unallocated = max(0.0, total_seconds-sum(point['seconds'] for point in daily))
    return {'since': since, 'until': until, 'seconds': total_seconds,
            'daily_unallocated_seconds': unallocated,
            'daily_incomplete': any(point['incomplete'] for point in daily),
            'plays': len(plays), 'uncertain_records': uncertain, 'incomplete': bool(uncertain),
            'recent_plays': plays,
            'titles': sorted(titles.values(), key=lambda r: (-r['plays'], r['title'])),
            'unlocated_title_plays': unknown_titles,
            'series': daily, 'source': 'recorded_viewing'}
