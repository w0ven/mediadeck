"""Panel measured ledger: idempotent ingest, month roll, unknown ≠ zero."""
from __future__ import annotations

from pathlib import Path

from app.core.db import Database
from app.modules.metering import MeasuredMeteringService, month_key
from app.modules.signing import user_tag


def _svc(tmp_path: Path, tags: dict[str, str] | None = None) -> MeasuredMeteringService:
    return MeasuredMeteringService(Database(tmp_path / "m.db"),
                                   tag_to_user=lambda: dict(tags or {}))


def _env(node: str, boot: str, seq: int, samples: list, *,
         nft_ok: bool = True, observed_at: float = 1_700_000_000.0) -> dict:
    return {
        "node": node, "boot_id": boot, "seq": seq,
        "observed_at": observed_at, "nft_ok": nft_ok,
        "nft_error": None, "unit": "kernel_outbound_ip_bytes",
        "samples": samples,
    }


def _sample(conn_id: str, utag: str, counter: int, *, generation: int = 1,
            observed_at: float = 1_700_000_000.0, coverage: str = "observed") -> dict:
    return {
        "conn_id": conn_id, "generation": generation, "utag": utag,
        "family": "inet", "local_ip": "127.0.0.1", "local_port": 443,
        "remote_ip": "127.0.0.1", "remote_port": 41001,
        "counter_bytes": counter, "counter_packets": 1,
        "observed_at": observed_at, "closed": False, "coverage": coverage,
    }


def test_snapshot_unknown_is_null_not_zero(tmp_path: Path) -> None:
    svc = _svc(tmp_path, {user_tag("u1"): "u1"})
    snap = svc.snapshot("u1", now=1_700_000_000.0)
    assert snap["source"] == "measured"
    assert snap["unit"] == "kernel_outbound_ip_bytes"
    assert snap["measured_used_bytes"] is None
    assert snap["period"] == month_key(1_700_000_000.0)


def test_retry_and_out_of_order_do_not_double_count(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    first = _env("n1", "bootA", 1, [_sample("c1", tag, 1000)])
    assert svc.ingest(first)["credited"] == 1000
    assert svc.ingest(first)["duplicate"] is True
    assert svc.ingest(first)["credited"] == 0
    # Out of order: smaller counter, same generation.
    late = _env("n1", "bootA", 2, [_sample("c1", tag, 400)])
    assert svc.ingest(late)["credited"] == 0
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 1000
    # Progress.
    nxt = _env("n1", "bootA", 3, [_sample("c1", tag, 1500)])
    assert svc.ingest(nxt)["credited"] == 500
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 1500


def test_new_boot_and_generation_are_new_curves(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    svc.ingest(_env("n1", "bootA", 1, [_sample("c1", tag, 1000)]))
    # Same conn_id after reboot is a different watermark key.
    svc.ingest(_env("n1", "bootB", 1, [_sample("c1", tag, 200)]))
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 1200
    # Explicit generation bump (counter reset on the node).
    svc.ingest(_env("n1", "bootB", 2, [_sample("c1", tag, 50, generation=2)]))
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 1250


def test_two_nodes_aggregate_and_unattributed_is_separate(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    svc.ingest(_env("n1", "b1", 1, [_sample("c1", tag, 100)]))
    svc.ingest(_env("n2", "b2", 1, [_sample("c2", tag, 40)]))
    svc.ingest(_env("n2", "b2", 2, [_sample("c3", "orphan", 9)]))
    totals = svc.totals(now=1_700_000_000.0)
    assert totals["by_user"]["u1"] == 140
    assert totals["unattributed_bytes"] == 9
    assert {r["node"] for r in totals["by_node"]} == {"n1", "n2"}
    assert "orphan" not in totals["by_user"]


def test_month_boundary_does_not_mix(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    # 2023-12-31 23:00 UTC vs 2024-01-01 01:00 UTC
    dec = 1_704_063_600.0
    jan = 1_704_070_800.0
    svc.ingest(_env("n1", "b", 1, [_sample("c1", tag, 10, observed_at=dec)],
                    observed_at=dec))
    svc.ingest(_env("n1", "b", 2, [_sample("c1", tag, 25, observed_at=jan)],
                    observed_at=jan))
    assert svc.snapshot("u1", now=dec)["measured_used_bytes"] == 10
    assert svc.snapshot("u1", now=jan)["measured_used_bytes"] == 15
    assert svc.snapshot("u1", now=dec)["period"] != svc.snapshot("u1", now=jan)["period"]


def test_reset_credit_clears_month_keeps_later_deltas(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    now = 1_700_000_000.0
    svc.ingest(_env("n1", "b", 1, [_sample("c1", tag, 80)], observed_at=now))
    out = svc.reset_credit("u1", now=now)
    assert out["measured_used_bytes"] is None
    assert svc.snapshot("u1", now=now)["measured_used_bytes"] is None
    svc.ingest(_env("n1", "b", 2, [_sample("c1", tag, 100)], observed_at=now))
    assert svc.snapshot("u1", now=now)["measured_used_bytes"] == 20


def test_does_not_read_legacy_estimate_tables(tmp_path: Path) -> None:
    tag = user_tag("u1")
    db = Database(tmp_path / "m.db")
    db.execute(
        "INSERT INTO members(emby_user_id,username,status,traffic_used_bytes,"
        "traffic_period_start,created_at,updated_at) "
        "VALUES('u1','alice','active',999999,1,1,1)")
    db.execute(
        "INSERT INTO edge_usage_daily(day,node,utag,emby_user_id,bytes,requests,seconds) "
        "VALUES('2023-11-15','n1',?,?,9000,1,1)", (tag, "u1"))
    svc = MeasuredMeteringService(db, tag_to_user=lambda: {tag: "u1"})
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] is None
