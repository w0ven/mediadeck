"""Verified, silent watch rewards; points and growth commit with daily watermarks.

The proof collector is unchanged. An insert-only outbox captures proofs *after*
activation, including late delivery, without scanning historical display totals.
Union intervals survive evidence retention and process restarts. No reader pays.
"""
from __future__ import annotations

import time
from datetime import datetime

from app.modules.economy_rules import BEIJING, day_bounds, economy_write

WATCH_RULE = "activity-watch-v1"
LEVEL_THRESHOLDS = (0, 120, 360, 900, 1800, 3600, 7200)
LEVEL_NAMES = ("布衣", "游侠", "逍遥", "剑仙", "真君", "道尊", "帝君")
LEVEL_TITLES = ("偷得浮生半日闲", "一身侠气走江湖", "醉卧美人膝", "一剑霜寒十四州",
                "笑问青天几时老", "坐看万古浮沉", "一念山河定")
LEVEL_PRICES = (80, 75, 70, 65, 60, 55, 50)
LEVEL_ICONS = ('🌱', '🗡', '🍃', '⚔', '☁', '☯', '👑')


def reward_total(seconds):
    steps = min(12, max(0, int(seconds // 300)))
    return steps, min(steps, 4) * 8 + max(0, steps - 4) * 2, steps * 5


def level_for(growth):
    index = max(i for i, threshold in enumerate(LEVEL_THRESHOLDS) if max(0, growth) >= threshold)
    return {"level": index + 1, "name": LEVEL_NAMES[index], "title": LEVEL_TITLES[index],
            "icon": LEVEL_ICONS[index], "growth": growth, "threshold": LEVEL_THRESHOLDS[index],
            "next_threshold": LEVEL_THRESHOLDS[index+1] if index < 6 else None,
            "renewal_price": LEVEL_PRICES[index]}


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def verified_seconds(db, user, start, end):
    """Union verified wall-clock proofs, not per-device sums or display totals."""
    rows = db.query(
        "SELECT started_at,ended_at FROM watch_verified_samples "
        "WHERE emby_user_id=? AND ended_at>? AND started_at<?",
        (str(user), start, end))
    return sum(b-a for a, b in merge_intervals(
        (max(start, r["started_at"]), min(end, r["ended_at"])) for r in rows))


class ActivityService:
    def __init__(self, db, points):
        self.db, self.points = db, points

    def activate(self, now=None):
        """Explicit writer, used only by the enabled job. Never backdate activation."""
        now = time.time() if now is None else now
        with economy_write(self.db) as conn:
            conn.execute("INSERT OR IGNORE INTO activity_rules VALUES(1,?,?)", (now, WATCH_RULE))
            return dict(conn.execute("SELECT * FROM activity_rules WHERE id=1").fetchone())

    def settle(self, now=None, batch_size=2000):
        """One bounded atomic batch. Retry drains more; even late overlaps are safe."""
        now = time.time() if now is None else now
        with economy_write(self.db) as conn:
            rule = conn.execute("SELECT * FROM activity_rules WHERE id=1").fetchone()
            if not rule:
                return {"ok": True, "active": False, "samples": 0, "points": 0, "growth": 0}
            rows = conn.execute(
                "SELECT * FROM activity_watch_pending WHERE ended_at<=? ORDER BY ended_at "
                "LIMIT ?", (now, max(1, min(10000, int(batch_size))))).fetchall()
            changed = set()
            for row in rows:
                start, end = max(row["started_at"], rule["effective_at"]), row["ended_at"]
                while start < end:
                    day, _, midnight = day_bounds(start)
                    stop = min(end, midnight)
                    self._union(conn, row["emby_user_id"], day, start, stop)
                    changed.add((row["emby_user_id"], day))
                    start = stop
                conn.execute("DELETE FROM activity_watch_pending WHERE run_key=? AND ended_at=?",
                             (row["run_key"], row["ended_at"]))
            paid = grown = 0
            for uid, day in sorted(changed):
                seconds = conn.execute(
                    "SELECT COALESCE(SUM(ended_at-started_at),0) FROM activity_watch_intervals "
                    "WHERE emby_user_id=? AND day=?", (uid, day)).fetchone()[0]
                steps, points, growth = reward_total(seconds)
                prior = conn.execute("SELECT * FROM activity_watch_days WHERE emby_user_id=? AND day=?",
                                     (uid, day)).fetchone()
                delta = points - (prior["points"] if prior else 0)
                growth_delta = growth - (prior["growth"] if prior else 0)
                if delta:
                    ref = f"{WATCH_RULE}:{day}:{steps}"
                    self.points._apply(conn, uid, delta, "watch.reward", ref, "activity", int(now))
                    conn.execute("INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) "
                                 "VALUES(?,?,?,?,?)", (uid, growth_delta, "watch.reward", ref, int(now)))
                    paid += delta
                    grown += growth_delta
                conn.execute(
                    "INSERT INTO activity_watch_days VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(emby_user_id,day) DO UPDATE SET "
                    "verified_seconds=excluded.verified_seconds,paid_steps=excluded.paid_steps,"
                    "points=excluded.points,growth=excluded.growth,updated_at=excluded.updated_at",
                    (uid, day, seconds, steps, points, growth, WATCH_RULE, int(now)))
            pending = conn.execute("SELECT COUNT(*) FROM activity_watch_pending").fetchone()[0]
            return {"ok": True, "active": True, "samples": len(rows), "days": len(changed),
                    "points": paid, "growth": grown, "pending": pending}

    @staticmethod
    def _union(conn, uid, day, start, end):
        overlap = conn.execute(
            "SELECT started_at,ended_at FROM activity_watch_intervals "
            "WHERE emby_user_id=? AND day=? AND ended_at>=? AND started_at<=?",
            (uid, day, start, end)).fetchall()
        if overlap:
            conn.execute("DELETE FROM activity_watch_intervals WHERE emby_user_id=? AND day=? "
                         "AND ended_at>=? AND started_at<=?", (uid, day, start, end))
            start = min(start, *(r["started_at"] for r in overlap))
            end = max(end, *(r["ended_at"] for r in overlap))
        conn.execute("INSERT INTO activity_watch_intervals VALUES(?,?,?,?)", (uid, day, start, end))

    def summary(self, user, now=None):
        """Read-only single-snapshot status for Web/Bot, never settlement on view."""
        now = time.time() if now is None else now
        uid = str(user)
        day, _, _ = day_bounds(now)
        with self.db.snapshot():
            rule = self.db.one("SELECT * FROM activity_rules WHERE id=1")
            growth = self.db.one("SELECT COALESCE(SUM(delta),0) AS n FROM growth_ledger "
                                 "WHERE emby_user_id=?", (uid,))["n"]
            today = self.db.one("SELECT * FROM activity_watch_days WHERE emby_user_id=? AND day=?",
                                (uid, day)) or {"verified_seconds": 0, "points": 0, "growth": 0}
            # Rolling 30*24 hours, clipped at both endpoints; count Beijing dates.
            intervals = self.db.query("SELECT day,started_at,ended_at FROM activity_watch_intervals "
                                      "WHERE emby_user_id=? AND ended_at>? AND started_at<?",
                                      (uid, now-30*86400, now))
            by_day = {}
            for interval in intervals:
                seconds = max(0, min(now, interval["ended_at"]) -
                              max(now-30*86400, interval["started_at"]))
                key = interval["day"]
                by_day[key] = by_day.get(key, 0) + seconds
            watched = sum(by_day.values())
            days = sum(seconds >= 600 for seconds in by_day.values())
            return {"active": bool(rule), "effective_at": rule["effective_at"] if rule else None,
                    "day": day, "level": level_for(growth), "today": today,
                    "qualification": {"seconds": watched, "days": days,
                                      "required_seconds": 4800, "required_days": 3,
                                      "eligible": watched >= 4800 and days >= 3},
                    "as_of": datetime.fromtimestamp(now, BEIJING).isoformat()}
