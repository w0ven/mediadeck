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
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.db import Database
from app.modules.groups import needs_duration, needs_traffic

MAX_DAYS = 366


def _day_list(days: int, end: datetime | None = None) -> list[str]:
    end = end or datetime.now(UTC)
    return [(end - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days - 1, -1, -1)]


class StatsService:
    def __init__(self, db: Database) -> None:
        self._db = db

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
        audit = self._db.execute(
            "DELETE FROM audit_log WHERE ts < ?", (cutoff_ts,))
        return {"play_events": events, "usage_daily": usage, "audit_log": audit}
