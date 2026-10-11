"""Playback existence evidence, independent of exact viewing/accounting."""
from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing, suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def recent_playback(db: Any, ids: set[str], *, now: float,
                    live: list[dict[str, Any]], history_path: str = '',
                    history_timezone: str = 'Asia/Shanghai') -> dict[str, dict[str, Any]]:
    out = {uid: {'at': None, 'sources': [], 'as_of': now,
                 'history_status': 'not_configured', 'available': False} for uid in ids}

    def add(uid: str, stamp: Any, source: str) -> None:
        if uid not in out:
            return
        try:
            at = float(stamp)
            if not math.isfinite(at) or at <= 0 or at > now+60:
                return
        except (ValueError, TypeError, OverflowError):
            return
        row = out[uid]
        if row['at'] is None or at > row['at']:
            row.update(at=at, sources=[source], available=True)
        elif at == row['at'] and source not in row['sources']:
            row['sources'].append(source)

    try:
        rows = db.query("""SELECT emby_user_id,last_at AS stamp,'Deck进度记录' AS source FROM watch_verified_totals
            UNION ALL SELECT emby_user_id,last_at,'Deck采样记录' FROM watch_sample_totals
            UNION ALL SELECT emby_user_id,last_at,'Deck播放事件' FROM watch_totals
            UNION ALL SELECT emby_user_id,cutoff_at,'已有播放记录' FROM watch_legacy_baselines
            UNION ALL SELECT emby_user_id,MAX(ended_at),'Deck播放事件' FROM play_events GROUP BY emby_user_id""")
        for row in rows:
            add(str(row['emby_user_id']), row['stamp'], row['source'])
    except Exception:  # noqa: BLE001 - Optional history cannot break the member list.
        for row in out.values():
            row['deck_status'] = 'unavailable'
    # Older read-only snapshots need not contain the optional diagnostic table.
    with suppress(Exception):
        for row in db.query("SELECT emby_user_id,json_extract(summary_json,'$.attempt_at') AS stamp "
                            "FROM watch_diagnostics"):
            add(str(row['emby_user_id']), row['stamp'], '播放上报/尝试')
    for row in live:
        # An actual bound playing item/report, not login, traffic or heartbeat.
        if row.get('watch_reason') not in ('idle', 'stopped'):
            add(str(row.get('user_id')), row.get('playback_attempt_at'), '当前播放上报')
    if history_path and ids:
        state = 'unavailable'
        try:
            zone = ZoneInfo(history_timezone)
            deadline = time.monotonic()+0.15
            with closing(sqlite3.connect(Path(history_path).resolve().as_uri()+'?mode=ro',
                                         uri=True, timeout=0.02)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                marks = ','.join('?' for _ in ids)
                rows = conn.execute('SELECT UserId,MAX(DateCreated) FROM PlaybackActivity '
                                    f'WHERE UserId IN ({marks}) GROUP BY UserId', tuple(ids)).fetchall()
                for uid, stamp in rows:
                    try:
                        parsed = datetime.fromisoformat(str(stamp))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=zone)
                        add(str(uid), parsed.timestamp(), 'Emby播放记录')
                    except (ValueError, TypeError, OverflowError):
                        state = 'partial'
                if state != 'partial':
                    state = 'available'
        except (OSError, sqlite3.Error, ValueError, KeyError):
            pass
        finally:
            for row in out.values():
                row['history_status'] = state
    return out
