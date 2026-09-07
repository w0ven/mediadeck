"""Measured traffic ledger — kernel outbound IP bytes, idempotent across nodes.

This is the panel half of measured metering. The node core is
``agent/flowmeter.py``. Bytes here are **kernel outbound IP-packet bytes**
as counted by nft on registered 4-tuples: headers included, possible
retransmits included. They are not HTTP content length and not a NIC
frame capture. A loopback header-overhead observation is not a general
error bound.

Old ``edge_usage_daily`` / ``members.traffic_used_bytes`` stay statistical.
Nothing here imports that history as a billing baseline. Cutover of
enforcement onto this ledger is an explicit operator decision; this
module never flips ``enforcement_enabled``.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.db import Database
from app.modules.members import period_start

UNIT = "kernel_outbound_ip_bytes"
SOURCE = "measured"
UNKNOWN_USER = ""


def month_key(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), UTC)
    return dt.strftime("%Y-%m")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class MeasuredMeteringService:
    """Idempotent ingest of node envelopes + UTC-month snapshots.

    High water is ``(node, boot_id, conn_id, generation)``. A smaller
    ``counter_bytes`` on a later envelope with the same key is a duplicate
    or out-of-order report, not a new generation. A new generation is only
    accepted when the node sends a higher ``generation`` (or a new
    ``conn_id`` / ``boot_id``).
    """

    def __init__(self, db: Database,
                 tag_to_user: Callable[[], dict[str, str]] | None = None) -> None:
        self._db = db
        self._tag_to_user = tag_to_user or dict

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
        duplicate = False

        with self._db.write() as conn:
            existing = conn.execute(
                "SELECT seq, acked FROM measured_node_seq "
                "WHERE node=? AND boot_id=?",
                (node, boot_id),
            ).fetchone()
            if existing is not None and int(existing["seq"]) >= seq:
                duplicate = True
            else:
                for sample in samples:
                    add, unknown = self._credit_sample(
                        conn, node, boot_id, sample, tag_map, observed_at)
                    credited += add
                    unattributed += unknown
                conn.execute(
                    "INSERT INTO measured_node_seq"
                    "(node,boot_id,seq,nft_ok,observed_at,updated_at,acked) "
                    "VALUES(?,?,?,?,?,?,1) "
                    "ON CONFLICT(node,boot_id) DO UPDATE SET "
                    "seq=excluded.seq, nft_ok=excluded.nft_ok, "
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
            "seq": seq,
            "boot_id": boot_id,
            "node": node,
            "ack": {"boot_id": boot_id, "seq": seq},
        }

    def _credit_sample(self, conn: Any, node: str, boot_id: str,
                       sample: dict[str, Any], tag_map: dict[str, str],
                       envelope_ts: float) -> tuple[int, int]:
        conn_id = str(sample.get("conn_id") or "").strip()
        generation = _as_int(sample.get("generation"), 0)
        if not conn_id or generation < 1:
            return 0, 0
        counter = _as_int(sample.get("counter_bytes"), -1)
        if counter < 0:
            return 0, 0
        utag = str(sample.get("utag") or "").strip()
        coverage = str(sample.get("coverage") or "observed")
        observed_at = float(sample.get("observed_at") or envelope_ts)
        user_id = tag_map.get(utag, UNKNOWN_USER)

        row = conn.execute(
            "SELECT counter_bytes FROM measured_watermarks "
            "WHERE node=? AND boot_id=? AND conn_id=? AND generation=?",
            (node, boot_id, conn_id, generation),
        ).fetchone()
        last = int(row["counter_bytes"]) if row is not None else 0
        if counter < last:
            # Duplicate / out-of-order. Not a new generation.
            return 0, 0
        delta = counter - last
        conn.execute(
            "INSERT INTO measured_watermarks"
            "(node,boot_id,conn_id,generation,utag,emby_user_id,counter_bytes,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(node,boot_id,conn_id,generation) DO UPDATE SET "
            "counter_bytes=excluded.counter_bytes, "
            "utag=excluded.utag, "
            "emby_user_id=CASE WHEN excluded.emby_user_id<>'' "
            "THEN excluded.emby_user_id ELSE measured_watermarks.emby_user_id END, "
            "updated_at=excluded.updated_at",
            (node, boot_id, conn_id, generation, utag, user_id, counter,
             observed_at),
        )
        if delta <= 0:
            return 0, 0
        if coverage not in ("observed", "retained"):
            # Unknown coverage is not billed as measured.
            return 0, 0
        month = month_key(observed_at)
        bucket_user = user_id
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
            (month, node, utag or "unknown", bucket_user, delta, unknown),
        )
        return delta, unknown

    # -- queries -------------------------------------------------------------
    def snapshot(self, user_id: str, *, now: float | None = None) -> dict[str, Any]:
        """Stable member-facing view. Missing measurement is not zero."""
        now = time.time() if now is None else now
        month = month_key(now)
        row = self._db.one(
            "SELECT SUM(bytes) AS b FROM measured_usage_monthly "
            "WHERE emby_user_id=? AND month=?",
            (user_id, month),
        )
        nodes = self._node_coverage(now)
        any_ok = any(n["ok"] for n in nodes) if nodes else False
        measured = None if (row is None or row.get("b") is None) and not any_ok else int(
            (row or {}).get("b") or 0)
        # Distinguish "user has a row of 0" from "never credited":
        has_row = self._db.one(
            "SELECT 1 AS n FROM measured_usage_monthly "
            "WHERE emby_user_id=? AND month=? LIMIT 1",
            (user_id, month),
        ) is not None
        if not has_row:
            measured = None
        degraded = (not nodes) or any(not n["ok"] for n in nodes)
        as_of = max((n["as_of"] or 0) for n in nodes) if nodes else None
        return {
            "user_id": user_id,
            "source": SOURCE,
            "unit": UNIT,
            "measured_used_bytes": measured,
            "period": month,
            "period_start": period_start(int(now)),
            "as_of": as_of,
            "coverage": {
                "nodes": nodes,
                "degraded": degraded,
                "reason": None if not degraded else (
                    "no_node_reports" if not nodes else "node_stale_or_nft_down"),
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
        """Zero this user's current UTC month measured credit.

        Watermarks stay: later node reports only add *new* deltas. Does not
        touch ``traffic_used_bytes`` or the edge log ledger.
        """
        now = time.time() if now is None else now
        month = month_key(now)
        self._db.execute(
            "DELETE FROM measured_usage_monthly WHERE emby_user_id=? AND month=?",
            (user_id, month),
        )
        return {"ok": True, "user_id": user_id, "period": month,
                "measured_used_bytes": None}

    def _node_coverage(self, now: float, stale_after: float = 120.0) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT node, boot_id, seq, nft_ok, observed_at FROM measured_node_seq"
        )
        out = []
        for row in rows:
            observed = float(row["observed_at"] or 0)
            ok = bool(row["nft_ok"]) and (now - observed) <= stale_after
            out.append({
                "name": row["node"],
                "ok": ok,
                "nft_ok": bool(row["nft_ok"]),
                "boot_id": row["boot_id"],
                "seq": int(row["seq"] or 0),
                "as_of": observed or None,
            })
        return out
