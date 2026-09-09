"""Edge access log parsing and the per-user traffic ledger.

The problem this solves
-----------------------
``members.traffic_used_bytes`` was the only traffic figure the panel had, and
it is wrong in two independent ways. It is derived from sampled playback
sessions — wall-clock playing time multiplied by bitrate — so it only sees
what the media server knows about. But playback is served by signed direct
links straight from the edge nodes, which the media server never observes at
all. And the counter is reset on a rolling period, so it answers "recently"
rather than "ever".

Acting on that number is dangerous: read as lifetime usage, hundreds of
active accounts look idle. This module replaces the guess with the bytes the
edge actually put on the wire.

Where the truth lives
---------------------
Each node's nginx writes one line per completed request::

    <msec> a=<ip> p=<port> u=<tag> r=<rate> <bytes_sent> <request_time>

with newer nodes appending ``s=<status> <uri>``. ``u`` is the anonymised user
tag the panel itself mints when signing a link, so the panel can map it back
to an account. The parser accepts both shapes and ignores unknown trailing
fields, because the fleet does not upgrade all at once and a parser coupled
to one template silently drops every line from the others.

Idempotent ingestion
--------------------
Logs are read incrementally and may be re-read after a crash, a retry or a
rotation. Double counting inflates a member's usage permanently, so the
cursor records ``(inode, offset)`` per file and the ledger is keyed on
``(day, user, node)`` with additive upserts of only the newly seen bytes.
Rotation is detected by inode change; truncation by a file shorter than the
cursor. Neither replays what was already counted.

Unattributable bytes are kept
-----------------------------
A line whose tag matches no member is not dropped: it is recorded under a
reserved tag. Traffic that cannot be attributed still leaves the building,
and silently discarding it would make the totals disagree with the node's own
counters for reasons nobody could later reconstruct.
"""
from __future__ import annotations

import gzip
import math
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Tag used when a line's user key matches no known member. Not a real tag
#: (those are 10 hex chars), so it can never collide with one.
UNKNOWN_TAG = "unknown"

#: Ceiling on one ingest call, so a first run against a large archive cannot
#: hold the event loop or the database lock for an unbounded time.
MAX_LINES_PER_INGEST = 500_000


def day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class EdgeEvent:
    """One completed edge request."""

    ts: float
    utag: str
    bytes_sent: int
    seconds: float
    status: int | None = None


def parse_line(line: str) -> EdgeEvent | None:
    """Parse one access-log line, or None if it is not a usable record.

    Tolerant by design. Nodes provisioned at different times write different
    shapes, and one node started appending ``s=<status> <uri>`` mid-deployment.
    Positional parsing against a labelled log fails on every line, which is a
    silent total-data-loss failure mode rather than a visible error — so
    labelled fields are read by name and unknown trailing tokens ignored.
    """
    parts = line.split()
    if len(parts) < 4:
        return None
    try:
        ts = float(parts[0])
        day_key(ts)  # Reject non-finite/out-of-range time before aggregation.
    except (ValueError, OverflowError, OSError):
        return None

    utag = ""
    labelled_user = False
    status: int | None = None
    positional: list[str] = []
    for token in parts[1:]:
        if token.startswith("u="):
            labelled_user = True
            utag = token[2:]
        elif token.startswith("s="):
            try:
                status = int(token[2:])
            except ValueError:
                status = None
        elif token[:2] in ("a=", "p=", "r="):
            continue
        else:
            positional.append(token)

    if labelled_user and not utag:
        utag = UNKNOWN_TAG
    if not labelled_user:
        # Oldest shape: "<msec> <tag> <bytes> <secs>".
        if len(positional) < 3:
            return None
        utag, positional = positional[0], positional[1:]
    if len(positional) < 2:
        return None

    # bytes and duration are the first two positional fields; on the newer
    # format the URI follows and is deliberately not read -- a media path is
    # not needed for accounting and would put titles in the database.
    try:
        sent = int(positional[0])
        took = float(positional[1])
    except ValueError:
        return None
    if not utag or utag == "-" or sent <= 0 or not math.isfinite(took):
        return None
    return EdgeEvent(ts=ts, utag=utag, bytes_sent=sent,
                     seconds=max(0.0, took), status=status)


