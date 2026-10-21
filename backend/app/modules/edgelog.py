"""Legacy log format decoding only. Traffic collection/ledger retired."""
from __future__ import annotations

import gzip
import math
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
    """Retired compatibility facade. No ingestion, cursor advances or aggregation."""
    def __init__(self, db):
        pass

    def ingest_batch(self, node, payload, *args, **kwargs):
        return {'ok': True, 'retired': True, 'accepted': False}

    def record(self, *args, **kwargs):
        return {'retired': True, 'accepted': False}

    def set_cursor(self, *args, **kwargs):
        return None

    def relink(self, *args, **kwargs):
        return 0

    def status(self):
        return {'retired': True, 'history_preserved': True}
