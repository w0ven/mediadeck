"""Target billing axes win; keeping a term never re-enables a disabled axis."""

import asyncio
import json
import time

import pytest
from test_bot_account_center import base_bot, bot, cb, msg  # noqa: F401

from app.core.db import Database
from app.modules.groups import WHITELIST_GROUP_ID, GroupService
from app.modules.member_ops import group_preview
from app.modules.members import MemberService


@pytest.fixture
def stack(tmp_path):
    db = Database(tmp_path / "groups.db")
    g = GroupService(db)
    g.seed_defaults()
    g.create(
        {
            "id": "short",
            "name": "Short",
            "billing_mode": "both",
            "duration_days": 7,
            "traffic_quota_bytes": 1024,
        }
    )
    g.create(
        {
            "id": "timeonly",
            "name": "Time",
            "billing_mode": "time",
            "duration_days": 30,
            "traffic_quota_bytes": 0,
        }
    )
    m = MemberService(db, g)
    now = int(time.time())
    m.upsert(
        "u",
        "alice",
        {"group_id": "standard", "expires_at": now + 180 * 86400, "traffic_used_bytes": 987654},
    )
    ov = {
        "expires_at_override": now + 90 * 86400,
        "bandwidth_limit_kbps": 777,
        "max_devices": 9,
        "extra_traffic_bytes": 333,
    }
    db.execute("UPDATE members SET overrides_json=? WHERE emby_user_id=?", (json.dumps(ov), "u"))
    return db, g, m, now, ov


@pytest.mark.parametrize("policy,days", [("apply_group", 7), ("clear", None), ("set", 60)])
def test_explicit_date_actions_replace_effective_overlay(stack, policy, days):
    db, g, m, now, ov = stack
    payload = {"group_id": "short", "expiry_policy": policy}
    if policy == "set":
        payload["expires_at"] = now + 60 * 86400
    after = m.upsert("u", "alice", payload)
    assert "expires_at_override" not in after["overrides"]
    if days is None:
        assert after["expires_at_effective"] is None
    else:
        assert abs(after["expires_at_effective"] - (now + days * 86400)) <= 2
    assert after["bandwidth_limit_kbps"] == 777 and after["max_devices"] == 9
    assert after["traffic_used_bytes"] == 987654


def test_timed_to_timed_keep_preserves_effective_term(stack):
    db, g, m, now, ov = stack
    out = m.upsert("u", "alice", {"group_id": "short"})
    assert out["expires_at_effective"] == now + 90 * 86400
    assert out["overrides"] == ov
    p = group_preview(m, "u", "standard")
    assert p["policies"]["keep"]["expires_at"] == out["expires_at_effective"]


@pytest.mark.parametrize("group_id,traffic_billed", [("vip", True), (WHITELIST_GROUP_ID, False)])
def test_non_time_target_clears_expiry_even_when_keep_selected(stack, group_id, traffic_billed):
    db, g, m, now, ov = stack
    p = group_preview(m, "u", group_id)
    assert p["policies"]["keep"]["expires_at"] is None
    out = m.upsert("u", "alice", {"group_id": group_id, "expiry_policy": "keep"})
    assert out["expires_at"] is None and out["expires_at_effective"] is None
    assert "expires_at_override" not in out["overrides"]
    assert out["traffic_used_bytes"] == 987654
    assert (out["traffic_quota_bytes"] > 0) == traffic_billed
    assert out["overrides"]["extra_traffic_bytes"] == 333
    assert out["bandwidth_limit_kbps"] == 777 and out["max_devices"] == 9


def test_time_only_disables_quota_without_wiping_ledger(stack):
    db, g, m, now, ov = stack
    out = m.upsert("u", "alice", {"group_id": "timeonly"})
    assert out["traffic_quota_bytes"] == 0 and out["traffic_used_bytes"] == 987654
    assert out["expires_at_effective"] == now + 90 * 86400


def test_existing_non_time_group_masks_stale_personal_date(stack):
    db, g, m, now, ov = stack
    db.execute("UPDATE members SET group_id='vip' WHERE emby_user_id='u'")
    assert m.get("u")["expires_at_effective"] is None
    # Changing back defaults to the actual unlimited term, not the hidden 90d.
    p = group_preview(m, "u", "standard")
    assert p["decision_required"]
    out = m.upsert("u", "alice", {"group_id": "standard", "expiry_policy": "keep"})
    assert out["expires_at_effective"] is None


def test_bot_group_confirm_can_cancel_then_applies_once(bot):
    run = asyncio.run
    before = bot.members.get("u1")["group_id"]
    run(bot._handle_message(msg("/kk 901", user="900")))
    mid = bot._panel["900"]
    run(bot._handle_callback(cb("admin_group_pick:" + WHITELIST_GROUP_ID, user="900", mid=mid)))
    extra = bot._pending["900"][2]
    action = "admin_group_apply:keep:" + extra["group_confirm"]["nonce"]
    assert bot.members.get("u1")["group_id"] == before
    run(bot._handle_callback(cb("admin_card", user="900", mid=mid)))
    run(bot._handle_callback(cb(action, user="900", mid=mid)))
    assert bot.members.get("u1")["group_id"] == before
    run(bot._handle_callback(cb("admin_group_pick:" + WHITELIST_GROUP_ID, user="900", mid=mid)))
    action = "admin_group_apply:keep:" + bot._pending["900"][2]["group_confirm"]["nonce"]
    run(bot._handle_callback(cb(action, user="900", mid=mid)))
    assert bot.members.get("u1")["group_id"] == WHITELIST_GROUP_ID
    assert bot.members.get("u1")["expires_at_effective"] is None
    stamp = bot.members.get("u1")["updated_at"]
    run(bot._handle_callback(cb(action, user="900", mid=mid)))
    assert bot.members.get("u1")["updated_at"] == stamp


def test_bot_changed_group_definition_invalidates_confirmation(bot):
    run = asyncio.run
    # /prouser grants directly; obtain a real preview from the group menu.
    run(bot._handle_message(msg("/kk 901", user="900")))
    mid = bot._panel["900"]
    run(bot._handle_callback(cb("admin_groups", user="900", mid=mid)))
    run(bot._handle_callback(cb("admin_group_pick:" + WHITELIST_GROUP_ID, user="900", mid=mid)))
    action = "admin_group_apply:keep:" + bot._pending["900"][2]["group_confirm"]["nonce"]
    assert bot.members.get("u1")["group_id"] == "standard"
    bot.groups.update(WHITELIST_GROUP_ID, {"name": "Renamed"})
    bot.calls.clear()
    run(bot._handle_callback(cb(action, user="900", mid=mid)))
    assert bot.members.get("u1")["group_id"] == "standard"
    assert any(
        method == "editMessageText"
        and payload["message_id"] == mid
        and "用户或用户组已变化，请重新预览" in payload["text"]
        for method, payload in bot.calls
    )
