"""Measured traffic ledger — kernel outbound IP bytes, idempotent across nodes.

Bytes are **kernel outbound IP-packet bytes** as counted by nft on
registered 4-tuples: headers included, possible retransmits included.
They are not HTTP content length and not a NIC frame capture.

Stream identity is ``(node, conn_id, generation)``. ``boot_id`` is only
the reporting process epoch (spool / ack). A live connection that
survives an agent restart keeps the same conn_id and must not be billed
from zero.

Old ``edge_usage_daily`` / ``members.traffic_used_bytes`` stay statistical.
Cutover onto this ledger is an explicit operator decision.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from app.core.db import Database
from app.modules.members import period_start

UNIT = "kernel_outbound_ip_bytes"
SOURCE = "measured"
UNKNOWN_USER = ""
BILLABLE_COVERAGE = frozenset({"observed", "retained"})
# First observation after a month-crossing gap longer than this is not
# attributed to the new month (and not invented for the old one).
MONTH_GAP_SECONDS = 3 * 3600


def month_key(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), UTC)
    return dt.strftime("%Y-%m")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _billable(sample: dict[str, Any], envelope: dict[str, Any]) -> str | None:
    """Return a reject reason, or None if the sample may move the watermark."""
    if envelope.get("unit") not in (None, "", UNIT) and str(envelope.get("unit")) != UNIT:
        return "invalid_unit"
    if envelope.get("nft_ok") is False:
        return "nft_down"
    coverage = str(sample.get("coverage") or "")
    if coverage not in BILLABLE_COVERAGE:
        return coverage or "unmeasured"
    if _as_int(sample.get("counter_bytes"), -1) < 0:
        return "unmeasured"
    return None


class MeasuredMeteringService:
    """Idempotent ingest of node envelopes + UTC-month snapshots.

    High water is ``(node, conn_id, generation)``. Duplicate envelopes are
    ``(node, boot_id, seq)``. A later envelope with a *lower* seq is still
    processed if that seq was never seen — unique samples must not be lost.
    """

    def __init__(self, db: Database,
                 tag_to_user: Callable[[], dict[str, str]] | None = None,
                 expected_nodes: Callable[[], Iterable[str]] | None = None,
                 ) -> None:
        self._db = db
        self._tag_to_user = tag_to_user or dict
        self._expected_nodes = expected_nodes or list

    # -- ingest --------------------------------------------------------------
    def ingest(self, envelope: dict[str, Any]) -> dict[str, Any]:
        node = str(envelope.get("node") or "").strip()
        boot_id = str(envelope.get("boot_id") or "").strip()
        seq = _as_int(envelope.get("seq"), -1)
        if not node or not boot_id or seq < 0:
            return {"ok": False, "reason": "bad_envelope", "credited": 0}
        samples = envelope.get("samples")
        if not isinstance(samples, list):
            return {"ok": False, "reason": "samples_not_list", "credited": 0}

        nft_ok = bool(envelope.get("nft_ok"))
        observed_at = float(envelope.get("observed_at") or time.time())
        tag_map = self._tag_to_user()
        credited = 0
        unattributed = 0
        skipped = 0
        duplicate = False

        with self._db.write() as conn:
            seen = conn.execute(
                "SELECT 1 FROM measured_envelopes WHERE node=? AND boot_id=? AND seq=?",
                (node, boot_id, seq),
            ).fetchone()
            if seen is not None:
                duplicate = True
            else:
                conn.execute(
                    "INSERT INTO measured_envelopes"
                    "(node,boot_id,seq,nft_ok,observed_at) VALUES(?,?,?,?,?)",
                    (node, boot_id, seq, 1 if nft_ok else 0, observed_at),
                )
                for sample in samples:
                    add, unknown, skip = self._credit_sample(
                        conn, node, sample, tag_map, envelope)
                    credited += add
                    unattributed += unknown
                    skipped += skip
            conn.execute(
                "INSERT INTO measured_node_seq"
                "(node,boot_id,seq,nft_ok,observed_at,updated_at,acked) "
                "VALUES(?,?,?,?,?,?,1) "
                "ON CONFLICT(node,boot_id) DO UPDATE SET "
                "seq=MAX(measured_node_seq.seq, excluded.seq), "
                "nft_ok=excluded.nft_ok, "
                "observed_at=excluded.observed_at, "
                "updated_at=excluded.updated_at, acked=1",
                (node, boot_id, seq, 1 if nft_ok else 0, observed_at,
                 time.time()),
            )

        return {
            "ok": True,
            "duplicate": duplicate,
            "credited": credited,
            "unattributed": unattributed,
            "skipped": skipped,
            "seq": seq,
            "boot_id": boot_id,
            "node": node,
            "ack": {"boot_id": boot_id, "seq": seq},
        }

    def _credit_sample(self, conn: Any, node: str, sample: dict[str, Any],
                       tag_map: dict[str, str], envelope: dict[str, Any]
                       ) -> tuple[int, int, int]:
        conn_id = str(sample.get("conn_id") or "").strip()
        generation = _as_int(sample.get("generation"), 0)
        if not conn_id or generation < 1:
            return 0, 0, 1
        reject = _billable(sample, envelope)
        if reject:
            # Do not advance the billable watermark on unmeasured / untrusted
            # bytes. A later observed sample must still be able to credit
            # the gap from the last *trusted* counter.
            return 0, 0, 1

        counter = _as_int(sample.get("counter_bytes"), -1)
        utag = str(sample.get("utag") or "").strip()
        observed_at = float(sample.get("observed_at") or envelope.get("observed_at")
                            or time.time())
        user_id = tag_map.get(utag, UNKNOWN_USER)

        row = conn.execute(
            "SELECT counter_bytes, observed_at FROM measured_watermarks "
            "WHERE node=? AND conn_id=? AND generation=?",
            (node, conn_id, generation),
        ).fetchone()
        last = int(row["counter_bytes"]) if row is not None else 0
        last_at = float(row["observed_at"] or 0) if row is not None else 0
        if counter < last:
            return 0, 0, 0
        delta = counter - last
        conn.execute(
            "INSERT INTO measured_watermarks"
            "(node,conn_id,generation,utag,emby_user_id,counter_bytes,observed_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(node,conn_id,generation) DO UPDATE SET "
            "counter_bytes=excluded.counter_bytes, "
            "observed_at=excluded.observed_at, "
            "utag=excluded.utag, "
            "emby_user_id=CASE WHEN excluded.emby_user_id<>'' "
            "THEN excluded.emby_user_id ELSE measured_watermarks.emby_user_id END, "
            "updated_at=excluded.updated_at",
            (node, conn_id, generation, utag, user_id, counter, observed_at,
             time.time()),
        )
        if delta <= 0:
            return 0, 0, 0

        month = month_key(observed_at)
        if last_at and month_key(last_at) != month:
            gap = observed_at - last_at
            if gap > MONTH_GAP_SECONDS:
                # Cannot place the bytes in either month without inventing a
                # split. Record as unattributed period_gap, not this month.
                conn.execute(
                    "INSERT INTO measured_usage_monthly"
                    "(month,node,utag,emby_user_id,bytes,unattributed) "
                    "VALUES(?,?,?,?,0,?) "
                    "ON CONFLICT(month,node,utag) DO UPDATE SET "
                    "unattributed=unattributed+excluded.unattributed",
                    (month, node, utag or "unknown", "", delta),
                )
                return 0, delta, 0

        unknown = 0 if user_id else delta
        conn.execute(
            "INSERT INTO measured_usage_monthly"
            "(month,node,utag,emby_user_id,bytes,unattributed) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(month,node,utag) DO UPDATE SET "
            "bytes=bytes+excluded.bytes, "
            "unattributed=unattributed+excluded.unattributed, "
            "emby_user_id=CASE WHEN excluded.emby_user_id<>'' "
            "THEN excluded.emby_user_id ELSE measured_usage_monthly.emby_user_id END",
            (month, node, utag or "unknown", user_id, delta, unknown),
        )
        return delta, unknown, 0

    # -- queries -------------------------------------------------------------
    def snapshot(self, user_id: str, *, now: float | None = None) -> dict[str, Any]:
        """Stable member-facing view. Missing measurement is not zero."""
        now = time.time() if now is None else now
        month = month_key(now)
        raw_row = self._db.one(
            "SELECT SUM(bytes) AS b FROM measured_usage_monthly "
            "WHERE emby_user_id=? AND month=?",
            (user_id, month),
        )
        has_row = self._db.one(
            "SELECT 1 AS n FROM measured_usage_monthly "
            "WHERE emby_user_id=? AND month=? LIMIT 1",
            (user_id, month),
        ) is not None
        raw = int((raw_row or {}).get("b") or 0) if has_row else None
        credit_row = self._db.one(
            "SELECT credit_bytes AS c FROM measured_credits "
            "WHERE emby_user_id=? AND month=?",
            (user_id, month),
        )
        credit = int((credit_row or {}).get("c") or 0)
        used = None if raw is None else max(0, raw - credit)
        nodes = self._node_coverage(now)
        degraded = (not nodes) or any(not n["ok"] for n in nodes)
        as_of_vals = [n["as_of"] for n in nodes if n.get("as_of")]
        return {
            "user_id": user_id,
            "source": SOURCE,
            "unit": UNIT,
            "measured_used_bytes": used,
            "measured_raw_bytes": raw,
            "credit_bytes": credit,
            "period": month,
            "period_start": period_start(int(now)),
            "as_of": max(as_of_vals) if as_of_vals else None,
            "coverage": {
                "nodes": nodes,
                "degraded": degraded,
                "reason": None if not degraded else (
                    "no_node_reports" if not any(n.get("as_of") for n in nodes)
                    else "node_stale_or_nft_down"),
            },
        }

    def totals(self, *, month: str | None = None,
               now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        month = month or month_key(now)
        by_user_rows = self._db.query(
            "SELECT emby_user_id, SUM(bytes) AS b FROM measured_usage_monthly "
            "WHERE month=? AND emby_user_id<>'' GROUP BY emby_user_id",
            (month,),
        )
        unattr = self._db.one(
            "SELECT SUM(unattributed) AS b FROM measured_usage_monthly WHERE month=?",
            (month,),
        )
        by_node = self._db.query(
            "SELECT node, SUM(bytes) AS b, SUM(unattributed) AS u "
            "FROM measured_usage_monthly WHERE month=? GROUP BY node",
            (month,),
        )
        return {
            "source": SOURCE,
            "unit": UNIT,
            "period": month,
            "by_user": {r["emby_user_id"]: int(r["b"] or 0) for r in by_user_rows},
            "unattributed_bytes": int((unattr or {}).get("b") or 0),
            "by_node": [
                {"node": r["node"], "bytes": int(r["b"] or 0),
                 "unattributed": int(r["u"] or 0)}
                for r in by_node
            ],
            "coverage": {"nodes": self._node_coverage(now)},
        }

    def reset_credit(self, user_id: str, *, now: float | None = None) -> dict[str, Any]:
        """Offset this user's current UTC month so quota-used is an explicit 0.

        History in ``measured_usage_monthly`` is kept. Watermarks stay so later
        reports only add new deltas. ``totals.by_user`` still shows the raw
        cumulative. Does not touch ``traffic_used_bytes`` or the edge ledger.
        """
        now = time.time() if now is None else now
        month = month_key(now)
        raw_row = self._db.one(
            "SELECT SUM(bytes) AS b FROM measured_usage_monthly "
            "WHERE emby_user_id=? AND month=?",
            (user_id, month),
        )
        has_row = self._db.one(
            "SELECT 1 AS n FROM measured_usage_monthly "
            "WHERE emby_user_id=? AND month=? LIMIT 1",
            (user_id, month),
        ) is not None
        raw = int((raw_row or {}).get("b") or 0) if has_row else None
        credit = 0 if raw is None else raw
        self._db.execute(
            "INSERT INTO measured_credits(month,emby_user_id,credit_bytes,updated_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(month,emby_user_id) DO UPDATE SET "
            "credit_bytes=excluded.credit_bytes, updated_at=excluded.updated_at",
            (month, user_id, credit, now),
        )
        used = None if raw is None else 0
        return {
            "ok": True, "user_id": user_id, "period": month,
            "measured_used_bytes": used,
            "measured_raw_bytes": raw,
            "credit_bytes": credit,
        }

    def used_bytes(self, user_id: str, *, now: float | None = None) -> int | None:
        snap = self.snapshot(user_id, now=now)
        return snap["measured_used_bytes"]

    def load_policy(self) -> dict[str, Any]:
        row = self._db.one("SELECT rev, blocked_json, updated_at FROM meter_policy WHERE id=1")
        if row is None:
            return {"rev": 0, "blocked_tags": [], "updated_at": None, "snapshot": True}
        try:
            tags = json.loads(row["blocked_json"] or "[]")
        except json.JSONDecodeError:
            tags = []
        if not isinstance(tags, list):
            tags = []
        return {
            "rev": int(row["rev"] or 0),
            "blocked_tags": [str(t) for t in tags if str(t).strip()],
            "updated_at": row["updated_at"],
            "snapshot": True,
        }

    def publish_policy(self, blocked_tags: list[str], *, now: float | None = None
                       ) -> dict[str, Any]:
        """Persist an authoritative deny snapshot. Empty list is a real snapshot."""
        now = time.time() if now is None else now
        tags = sorted({str(t).strip() for t in blocked_tags if str(t).strip()})
        current = self.load_policy()
        if current["rev"] and current["blocked_tags"] == tags:
            return current
        rev = int(current["rev"] or 0) + 1
        payload = json.dumps(tags, separators=(",", ":"))
        self._db.execute(
            "INSERT INTO meter_policy(id,rev,blocked_json,updated_at) VALUES(1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET rev=excluded.rev, "
            "blocked_json=excluded.blocked_json, updated_at=excluded.updated_at",
            (rev, payload, now),
        )
        return {"rev": rev, "blocked_tags": tags, "updated_at": now, "snapshot": True}

    def note_policy_sent(self, node: str, rev: int, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._db.execute(
            "INSERT INTO meter_policy_ack(node,sent_rev,applied_rev,sent_at,applied_at) "
            "VALUES(?,?,0,?,0) "
            "ON CONFLICT(node) DO UPDATE SET "
            "sent_rev=excluded.sent_rev, sent_at=excluded.sent_at",
            (node, int(rev), now),
        )

    def note_policy_applied(self, node: str, rev: int, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        current = self._db.one(
            "SELECT sent_rev FROM meter_policy_ack WHERE node=?", (node,))
        sent = int((current or {}).get("sent_rev") or 0)
        if int(rev) <= 0 or int(rev) > sent:
            return
        self._db.execute(
            "INSERT INTO meter_policy_ack(node,sent_rev,applied_rev,sent_at,applied_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(node) DO UPDATE SET "
            "applied_rev=MAX(meter_policy_ack.applied_rev, excluded.applied_rev), "
            "applied_at=excluded.applied_at",
            (node, sent, int(rev), now, now),
        )

    def ack_policy(self, node: str, rev: int, *, now: float | None = None) -> None:
        """Compatibility: recording a send is not an applied confirmation."""
        self.note_policy_sent(node, rev, now=now)

    def policy_acks(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.query(
            "SELECT node, sent_rev, applied_rev, sent_at, applied_at "
            "FROM meter_policy_ack ORDER BY node")]

    def _node_coverage(self, now: float, stale_after: float = 120.0) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT node, boot_id, seq, nft_ok, observed_at FROM measured_node_seq"
        )
        by_name = {r["node"]: r for r in rows}
        expected = list(self._expected_nodes() or [])
        names = list(expected) if expected else list(by_name)
        # Always include expected nodes even if they have never reported.
        for name in by_name:
            if name not in names:
                names.append(name)
        out = []
        for name in names:
            row = by_name.get(name)
            if row is None:
                out.append({
                    "name": name, "ok": False, "nft_ok": False,
                    "boot_id": None, "seq": 0, "as_of": None,
                    "reason": "never_reported",
                })
                continue
            observed = float(row["observed_at"] or 0)
            ok = bool(row["nft_ok"]) and (now - observed) <= stale_after
            out.append({
                "name": name,
                "ok": ok,
                "nft_ok": bool(row["nft_ok"]),
                "boot_id": row["boot_id"],
                "seq": int(row["seq"] or 0),
                "as_of": observed or None,
                "reason": None if ok else (
                    "nft_down" if not row["nft_ok"] else "stale"),
            })
        return out
