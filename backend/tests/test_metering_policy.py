"""Authoritative deny snapshot: both nodes converge, empty is real, restart keeps it."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from test_metering import _env, _sample

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.metering import MeasuredMeteringService
from app.modules.signing import user_tag

AGENT = Path(__file__).resolve().parents[2] / "agent"


def _flow():
    spec = importlib.util.spec_from_file_location("flowmeter", AGENT / "flowmeter.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_two_nodes_share_snapshot_and_panel_restart_keeps_it(tmp_path: Path) -> None:
    db = Database(tmp_path / "panel.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    tag = user_tag("u1")
    meter = MeasuredMeteringService(db, tag_to_user=lambda: {tag: "u1"})
    members.bind_metering(meter, cutover=lambda: True)
    members.upsert("u1", "alice", {"group_id": "standard"})
    now = time.time()
    meter.ingest(_env("n1", "b", 1, [_sample("c1", tag, 1024 ** 4, observed_at=now)],
                      observed_at=now))
    assert members.get("u1")["state"] == "exhausted"

    policy = meter.publish_policy([tag])
    assert policy["snapshot"] is True
    flow = _flow()
    n1 = flow.FlowMeter(str(tmp_path / "n1.db"), node="n1",
                        deny_map_path=str(tmp_path / "n1.map"))
    n2 = flow.FlowMeter(str(tmp_path / "n2.db"), node="n2",
                        deny_map_path=str(tmp_path / "n2.map"))
    n1.apply_policy(policy["blocked_tags"], snapshot=True, terminate=False)
    n2.apply_policy(policy["blocked_tags"], snapshot=True, terminate=False)
    assert tag in n1.blocked_tags() and tag in n2.blocked_tags()
    meter.ack_policy("n1", policy["rev"])
    meter.ack_policy("n2", policy["rev"])

    # Disconnect / empty merge must not clear known deny.
    n1.apply_policy([], snapshot=False, terminate=False)
    assert tag in n1.blocked_tags()

    members.reset_traffic("u1")
    assert members.get("u1")["state"] == "active"
    cleared = meter.publish_policy([])
    n1.apply_policy(cleared["blocked_tags"], snapshot=True, terminate=False)
    n2.apply_policy(cleared["blocked_tags"], snapshot=True, terminate=False)
    assert tag not in n1.blocked_tags() and tag not in n2.blocked_tags()

    # Extra traffic / group change: recompute snapshot, both nodes match.
    members.add_extra_traffic("u1", 10)
    again = meter.publish_policy([])
    n1.apply_policy(again["blocked_tags"], snapshot=True, terminate=False)
    n2.apply_policy(again["blocked_tags"], snapshot=True, terminate=False)
    assert n1.blocked_tags() == n2.blocked_tags()

    members.upsert("u1", "alice", {"group_id": "whitelist"})
    none_pol = meter.publish_policy([])
    n1.apply_policy(none_pol["blocked_tags"], snapshot=True, terminate=False)
    n2.apply_policy(none_pol["blocked_tags"], snapshot=True, terminate=False)
    assert n1.blocked_tags() == [] and n2.blocked_tags() == []

    # Panel process restart: load_policy still has the last snapshot.
    meter2 = MeasuredMeteringService(db, tag_to_user=lambda: {tag: "u1"})
    loaded = meter2.load_policy()
    assert loaded["rev"] == none_pol["rev"]
    assert loaded["blocked_tags"] == []
    n1.close()
    n2.close()


def test_suspended_is_not_unblocked_by_reset_or_extra(tmp_path: Path) -> None:
    db = Database(tmp_path / "panel.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    tag = user_tag("u1")
    meter = MeasuredMeteringService(db, tag_to_user=lambda: {tag: "u1"})
    members.bind_metering(meter, cutover=lambda: True)
    members.upsert("u1", "alice", {"group_id": "standard"})
    members.set_status("u1", "suspended")
    members.reset_traffic("u1")
    members.add_extra_traffic("u1", 99)
    after = members.get("u1")
    assert after["state"] == "suspended"
    policy = meter.publish_policy([tag])
    assert tag in policy["blocked_tags"]


def test_cutover_unknown_does_not_fall_back_to_estimate(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.bind_metering(
        MeasuredMeteringService(db, tag_to_user=dict),
        cutover=lambda: True)
    members.upsert("u1", "alice", {"group_id": "standard"})
    db.execute("UPDATE members SET traffic_used_bytes=? WHERE emby_user_id='u1'",
               (10 * 1024 ** 4,))
    row = members.get("u1")
    assert row["quota_source"] == "measured"
    assert "measured_used_bytes" in row
    assert row["measured_used_bytes"] is None
    assert row["state"] == "active"
    assert row["traffic_remaining_bytes"] is None
    assert row["traffic_percent"] is None
