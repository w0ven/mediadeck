"""Panel measured ledger: idempotent ingest, month roll, unknown ≠ zero."""
from __future__ import annotations

import time
from pathlib import Path

from app.core.db import Database
from app.modules.metering import MONTH_GAP_SECONDS, MeasuredMeteringService, month_key
from app.modules.signing import user_tag


def _svc(tmp_path: Path, tags: dict[str, str] | None = None,
         expected: list[str] | None = None) -> MeasuredMeteringService:
    return MeasuredMeteringService(
        Database(tmp_path / "m.db"),
        tag_to_user=lambda: dict(tags or {}),
        expected_nodes=(lambda: list(expected or [])),
    )


def _env(node: str, boot: str, seq: int, samples: list, *,
         nft_ok: bool = True, observed_at: float = 1_700_000_000.0,
         unit: str = "kernel_outbound_ip_bytes") -> dict:
    return {
        "node": node, "boot_id": boot, "seq": seq,
        "observed_at": observed_at, "nft_ok": nft_ok,
        "nft_error": None, "unit": unit,
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
    late = _env("n1", "bootA", 2, [_sample("c1", tag, 400)])
    assert svc.ingest(late)["credited"] == 0
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 1000
    nxt = _env("n1", "bootA", 3, [_sample("c1", tag, 1500)])
    assert svc.ingest(nxt)["credited"] == 500
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 1500


def test_older_unique_seq_is_not_skipped(tmp_path: Path) -> None:
    """Highest-seq-wins would drop a late unique sample from an older envelope."""
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    later = _env("n1", "bootA", 5, [_sample("c2", tag, 40)])
    earlier = _env("n1", "bootA", 2, [_sample("c1", tag, 100)])
    assert svc.ingest(later)["credited"] == 40
    assert svc.ingest(earlier)["credited"] == 100
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 140
    # Retry of either envelope is a no-op.
    assert svc.ingest(later)["duplicate"] is True
    assert svc.ingest(earlier)["credited"] == 0


def test_restart_same_conn_does_not_rebill_from_zero(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    svc.ingest(_env("n1", "bootA", 1, [_sample("c1", tag, 100)]))
    # Same live stream, new report epoch, counter continued to 120.
    out = svc.ingest(_env("n1", "bootB", 1, [_sample("c1", tag, 120)]))
    assert out["credited"] == 20
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 120
    # Replaying the pre-restart pending envelope must not add 100 again.
    replay = svc.ingest(_env("n1", "bootA", 1, [_sample("c1", tag, 100)]))
    assert replay["duplicate"] is True
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 120


def test_generation_bump_is_a_new_curve(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    svc.ingest(_env("n1", "bootA", 1, [_sample("c1", tag, 1000)]))
    svc.ingest(_env("n1", "bootA", 2, [_sample("c1", tag, 50, generation=2)]))
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 1050


def test_untrusted_sample_does_not_advance_watermark(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    svc.ingest(_env("n1", "b", 1, [_sample("c1", tag, 100)]))
    bad = _env("n1", "b", 2, [_sample("c1", tag, 500, coverage="guess")])
    assert svc.ingest(bad)["credited"] == 0
    down = _env("n1", "b", 3, [_sample("c1", tag, 800)], nft_ok=False)
    assert svc.ingest(down)["credited"] == 0
    wrong_unit = _env("n1", "b", 4, [_sample("c1", tag, 900)], unit="http_body")
    assert svc.ingest(wrong_unit)["credited"] == 0
    good = _env("n1", "b", 5, [_sample("c1", tag, 150)])
    assert svc.ingest(good)["credited"] == 50
    assert svc.snapshot("u1", now=1_700_000_000.0)["measured_used_bytes"] == 150


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


def test_timely_month_boundary_credits_new_month(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    dec = 1_704_063_600.0
    jan = 1_704_070_800.0  # ~2h later
    svc.ingest(_env("n1", "b", 1, [_sample("c1", tag, 10, observed_at=dec)],
                    observed_at=dec))
    svc.ingest(_env("n1", "b", 2, [_sample("c1", tag, 25, observed_at=jan)],
                    observed_at=jan))
    assert svc.snapshot("u1", now=dec)["measured_used_bytes"] == 10
    assert svc.snapshot("u1", now=jan)["measured_used_bytes"] == 15


def test_stale_cross_month_gap_is_unattributed_not_this_month(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    dec = 1_704_063_600.0
    jan = dec + MONTH_GAP_SECONDS + 3600
    svc.ingest(_env("n1", "b", 1, [_sample("c1", tag, 10, observed_at=dec)],
                    observed_at=dec))
    out = svc.ingest(_env("n1", "b", 2, [_sample("c1", tag, 25, observed_at=jan)],
                          observed_at=jan))
    assert out["credited"] == 0
    assert out["unattributed"] == 15
    assert svc.snapshot("u1", now=jan)["measured_used_bytes"] is None


def test_reset_credit_clears_month_keeps_later_deltas(tmp_path: Path) -> None:
    tag = user_tag("u1")
    svc = _svc(tmp_path, {tag: "u1"})
    now = 1_700_000_000.0
    svc.ingest(_env("n1", "b", 1, [_sample("c1", tag, 80)], observed_at=now))
    out = svc.reset_credit("u1", now=now)
    assert out["measured_used_bytes"] == 0
    assert out["measured_raw_bytes"] == 80
    assert out["credit_bytes"] == 80
    snap = svc.snapshot("u1", now=now)
    assert snap["measured_used_bytes"] == 0
    assert snap["measured_raw_bytes"] == 80
    assert svc.totals(now=now)["by_user"]["u1"] == 80
    svc.ingest(_env("n1", "b", 2, [_sample("c1", tag, 100)], observed_at=now))
    later = svc.snapshot("u1", now=now)
    assert later["measured_used_bytes"] == 20
    assert later["measured_raw_bytes"] == 100
    assert svc.totals(now=now)["by_user"]["u1"] == 100


def test_expected_nodes_cover_never_reported(tmp_path: Path) -> None:
    svc = _svc(tmp_path, expected=["edge-a", "edge-b"])
    cov = svc.snapshot("u1", now=1_700_000_000.0)["coverage"]["nodes"]
    names = {n["name"] for n in cov}
    assert names == {"edge-a", "edge-b"}
    assert all(n["ok"] is False for n in cov)
    assert all(n["reason"] == "never_reported" for n in cov)


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


def test_cutover_uses_measured_not_estimate(tmp_path: Path) -> None:
    from app.modules.groups import GroupService
    from app.modules.members import MemberService

    db = Database(tmp_path / "m.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    tag = user_tag("u1")
    meter = MeasuredMeteringService(db, tag_to_user=lambda: {tag: "u1"})
    members.bind_metering(meter, cutover=lambda: True)
    members.upsert("u1", "alice", {"group_id": "standard"})
    # Estimate is huge; measured is not. Must not exhaust from estimate.
    db.execute("UPDATE members SET traffic_used_bytes=? WHERE emby_user_id='u1'",
               (10 * 1024 ** 4,))
    assert members.get("u1")["state"] == "active"
    now = time.time()
    meter.ingest(_env("n1", "b", 1, [_sample("c1", tag, 1024 ** 4, observed_at=now)],
                      observed_at=now))
    assert members.get("u1")["state"] == "exhausted"
    members.reset_traffic("u1")
    after = members.get("u1")
    assert after["state"] == "active"
    assert after["quota_source"] == "measured"
    assert after["measured_used_bytes"] == 0
    assert after["metering"]["measured_raw_bytes"] == 1024 ** 4
    assert after["traffic_remaining_bytes"] is not None