def parse_lines(lines: Iterable[str]) -> Iterator[EdgeEvent]:
    for line in lines:
        event = parse_line(line)
        if event is not None:
            yield event


def open_log(path: str | Path) -> Any:
    """Open a plain or gzipped log for text reading."""
    name = str(path)
    if name.endswith(".gz"):
        return gzip.open(name, "rt", encoding="utf-8", errors="replace")
    return open(name, encoding="utf-8", errors="replace")


def aggregate(events: Iterable[EdgeEvent]) -> dict[tuple[str, str], dict[str, int]]:
    """Fold events into per (day, tag) totals.

    Aggregating before writing keeps one ingest to a handful of upserts
    instead of one per line: a busy node produces thousands of lines per
    rotation, and a row-per-line write pattern would hold the database lock
    long enough to stall the panel.
    """
    out: dict[tuple[str, str], dict[str, int]] = {}
    for event in events:
        key = (day_key(event.ts), event.utag)
        bucket = out.setdefault(key, {"bytes": 0, "requests": 0, "seconds": 0})
        bucket["bytes"] += event.bytes_sent
        bucket["requests"] += 1
        bucket["seconds"] += int(event.seconds)
    return out


class TrafficLedger:
    """Persistent per user x node x day byte ledger.

    Separate from ``usage_daily`` on purpose. That table holds an estimate
    derived from playback sampling; this holds bytes measured on the wire.
    Merging them would destroy the ability to say which number came from
    where, and the whole point of this work is that one of them was trusted
    when it should not have been.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    # -- ingestion ---------------------------------------------------------
    def cursor(self, node: str, path: str) -> dict[str, Any] | None:
        return self._db.one(
            "SELECT * FROM edge_cursors WHERE node=? AND path=?", (node, path))

    def set_cursor(self, node: str, path: str, inode: int, offset: int,
                   now: float | None = None) -> None:
        self._db.execute(
            "INSERT INTO edge_cursors(node,path,inode,offset,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(node,path) DO UPDATE SET "
            "inode=excluded.inode, offset=excluded.offset, "
            "updated_at=excluded.updated_at",
            (node, path, int(inode), int(offset), int(now or time.time())))

    def resume_offset(self, node: str, path: str, inode: int, size: int) -> int:
        """Where to start reading this file.

        Three cases, and getting any of them wrong corrupts the ledger:

        - unseen file: start at 0 and read it all
        - same inode, file grew: resume at the stored offset (no double count)
        - different inode, or shorter than the cursor: the file was rotated or
          truncated, so start at 0 — this is a *different* file that happens
          to share a name
        """
        row = self.cursor(node, path)
        if row is None:
            return 0
        if int(row["inode"]) != int(inode):
            return 0
        offset = int(row["offset"])
        if size < offset:
            return 0
        return offset

    def record(self, node: str, buckets: dict[tuple[str, str], dict[str, int]],
               tag_to_user: dict[str, str]) -> dict[str, int]:
        """Add aggregated buckets to the ledger.

        Additive upserts: re-ingesting the same window is prevented by the
        cursor, and a partially-written batch that is retried adds only what
        it adds. The ledger never stores a computed total that could drift
        from the sum of its rows.
        """
        with self._db.write() as conn:
            return self._record(conn, node, buckets, tag_to_user)

    @staticmethod
    def _record(conn: Any, node: str, buckets: dict, tag_to_user: dict) -> dict[str, int]:
        rows = 0
        total = 0
        unknown = 0
        for (day, utag), values in buckets.items():
            user_id = tag_to_user.get(utag, "")
            if not user_id:
                unknown += values["bytes"]
            conn.execute(
                "INSERT INTO edge_usage_daily"
                "(day,node,utag,emby_user_id,bytes,requests,seconds) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(day,node,utag) DO UPDATE SET "
                "bytes=bytes+excluded.bytes, "
                "requests=requests+excluded.requests, "
                "seconds=seconds+excluded.seconds, "
                # A tag resolves once its member is known; backfilling it
                # here means history becomes attributable retroactively
                # instead of staying orphaned forever.
                "emby_user_id=CASE WHEN excluded.emby_user_id<>'' "
                "THEN excluded.emby_user_id ELSE edge_usage_daily.emby_user_id END",
                (day, node, utag, user_id, values["bytes"],
                 values["requests"], values["seconds"]))
            rows += 1
            total += values["bytes"]
        return {"rows": rows, "bytes": total, "unknown_bytes": unknown}

    def ingest_batch(self, node: str, payload: dict[str, Any],
                     tag_to_user: dict[str, str]) -> dict[str, int]:
        """Commit reported bytes and the confirmed cursor in one transaction.

        New reporters send previous_offset for compare-and-swap, including a
        copytruncate. Legacy single batches still deduplicate by end offset.
        A plain-file rename keeps its inode and therefore its high watermark.
        """
        path = str(payload.get("path") or "")
        inode, offset = int(payload.get("inode") or 0), int(payload.get("offset") or 0)
        previous = payload.get("previous_offset")
        previous = int(previous) if previous is not None else None
        lines = payload.get("lines")
        if (not path or inode <= 0 or offset <= 0 or not isinstance(lines, list)
                or len(lines) > MAX_LINES_PER_INGEST or (previous is not None and previous < 0)):
            raise ValueError("invalid log batch or cursor")
        buckets = aggregate(parse_lines(str(line) for line in lines))
        empty = {"rows": 0, "bytes": 0, "unknown_bytes": 0}
        with self._db.write() as conn:
            # Most recently committed cursor wins after a copytruncate; MAX
            # offset would resurrect the old, longer file's stale position.
            row = conn.execute(
                "SELECT offset FROM edge_cursors WHERE node=? AND inode=? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1", (node, inode)).fetchone()
            current = int(row["offset"]) if row else 0
            if previous is not None and previous != current:
                if offset <= current:
                    return empty
                raise ValueError("log cursor changed; fetch cursors and retry")
            if previous is None and offset <= current:
                return empty
            if offset == current:
                return empty
            result = self._record(conn, node, buckets, tag_to_user)
            # Synchronise renamed aliases too, so truncation cannot expose an
            # older position via the previous path.
            conn.execute("UPDATE edge_cursors SET offset=?,updated_at=? WHERE node=? AND inode=?",
                         (offset, time.time(), node, inode))
            conn.execute(
                "INSERT INTO edge_cursors(node,path,inode,offset,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(node,path) DO UPDATE SET inode=excluded.inode,offset=excluded.offset,"
                "updated_at=excluded.updated_at", (node, path, inode, offset, time.time()))
            return result

    def relink(self, tag_to_user: dict[str, str]) -> int:
        """Attach newly-known tags to rows ingested before the member existed."""
        updated = 0
        with self._db.write() as conn:
            for utag, user_id in tag_to_user.items():
                if not user_id:
                    continue
                cur = conn.execute(
                    "UPDATE edge_usage_daily SET emby_user_id=? "
                    "WHERE utag=? AND emby_user_id=''", (user_id, utag))
                updated += cur.rowcount or 0
        return updated

    # -- queries -----------------------------------------------------------
    def totals_for_users(self, days: int | None = None,
                         now: float | None = None) -> dict[str, int]:
        """user id -> bytes over the window (all history when days is None)."""
        if days is None:
            rows = self._db.query(
                "SELECT emby_user_id, SUM(bytes) AS b FROM edge_usage_daily "
                "WHERE emby_user_id<>'' GROUP BY emby_user_id")
        else:
            rows = self._db.query(
                "SELECT emby_user_id, SUM(bytes) AS b FROM edge_usage_daily "
                "WHERE emby_user_id<>'' AND day>=? GROUP BY emby_user_id",
                (self._since(days, now),))
        return {r["emby_user_id"]: int(r["b"] or 0) for r in rows}

    def summary_for_users(self, now: float | None = None) -> dict[str, dict[str, int]]:
        """Per user: 7-day, 30-day and lifetime bytes, in one pass.

        One query rather than three: the member list renders hundreds of rows
        and a per-row lookup is what makes a list feel broken.
        """
        day7 = self._since(7, now)
        day30 = self._since(30, now)
        rows = self._db.query(
            "SELECT emby_user_id, "
            "SUM(CASE WHEN day>=? THEN bytes ELSE 0 END) AS b7, "
            "SUM(CASE WHEN day>=? THEN bytes ELSE 0 END) AS b30, "
            "SUM(bytes) AS total FROM edge_usage_daily "
            "WHERE emby_user_id<>'' GROUP BY emby_user_id",
            (day7, day30))
        return {
            r["emby_user_id"]: {
                "bytes_7d": int(r["b7"] or 0),
                "bytes_30d": int(r["b30"] or 0),
                "bytes_total": int(r["total"] or 0),
            } for r in rows
        }

    def member_detail(self, user_id: str, days: int = 30,
                      now: float | None = None) -> dict[str, Any]:
        since = self._since(days, now)
        by_day = self._db.query(
            "SELECT day, SUM(bytes) AS b, SUM(requests) AS r "
            "FROM edge_usage_daily WHERE emby_user_id=? AND day>=? "
            "GROUP BY day ORDER BY day", (user_id, since))
        by_node = self._db.query(
            "SELECT node, SUM(bytes) AS b, SUM(requests) AS r "
            "FROM edge_usage_daily WHERE emby_user_id=? AND day>=? "
            "GROUP BY node ORDER BY b DESC", (user_id, since))
        return {
            "days": days,
            "by_day": [{"day": r["day"], "bytes": int(r["b"] or 0),
                        "requests": int(r["r"] or 0)} for r in by_day],
            "by_node": [{"node": r["node"], "bytes": int(r["b"] or 0),
                         "requests": int(r["r"] or 0)} for r in by_node],
        }

    def node_totals(self, days: int = 30, now: float | None = None
                    ) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT node, SUM(bytes) AS b, SUM(requests) AS r "
            "FROM edge_usage_daily WHERE day>=? GROUP BY node ORDER BY b DESC",
            (self._since(days, now),))
        return [{"node": r["node"], "bytes": int(r["b"] or 0),
                 "requests": int(r["r"] or 0)} for r in rows]

    def unattributed(self, days: int = 30, now: float | None = None
                     ) -> dict[str, int]:
        row = self._db.one(
            "SELECT SUM(bytes) AS b, COUNT(*) AS n FROM edge_usage_daily "
            "WHERE emby_user_id='' AND day>=?", (self._since(days, now),))
        return {"bytes": int((row or {}).get("b") or 0),
                "rows": int((row or {}).get("n") or 0)}

    def status(self) -> dict[str, Any]:
        row = self._db.one(
            "SELECT COUNT(*) AS n, SUM(bytes) AS b, MIN(day) AS first_day, "
            "MAX(day) AS last_day FROM edge_usage_daily")
        cursors = self._db.query(
            "SELECT node, path, offset, updated_at FROM edge_cursors "
            "ORDER BY node, path")
        return {
            "rows": int((row or {}).get("n") or 0),
            "bytes": int((row or {}).get("b") or 0),
            "first_day": (row or {}).get("first_day"),
            "last_day": (row or {}).get("last_day"),
            "cursors": [dict(c) for c in cursors],
        }

    @staticmethod
    def _since(days: int, now: float | None = None) -> str:
        base = now if now is not None else time.time()
        return day_key(base - max(0, days - 1) * 86400)
