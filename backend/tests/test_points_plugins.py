"""Check-in and transfer differ from the task plugins: a member triggers them,
and ``run()`` only reports. That shifts what is worth testing.

- **A day pays once.** The whole feature is "you showed up today", so a second
  tap -- or a double-tap on a slow connection -- must not pay twice.
- **A streak is consecutive or it is not.** Missing a day resets it; the bonus
  is capped, because at +5/day an uninterrupted year is 1800 points of pure
  consistency and the top of the ledger runs away from everyone else.
- **The daily transfer cap is what limits the damage** when an account is
  taken over, so it is pinned at the boundary rather than approximately.
- **A disabled plugin has no button.** The keyboard is built from the switch,
  so turning the feature off has to remove it rather than make it fail.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.plugins import PluginRegistry
from app.modules.plugins_builtin import PluginContext, register_builtin
from app.modules.points import PointsService
from app.modules.telegram import TelegramBot

FAKE_CRED = "1234567" + ":" + "placeholder-not-a-real-credential"

DAY = 86400


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def set_section(self, name: str, value: dict[str, Any]) -> None:
        self.data[name] = value


@pytest.fixture()
def stack(tmp_path):
    db = Database(tmp_path / "plugins.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    points = PointsService(db)
    store = FakeStore()
    ctx = PluginContext(members=members, db=db, store=store, points=points)
    registry = register_builtin(PluginRegistry(store, db), ctx)
    members.upsert("u1", "alice", {"group_id": "standard"}, actor="test")
    members.upsert("u2", "bob", {"group_id": "standard"}, actor="test")
    return registry, points, members, db


@pytest.fixture()
def checkin(stack):
    return stack[0].get("checkin")


@pytest.fixture()
def transfer(stack):
    return stack[0].get("points_transfer")


def _configure(registry: PluginRegistry, plugin_id: str, **config) -> None:
    registry.save(plugin_id, enabled=True, config=config)


# -- check-in ----------------------------------------------------------------

def test_a_first_checkin_pays_the_base_rate(stack, checkin) -> None:
    registry, points, _, _ = stack
    _configure(registry, "checkin", points_per_day=10, streak_bonus=5,
               max_streak_bonus=50)

    result = checkin.checkin("u1")
    assert result["ok"] is True
    # Day one is not "coming back", so it earns no streak bonus.
    assert result["points"] == 10 and result["bonus"] == 0
    assert result["streak"] == 1
    assert result["balance"] == 10
    assert points.balance("u1") == 10


def test_checking_in_twice_on_one_day_is_refused_without_paying(
        stack, checkin) -> None:
    registry, points, _, _ = stack
    _configure(registry, "checkin", points_per_day=10)

    assert checkin.checkin("u1")["ok"] is True
    second = checkin.checkin("u1")
    assert second["ok"] is False
    assert second["reason"] == "今天已签到"
    assert second["balance"] == 10
    assert points.balance("u1") == 10
    assert len(points.ledger("u1")) == 1


def test_a_streak_accumulates_across_consecutive_days(stack, checkin) -> None:
    registry, points, _, _ = stack
    _configure(registry, "checkin", points_per_day=10, streak_bonus=5,
               max_streak_bonus=100)
    now = time.time()

    day1 = checkin.checkin("u1", now=now - 2 * DAY)
    day2 = checkin.checkin("u1", now=now - DAY)
    day3 = checkin.checkin("u1", now=now)

    assert [d["streak"] for d in (day1, day2, day3)] == [1, 2, 3]
    assert [d["points"] for d in (day1, day2, day3)] == [10, 15, 20]
    assert points.balance("u1") == 45


def test_missing_a_day_restarts_the_streak(stack, checkin) -> None:
    registry, _, _, _ = stack
    _configure(registry, "checkin", points_per_day=10, streak_bonus=5,
               max_streak_bonus=100)
    now = time.time()

    checkin.checkin("u1", now=now - 3 * DAY)
    checkin.checkin("u1", now=now - 2 * DAY)  # streak 2
    # nothing on day -1: the chain is broken
    after_gap = checkin.checkin("u1", now=now)

    assert after_gap["streak"] == 1
    # Back to base pay -- but they did check in, so it is 1 rather than 0.
    assert after_gap["points"] == 10


def test_the_streak_bonus_stops_at_its_cap(stack, checkin) -> None:
    registry, _, _, _ = stack
    _configure(registry, "checkin", points_per_day=10, streak_bonus=5,
               max_streak_bonus=20)
    now = time.time()

    awards = [checkin.checkin("u1", now=now - (9 - i) * DAY)["points"]
              for i in range(10)]
    # 10, 15, 20, 25, then capped at 10+20 forever.
    assert awards[:4] == [10, 15, 20, 25]
    assert set(awards[4:]) == {30}


def test_a_zero_bonus_turns_the_streak_reward_off(stack, checkin) -> None:
    registry, _, _, _ = stack
    _configure(registry, "checkin", points_per_day=10, streak_bonus=0,
               max_streak_bonus=50)
    now = time.time()

    first = checkin.checkin("u1", now=now - DAY)
    second = checkin.checkin("u1", now=now)
    assert second["streak"] == 2
    assert first["points"] == second["points"] == 10


def test_checkins_are_per_member(stack, checkin) -> None:
    registry, points, _, _ = stack
    _configure(registry, "checkin", points_per_day=10)
    checkin.checkin("u1")
    assert checkin.checkin("u2")["ok"] is True
    assert points.balance("u1") == points.balance("u2") == 10


def test_a_checkin_without_a_member_id_is_refused(stack, checkin) -> None:
    _configure(stack[0], "checkin", points_per_day=10)
    assert checkin.checkin("")["ok"] is False


def test_the_checkin_card_reports_todays_totals(stack, checkin) -> None:
    import asyncio
    registry, _, _, _ = stack
    _configure(registry, "checkin", points_per_day=10, streak_bonus=5,
               max_streak_bonus=50)
    now = time.time()
    checkin.checkin("u1", now=now - DAY)
    checkin.checkin("u1", now=now)
    checkin.checkin("u2", now=now)

    summary = asyncio.run(registry.run_now("checkin"))
    assert summary["ok"] is True
    assert summary["今日签到人数"] == 2
    assert summary["今日发出积分"] == 25  # 15 (streak 2) + 10
    assert summary["今日最长连签"] == 2
    assert summary["累计签到次数"] == 3


# -- transfer ----------------------------------------------------------------

def test_a_transfer_within_the_rules_is_allowed(stack, transfer) -> None:
    registry, points, _, _ = stack
    _configure(registry, "points_transfer", enabled_for_members=True,
               daily_limit=500, min_amount=1, fee_percent=0)
    points.add("u1", 200, "checkin")

    assert transfer.can_transfer("u1", 100) == (True, "")
    result = transfer.transfer("u1", "u2", 100)
    assert result["received"] == 100
    assert points.balance("u1") == 100
    assert points.balance("u2") == 100


def test_the_daily_limit_is_a_sum_not_a_per_transfer_cap(stack,
                                                          transfer) -> None:
    registry, points, _, _ = stack
    _configure(registry, "points_transfer", daily_limit=500, min_amount=1)
    points.add("u1", 1000, "checkin")

    transfer.transfer("u1", "u2", 300)
    ok, reason = transfer.can_transfer("u1", 201)
    assert ok is False and "每日转出上限" in reason
    # Exactly at the cap is still allowed: the limit is inclusive.
    assert transfer.can_transfer("u1", 200)[0] is True
    transfer.transfer("u1", "u2", 200)
    assert transfer.can_transfer("u1", 1)[0] is False
    assert points.balance("u2") == 500


def test_a_zero_daily_limit_means_no_limit(stack, transfer) -> None:
    registry, points, _, _ = stack
    _configure(registry, "points_transfer", daily_limit=0, min_amount=1)
    points.add("u1", 10_000, "checkin")
    transfer.transfer("u1", "u2", 9_000)
    assert transfer.can_transfer("u1", 1000)[0] is True


def test_the_fee_is_taken_from_the_sender_and_destroyed(stack,
                                                        transfer) -> None:
    registry, points, _, _ = stack
    _configure(registry, "points_transfer", daily_limit=0, fee_percent=10)
    points.add("u1", 500, "checkin")

    assert transfer.fee_for(100) == 10
    result = transfer.transfer("u1", "u2", 100)
    assert result["fee"] == 10 and result["received"] == 90
    assert points.balance("u1") == 400
    assert points.balance("u2") == 90
    # 10 points left the economy entirely.
    assert points.balance("u1") + points.balance("u2") == 490


def test_no_fee_configured_means_the_full_amount_arrives(stack,
                                                         transfer) -> None:
    registry, points, _, _ = stack
    _configure(registry, "points_transfer", fee_percent=0, daily_limit=0)
    points.add("u1", 500, "checkin")
    assert transfer.fee_for(100) == 0
    assert transfer.transfer("u1", "u2", 100)["received"] == 100


def test_a_transfer_below_the_minimum_is_refused(stack, transfer) -> None:
    registry, points, _, _ = stack
    _configure(registry, "points_transfer", min_amount=50, daily_limit=0)
    points.add("u1", 500, "checkin")

    ok, reason = transfer.can_transfer("u1", 10)
    assert ok is False and "至少" in reason
    assert transfer.can_transfer("u1", 50)[0] is True


def test_transferring_more_than_the_balance_is_refused(stack,
                                                       transfer) -> None:
    registry, points, _, _ = stack
    _configure(registry, "points_transfer", daily_limit=0)
    points.add("u1", 30, "checkin")

    ok, reason = transfer.can_transfer("u1", 40)
    assert ok is False and "积分不足" in reason
    with pytest.raises(ValueError):
        transfer.transfer("u1", "u2", 40)
    assert points.balance("u1") == 30
    assert points.balance("u2") == 0


def test_a_disabled_transfer_plugin_refuses_at_the_service_too(stack,
                                                               transfer) -> None:
    """Hiding the button is not the only defence: the call is refused too."""
    registry, points, _, _ = stack
    registry.save("points_transfer", enabled=True,
                  config={"enabled_for_members": False})
    points.add("u1", 500, "checkin")

    ok, reason = transfer.can_transfer("u1", 10)
    assert ok is False and "关闭" in reason
    with pytest.raises(ValueError):
        transfer.transfer("u1", "u2", 10)


def test_non_numeric_amounts_are_refused(stack, transfer) -> None:
    _configure(stack[0], "points_transfer", daily_limit=0)
    stack[1].add("u1", 500, "checkin")
    assert transfer.can_transfer("u1", "many")[0] is False


def test_the_transfer_card_reports_todays_traffic(stack, transfer) -> None:
    import asyncio
    registry, points, _, _ = stack
    _configure(registry, "points_transfer", daily_limit=500, fee_percent=0)
    points.add("u1", 500, "checkin")
    transfer.transfer("u1", "u2", 100)
    transfer.transfer("u1", "u2", 50)

    summary = asyncio.run(registry.run_now("points_transfer"))
    assert summary["ok"] is True
    assert summary["今日转账笔数"] == 2
    assert summary["今日转出积分"] == 150
    assert summary["每日上限"] == 500
    assert summary["状态"] == "开启"


# -- the keyboard follows the switches ---------------------------------------

def _bot(registry: PluginRegistry, points: PointsService,
         members: MemberService) -> TelegramBot:
    cfg = {"enabled": True, "bot_token": FAKE_CRED}
    return TelegramBot(lambda: cfg, members, points=points, plugins=registry)


def test_disabled_points_plugins_show_no_buttons(stack) -> None:
    registry, points, members, _ = stack
    bot = _bot(registry, points, members)
    actions = {b["callback_data"] for row in bot.member_menu() for b in row}
    assert "checkin" not in actions
    assert "transfer" not in actions
    # The rest of the menu is unaffected by a points feature being off.
    assert {"me", "bag", "top"} <= actions


def test_enabling_a_plugin_makes_its_button_appear(stack) -> None:
    registry, points, members, _ = stack
    bot = _bot(registry, points, members)

    registry.save("checkin", enabled=True)
    actions = {b["callback_data"] for row in bot.member_menu() for b in row}
    assert "checkin" in actions and "transfer" not in actions

    registry.save("points_transfer", enabled=True)
    actions = {b["callback_data"] for row in bot.member_menu() for b in row}
    assert {"checkin", "transfer"} <= actions


def test_switching_a_plugin_off_again_removes_its_button(stack) -> None:
    registry, points, members, _ = stack
    bot = _bot(registry, points, members)
    registry.save("checkin", enabled=True)
    assert "checkin" in {b["callback_data"] for r in bot.member_menu() for b in r}

    registry.save("checkin", enabled=False)
    assert "checkin" not in {b["callback_data"]
                             for r in bot.member_menu() for b in r}


def test_a_bot_with_no_registry_offers_no_points_buttons(stack) -> None:
    """A panel that never wired the plugins must not promise features."""
    _, points, members, _ = stack
    bot = TelegramBot(lambda: {"enabled": True, "bot_token": FAKE_CRED},
                      members, points=points)
    actions = {b["callback_data"] for row in bot.member_menu() for b in row}
    assert "checkin" not in actions and "transfer" not in actions
