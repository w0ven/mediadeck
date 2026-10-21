"""Authoritative deny snapshot: both nodes converge, empty is real, restart keeps it.

Byte quotas are retired (docs/TRAFFIC_RETIREMENT.md): the snapshot now only
carries manual disable, pending and expiry. These tests keep the access-denial
guarantees on the panel endpoint and the node guard that replaced the meter.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.core.errors import ConfigError
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.metering import MeasuredMeteringService
from app.modules.signing import user_tag

AGENT = Path(__file__).resolve().parents[2] / "agent"
ADMIN = ("admin", "change-me")


def _guard_module():
    spec = importlib.util.spec_from_file_location("meterd", AGENT / "meterd.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _policy(client, node: str = "mock-a") -> dict:
    token = client.get(f"/api/nodes/{node}/report-token", auth=ADMIN).json()["report_token"]
    response = client.get(f"/api/edge/{node}/policy",
                          headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return response.json()


def test_two_nodes_share_snapshot_and_guard_restart_keeps_it(tmp_path: Path) -> None:
    mod = _guard_module()
    tag = user_tag("u1")
    n1 = mod.AccessPolicy(str(tmp_path / "n1.json"), str(tmp_path / "n1.map"),
                          mod.DenyPublisher(str(tmp_path / "n1.map")))
    n2 = mod.AccessPolicy(str(tmp_path / "n2.json"), str(tmp_path / "n2.map"),
                          mod.DenyPublisher(str(tmp_path / "n2.map")))
    with TestClient(app) as client:
        members = app.state.members
        members.upsert("u1", "demo-user-1", {"group_id": "standard"})
        members.set_status("u1", "suspended")
        policy = _policy(client)
        assert policy["policy"]["snapshot"] is True
        assert tag in policy["policy"]["blocked_tags"]
        n1.apply(policy)
        n2.apply(policy)
        assert tag in n1.blocked and tag in n2.blocked

        # Disconnect / empty merge must not clear known deny.
        with pytest.raises(ValueError):
            n1.apply({"ok": True, "policy": {"blocked_tags": []}})
        with pytest.raises(ValueError):
            n1.apply({"ok": False, "policy": {"snapshot": True, "blocked_tags": []}})
        assert tag in n1.blocked

        members.set_status("u1", "active")
        cleared = _policy(client)
        assert tag not in cleared["policy"]["blocked_tags"]
        n1.apply(cleared)
        n2.apply(cleared)
        assert tag not in n1.blocked and tag not in n2.blocked
        assert n1.blocked == n2.blocked

        members.set_status("u1", "suspended")
        again = _policy(client)
        n1.apply(again)
        n2.apply(again)
    # Guard process restart: the last authoritative snapshot is reloaded.
    restored = mod.AccessPolicy(str(tmp_path / "n1.json"), str(tmp_path / "n1.map"))
    assert tag in restored.blocked
    assert '"' + tag + '" 1;' in (tmp_path / "n2.map").read_text()


def test_suspended_is_not_unblocked_by_retired_reset_or_extra() -> None:
    tag = user_tag("u1")
    with TestClient(app) as client:
        members = app.state.members
        members.upsert("u1", "demo-user-1", {"group_id": "standard"})
        members.set_status("u1", "suspended")
        with pytest.raises(ConfigError):
            members.reset_traffic("u1")
        with pytest.raises(ConfigError):
            members.add_extra_traffic("u1", 99)
        assert members.get("u1")["state"] == "suspended"
        assert tag in _policy(client)["policy"]["blocked_tags"]


def test_archived_estimate_never_becomes_a_quota_or_usage(tmp_path: Path) -> None:
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
    assert row["quota_source"] == "retired"
    assert row["traffic_tracking"] is False
    assert row["measured_used_bytes"] is None
    assert row["traffic_used_bytes"] is None
    assert row["state"] == "active"
    assert row["traffic_remaining_bytes"] is None
    assert row["traffic_percent"] is None
    # The archived counter itself is preserved, only no longer presented.
    assert db.one("SELECT traffic_used_bytes FROM members WHERE emby_user_id='u1'")[
        "traffic_used_bytes"] == 10 * 1024 ** 4
