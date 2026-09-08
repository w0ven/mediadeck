"""Operational statistics.

Answers the questions an operator actually asks, rather than dumping raw
counters on a page:

* Is the service growing or shrinking?           (trend over N days)
* Who is costing me the most bandwidth?          (top consumers)
* What is worth keeping in the library?          (top titles)
* Is transcoding hurting me?                     (direct vs transcode ratio)
* What is about to lapse?                        (expiring soon)

Every query is bounded and indexed. The tables it reads grow forever, so an
unbounded `SELECT *` here would quietly become the slowest thing in the panel
after a few months of playback history.
"""
from __future__ import annotations

import time
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.db import Database
from app.modules.groups import needs_duration, needs_traffic

MAX_DAYS = 366


def _day_list(days: int, end: datetime | None = None) -> list[str]:
    end = end or datetime.now(UTC)
    return [(end - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days - 1, -1, -1)]


def legacy_watch_fingerprint(rows: list[dict[str, Any]]) -> str:
    records = sorted((str(r['event_id']), str(r['emby_user_id']), int(r['seconds']),
                      int(r['started_at']), int(r['ended_at'])) for r in rows)
    return hashlib.sha256(json.dumps(records, separators=(',', ':')).encode()).hexdigest()


class StatsService:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._live_watch = lambda: []

    def bind_live_watch(self, provider: Any) -> None:
        self._live_watch = provider

    def watch_summary(self, user_id: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        row = self._db.one(
            "SELECT SUM(CASE WHEN started_at>=? THEN seconds ELSE 0 END) AS day,"
            "SUM(seconds) AS month FROM ("
            "SELECT seconds,started_at FROM play_events WHERE emby_user_id=? AND started_at>=? "
            "UNION ALL SELECT seconds,started_at FROM watch_legacy_events WHERE emby_user_id=? AND started_at>=?)",
            (int(now)-86400, user_id, int(now)-30*86400, user_id, int(now)-30*86400)) or {}
        total = self._db.one("SELECT seconds,first_at FROM watch_totals WHERE emby_user_id=?", (user_id,)) or {}
        legacy = self._db.one("SELECT seconds,first_at,cutoff_at FROM watch_legacy_baselines WHERE emby_user_id=?", (user_id,)) or {}
        day, month = int(row.get("day") or 0), int(row.get("month") or 0)
        recorded = int(total.get("seconds") or 0) + int(legacy.get("seconds") or 0)
        starts = [int(r["first_at"]) for r in (total, legacy) if r.get("first_at") is not None]
        for live in self._live_watch() or []:
            if live.get("user_id") != user_id:
                continue
            seconds, start = int(live.get("seconds") or 0), int(live.get("started_at") or now)
            recorded += seconds
            starts.append(start)
            day += seconds if start >= now-86400 else 0
            month += seconds if start >= now-30*86400 else 0
        return {"seconds_24h": day, "seconds_30d": month, "recorded_seconds": recorded,
                "first_at": min(starts) if starts else None,
                "legacy_seconds": int(legacy.get("seconds") or 0),
                "window_basis": "session_start", "source": "sampled_playing_seconds"}

    def recent_watches(self, user_id: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._db.query("SELECT item_name,series_name,seconds,started_at FROM play_events "
                              "WHERE emby_user_id=? ORDER BY started_at DESC LIMIT ?", (user_id, max(1,min(limit,10))))

    def import_legacy_watch(self, rows: list[dict[str, Any]], *, source: str) -> int:
        """Explicit one-source import of completed, non-overlapping old sessions.

        Event identities are content hashes, not mutable SQLite rowids. A fixed
        source fingerprint makes retrying the same export harmless; another
        source cannot silently add the same history again.
        """
        first = self._db.one("SELECT MIN(first_at) AS t FROM watch_totals") or {}
        boundary = first.get("t")
        if boundary is None or not source or len(rows) > 200000:
            raise ValueError("缺少 Deck 统计起点或可信旧历史来源")
        if source != legacy_watch_fingerprint(rows):
            raise ValueError('旧历史文件指纹不符，未导入')
        clean, seen = [], set()
        for r in rows:
            uid, event = str(r['emby_user_id']), str(r['event_id'])
            seconds, start, end = int(r['seconds']), int(r['started_at']), int(r['ended_at'])
            if not event or event in seen or not 0 <= seconds <= end-start or not 0 <= start < end <= int(boundary):
                raise ValueError("旧历史重复、时间不明或与 Deck 重叠")
            seen.add(event)
            clean.append((event,uid,seconds,start,end,source))
        with self._db.write() as conn:
            completed = conn.execute("SELECT value FROM meta WHERE key='watch_legacy_import_source'").fetchone()
            if completed:
                if completed[0] != source:
                    raise ValueError('已完成旧历史导入，不能叠加另一份历史')
                return 0
            if conn.execute('SELECT 1 FROM watch_legacy_baselines WHERE source<>? LIMIT 1',(source,)).fetchone():
                raise ValueError('已有不同旧历史基线，禁止重复叠加')
            for uid in {r[1] for r in clean}:
                if not conn.execute('SELECT 1 FROM members WHERE emby_user_id=?',(uid,)).fetchone():
                    raise ValueError('旧历史包含已清退账号，不恢复用户')
            added = 0
            for record in clean:
                prior = conn.execute('SELECT event_id,emby_user_id,seconds,started_at,ended_at,source FROM watch_legacy_events WHERE event_id=?',(record[0],)).fetchone()
                if prior:
                    if tuple(prior)!=record:
                        raise ValueError('旧记录身份冲突，未导入')
                    continue
                conn.execute('INSERT INTO watch_legacy_events VALUES(?,?,?,?,?,?)',record)
                added += 1
            for uid in {r[1] for r in clean}:
                conn.execute('INSERT INTO watch_legacy_baselines SELECT emby_user_id,SUM(seconds),MIN(started_at),MAX(ended_at),? FROM watch_legacy_events WHERE emby_user_id=? GROUP BY emby_user_id ON CONFLICT(emby_user_id) DO NOTHING',(source,uid))
            conn.execute("INSERT INTO meta(key,value) VALUES('watch_legacy_import_source',?)",(source,))
        return added

    # -- headline ------------------------------------------------------------
    def overview(self, days: int = 30) -> dict[str, Any]:
        days = max(1, min(days, MAX_DAYS))
        now = int(time.time())
        since = now - days * 86400
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        members = self._db.query("SELECT * FROM members")
        groups = {g["id"]: g for g in self._db.query("SELECT * FROM groups")}

        active = expired = exhausted = suspended = 0
        expiring_7d = []
        for m in members:
            group = groups.get(m.get("group_id") or "")
            mode = group["billing_mode"] if group else "none"
            status = m.get("status") or "active"
            if status in ("suspended", "pending"):
                suspended += 1
            elif group and needs_duration(mode) and \
                    m.get("expires_at") and now >= m["expires_at"]:
                expired += 1
            elif group and needs_traffic(mode) and \
                    group["traffic_quota_bytes"] and \
                    m.get("traffic_used_bytes", 0) >= group["traffic_quota_bytes"]:
                exhausted += 1
            else:
                active += 1

            if group and m.get("expires_at") and 0 < m["expires_at"] - now <= 7 * 86400:
                expiring_7d.append({
                    "user_id": m["emby_user_id"],
                    "username": m.get("username"),
                    "group": group["name"],
                    "expires_at": m["expires_at"],
                    "days_left": max(0, int((m["expires_at"] - now) // 86400)),
                })

        totals = self._db.one(
            "SELECT COALESCE(SUM(bytes),0) AS bytes, COALESCE(SUM(seconds),0) AS secs,"
            " COALESCE(SUM(plays),0) AS plays, COALESCE(SUM(transcodes),0) AS trans"
            " FROM usage_daily WHERE day >= ?",
            (datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%d"),)) or {}
        today_row = self._db.one(
            "SELECT COALESCE(SUM(bytes),0) AS bytes, COALESCE(SUM(seconds),0) AS secs,"
            " COALESCE(SUM(plays),0) AS plays FROM usage_daily WHERE day = ?",
            (today,)) or {}

        plays = int(totals.get("plays") or 0)
        transcodes = int(totals.get("trans") or 0)
        return {
            "window_days": days,
            "members": {
                "total": len(members),
                "active": active,
                "expired": expired,
                "exhausted": exhausted,
                "suspended": suspended,
            },
            "traffic": {
                "window_bytes": int(totals.get("bytes") or 0),
                "today_bytes": int(today_row.get("bytes") or 0),
                "window_hours": round(int(totals.get("secs") or 0) / 3600, 1),
                "today_hours": round(int(today_row.get("secs") or 0) / 3600, 1),
            },
            "playback": {
                "window_plays": plays,
                "today_plays": int(today_row.get("plays") or 0),
                "transcode_plays": transcodes,
                # The number that decides whether the CPU is being wasted.
                "direct_ratio": round((plays - transcodes) / plays * 100, 1) if plays else None,
            },
            "expiring_7d": sorted(expiring_7d, key=lambda x: x["expires_at"])[:20],
            "devices": self._db.one(
                "SELECT COUNT(*) AS n FROM devices WHERE blocked=0")["n"],
        }

    # -- trends --------------------------------------------------------------
    def daily_series(self, days: int = 30) -> list[dict[str, Any]]:
        """Zero-filled series: a gap in the data must render as a zero, not as
        a missing point that makes a chart lie about continuity."""
        days = max(1, min(days, MAX_DAYS))
        wanted = _day_list(days)
        rows = {
            r["day"]: r for r in self._db.query(
                "SELECT day, SUM(bytes) AS bytes, SUM(seconds) AS secs,"
                " SUM(plays) AS plays, COUNT(DISTINCT emby_user_id) AS users"
                " FROM usage_daily WHERE day >= ? GROUP BY day", (wanted[0],))
        }
        return [{
            "day": d,
            "bytes": int((rows.get(d) or {}).get("bytes") or 0),
            "hours": round(int((rows.get(d) or {}).get("secs") or 0) / 3600, 2),
            "plays": int((rows.get(d) or {}).get("plays") or 0),
            "users": int((rows.get(d) or {}).get("users") or 0),
        } for d in wanted]

    # -- leaderboards --------------------------------------------------------
    def top_users(self, days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
        days = max(1, min(days, MAX_DAYS))
        since = _day_list(days)[0]
        rows = self._db.query(
            "SELECT u.emby_user_id, COALESCE(m.username,'') AS username,"
            " COALESCE(m.group_id,'') AS group_id,"
            " SUM(u.bytes) AS bytes, SUM(u.seconds) AS secs, SUM(u.plays) AS plays"
            " FROM usage_daily u LEFT JOIN members m ON m.emby_user_id=u.emby_user_id"
            " WHERE u.day >= ? GROUP BY u.emby_user_id"
            " ORDER BY secs DESC, bytes DESC LIMIT ?", (since, max(1, min(limit, 200))))
        return [{
            "user_id": r["emby_user_id"],
            "username": r["username"] or r["emby_user_id"][:8],
            "group_id": r["group_id"],
            "bytes": int(r["bytes"] or 0),
            "hours": round(int(r["secs"] or 0) / 3600, 1),
            "plays": int(r["plays"] or 0),
        } for r in rows]

    def top_watchers(self, hours: int = 24, limit: int = 10) -> list[dict[str, Any]]:
        """Rolling watch-time ranking from play_events, not calendar days or bytes."""
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            hours = 24
        hours = max(1, min(hours, 24 * MAX_DAYS))
        since = int(time.time()) - hours * 3600
        rows = self._db.query(
            "SELECT p.emby_user_id,m.username,SUM(p.seconds) AS secs,COUNT(*) AS plays "
            "FROM (SELECT emby_user_id,seconds FROM play_events WHERE started_at>=? "
            "UNION ALL SELECT emby_user_id,seconds FROM watch_legacy_events WHERE started_at>=?) p "
            "JOIN members m ON m.emby_user_id=p.emby_user_id GROUP BY p.emby_user_id",
            (since,since))
        by_user = {r['emby_user_id']:r for r in rows}
        for live in self._live_watch() or []:
            uid = str(live.get('user_id') or '')
            if int(live.get('started_at') or 0) < since:
                continue
            if uid not in by_user:
                member = self._db.one('SELECT username FROM members WHERE emby_user_id=?',(uid,))
                if not member:
                    continue
                by_user[uid] = {'emby_user_id':uid,'username':member['username'],'secs':0,'plays':0}
            by_user[uid]['secs'] += int(live.get('seconds') or 0)
        ordered = sorted(by_user.values(),key=lambda r:(-int(r['secs']),-int(r['plays']),r['emby_user_id']))[:max(1,min(int(limit),50))]
        return [{'user_id':r['emby_user_id'],'username':r['username'] or r['emby_user_id'][:8],
                 'hours':round(int(r['secs'] or 0)/3600,1),'seconds':int(r['secs'] or 0),
                 'plays':int(r['plays'] or 0)} for r in ordered]

    def top_titles(self, days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
        days = max(1, min(days, MAX_DAYS))
        since = int(time.time()) - days * 86400
        rows = self._db.query(
            "SELECT item_name, series_name, item_type, COUNT(*) AS plays,"
            " COUNT(DISTINCT emby_user_id) AS viewers, SUM(seconds) AS secs,"
            " SUM(bytes) AS bytes FROM play_events WHERE started_at >= ?"
            " GROUP BY COALESCE(NULLIF(series_name,''), item_name)"
            " ORDER BY plays DESC, secs DESC LIMIT ?",
            (since, max(1, min(limit, 200))))
        return [{
            "title": r["series_name"] or r["item_name"],
            "type": r["item_type"],
            "plays": int(r["plays"] or 0),
            "viewers": int(r["viewers"] or 0),
            "hours": round(int(r["secs"] or 0) / 3600, 1),
            "bytes": int(r["bytes"] or 0),
        } for r in rows]

    def client_breakdown(self, days: int = 30) -> list[dict[str, Any]]:
        since = int(time.time()) - max(1, min(days, MAX_DAYS)) * 86400
        rows = self._db.query(
            "SELECT client, COUNT(*) AS plays, SUM(seconds) AS secs"
            " FROM play_events WHERE started_at >= ? AND client<>''"
            " GROUP BY client ORDER BY plays DESC LIMIT 20", (since,))
        total = sum(int(r["plays"] or 0) for r in rows) or 1
        return [{
            "client": r["client"],
            "plays": int(r["plays"] or 0),
            "hours": round(int(r["secs"] or 0) / 3600, 1),
            "percent": round(int(r["plays"] or 0) / total * 100, 1),
        } for r in rows]

    def node_breakdown(self, days: int = 30) -> list[dict[str, Any]]:
        since = int(time.time()) - max(1, min(days, MAX_DAYS)) * 86400
        rows = self._db.query(
            "SELECT COALESCE(NULLIF(node,''),'(origin)') AS node, COUNT(*) AS plays,"
            " SUM(bytes) AS bytes FROM play_events WHERE started_at >= ?"
            " GROUP BY node ORDER BY bytes DESC LIMIT 20", (since,))
        total = sum(int(r["plays"] or 0) for r in rows) or 1
        return [{"node": r["node"], "plays": int(r["plays"] or 0),
                 "bytes": int(r["bytes"] or 0),
                 "percent": round(int(r["plays"] or 0) / total * 100, 1)}
                for r in rows]

    def play_method_breakdown(self, days: int = 30) -> dict[str, Any]:
        """Direct play vs transcode: the cost signal that actually matters."""
        since = int(time.time()) - max(1, min(days, MAX_DAYS)) * 86400
        rows = self._db.query(
            "SELECT LOWER(COALESCE(NULLIF(play_method,''),'(unknown)')) AS method,"
            " COUNT(*) AS plays, SUM(seconds) AS secs, SUM(bytes) AS bytes"
            " FROM play_events WHERE started_at >= ? GROUP BY method", (since,))
        transcode = 0
        direct = 0
        unknown = 0
        methods = []
        for r in rows:
            method = r["method"] or "(unknown)"
            plays = int(r["plays"] or 0)
            methods.append({
                "method": method,
                "plays": plays,
                "hours": round(int(r["secs"] or 0) / 3600, 1),
                "bytes": int(r["bytes"] or 0),
            })
            if "transcode" in method:
                transcode += plays
            elif method in ("directplay", "directstream", "direct"):
                direct += plays
            else:
                unknown += plays
        total = transcode + direct + unknown
        return {
            "total": total,
            "direct": direct,
            "transcode": transcode,
            "unknown": unknown,
            "direct_ratio": round(direct / total * 100, 1) if total else None,
            "transcode_ratio": round(transcode / total * 100, 1) if total else None,
            "methods": methods,
        }

    def hours_this_month(self) -> dict[str, float]:
        """Watch hours per member since the 1st, as one query.

        The member list renders hundreds of rows; asking per row is how that
        page starts taking seconds. Callers get a plain dict and decide what a
        missing member means (zero, not unknown).
        """
        since = datetime.now(UTC).replace(day=1).strftime("%Y-%m-%d")
        rows = self._db.query(
            "SELECT emby_user_id, SUM(seconds) AS secs FROM usage_daily"
            " WHERE day >= ? GROUP BY emby_user_id", (since,))
        return {str(r["emby_user_id"]): round(int(r["secs"] or 0) / 3600, 1)
                for r in rows}

    def member_detail(self, user_id: str, days: int = 30) -> dict[str, Any]:
        days = max(1, min(days, MAX_DAYS))
        since_day = _day_list(days)[0]
        since_ts = int(time.time()) - days * 86400
        series = {
            r["day"]: r for r in self._db.query(
                "SELECT day, bytes, seconds, plays FROM usage_daily"
                " WHERE emby_user_id=? AND day >= ?", (user_id, since_day))
        }
        return {
            "series": [{
                "day": d,
                "bytes": int((series.get(d) or {}).get("bytes") or 0),
                "hours": round(int((series.get(d) or {}).get("seconds") or 0) / 3600, 2),
                "plays": int((series.get(d) or {}).get("plays") or 0),
            } for d in _day_list(days)],
            "recent_plays": self._db.query(
                "SELECT item_name, series_name, client, play_method, node, seconds,"
                " bytes, started_at FROM play_events WHERE emby_user_id=?"
                " AND started_at >= ? ORDER BY started_at DESC LIMIT 50",
                (user_id, since_ts)),
            "devices": self._db.query(
                "SELECT * FROM devices WHERE emby_user_id=?"
                " ORDER BY last_seen_at DESC LIMIT 50", (user_id,)),
        }

    # -- retention -----------------------------------------------------------
    def prune(self, keep_days: int = 400) -> dict[str, int]:
        """Drop history beyond the retention window.

        play_events is the fastest-growing table in the panel; without this the
        stats queries degrade steadily and the database grows without bound.
        """
        cutoff_ts = int(time.time()) - max(30, keep_days) * 86400
        cutoff_day = datetime.fromtimestamp(cutoff_ts, UTC).strftime("%Y-%m-%d")
        events = self._db.execute(
            "DELETE FROM play_events WHERE started_at < ?", (cutoff_ts,))
        usage = self._db.execute(
            "DELETE FROM usage_daily WHERE day < ?", (cutoff_day,))
        self._db.execute('DELETE FROM watch_legacy_events WHERE started_at < ?', (cutoff_ts,))
        audit = self._db.execute(
            "DELETE FROM audit_log WHERE ts < ?", (cutoff_ts,))
        return {"play_events": events, "usage_daily": usage, "audit_log": audit}
