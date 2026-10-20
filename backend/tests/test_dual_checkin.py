"""Dual check-in through actual services and received Bot updates, never live sends."""
import asyncio
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_checkin_interaction import economy_env, interaction_env, message, posted  # noqa: F401
from test_checkin_interaction import env as bot_env  # noqa: F401
from test_economy import NOW, build, proof
from test_economy import env as economy_fixture  # noqa: F401
from test_tg_interaction_context import GROUP, VIEWER

from app.core.db import Database
from app.modules.dual_checkin import LUCK_SIZE, VERSION, luck_value
from app.modules.economy_rules import economy_write
from app.modules.play_money import CashBook


@pytest.fixture
def env(request):
    e = request.getfixturevalue("economy_fixture")
    e.registry.save("checkin", config={"dual_checkin": True, "weekends": False, "holidays": "[]"})
    proof(e.db, "u")
    return e


def test_exact_distribution_has_every_positive_integer_and_mean_ten():
    counts = Counter(luck_value(n) for n in range(LUCK_SIZE))
    assert counts == {**{n: 65 for n in range(1, 11)}, **{n: 25 for n in range(11, 21)},
                      **{n: 10 for n in range(21, 31)}}
    assert sum(n*weight for n, weight in counts.items()) == 10000


@pytest.mark.parametrize("roll,reward", [(0, 1), (129, 2), (130, 3), (649, 10),
                                        (650, 11), (899, 20), (900, 21), (950, 26), (999, 30)])
def test_normal_then_luck_exact_net_and_terminal(env, monkeypatch, roll, reward):
    calls = []
    def fixed(secret, uid, day, domain, size):
        calls.append(domain)
        return roll if domain.endswith(":luck") else 999999
    monkeypatch.setattr("app.modules.dual_checkin.draw", fixed)
    normal = env.checkin.checkin("u", NOW)
    assert normal["points"] == 10 and normal["streak"] == 1
    assert "可能更低" in normal["warning"]
    lucky = env.checkin.checkin("u", NOW, mode="luck")
    assert lucky["points"] == reward and lucky["delta"] == reward-10
    assert lucky["streak"] == 1
    assert env.points.balance("u") == reward
    for mode in ("luck", "normal", "luck"):
        duplicate = env.checkin.checkin("u", NOW, mode=mode)
        assert not duplicate["ok"] and duplicate["saved_result"]["points"] == reward
    assert calls == [VERSION+":drop", VERSION+":luck"]
    assert not env.db.query("SELECT * FROM growth_ledger")
    assert sum(r["delta"] for r in env.points.ledger("u")) == reward


@pytest.mark.parametrize("spend", ["shop", "transfer", "escrow"])
def test_recovery_requires_spendable_balance_before_any_draw(env, monkeypatch, spend):
    env.checkin.checkin("u", NOW)
    if spend == "shop": env.points.add("u", -2, "shop.redeem")
    elif spend == "transfer": env.points.transfer("u", "v", 2)
    else:
        with economy_write(env.db) as conn:
            CashBook(env.points).reserve(conn, "test", "room", "u", 2)
    def no_draw(*args): raise AssertionError("must reject before draw")
    monkeypatch.setattr("app.modules.dual_checkin.draw", no_draw)
    result = env.checkin.checkin("u", NOW, mode="luck")
    assert not result["ok"] and result["shortfall"] == 1
    assert env.points.balance("u") == 8
    assert json.loads(env.db.one("SELECT result_json FROM checkins")["result_json"])["mode"] == "normal"


def test_direct_luck_no_streak_or_holiday_point_multiplier_and_single_drop(env, monkeypatch):
    config = {"weekends": True, "double_ppm": 1000000, "multiplier": 100,
              "streak_tiers": '[{"days":1,"percent":10000}]',
              "drops": '[{"ppm":1000000,"spec":{"kind":"invite_card","name":"Demo","amount":1,"duration_days":0}}]'}
    env.registry.save("checkin", config=config)
    monkeypatch.setattr("app.modules.dual_checkin.draw", lambda *args: 0)
    result = env.checkin.checkin("u", NOW, mode="luck")
    assert result["points"] == 1 and result["bonus"] == 0 and result["multiplier"] == 1
    env.checkin.checkin("u", NOW)
    env.checkin.checkin("u", NOW, mode="luck")
    assert len(env.db.query("SELECT * FROM inventory")) == 1
    assert env.points.balance("u") == 1


def test_concurrent_connections_and_restart_do_not_redraw(env, monkeypatch):
    monkeypatch.setattr("app.modules.dual_checkin.draw", lambda *args: 0)
    env.checkin.checkin("u", NOW)
    other_db = Database(env.db.path)
    try:
        other = build(other_db, env.checkin.ctx.store)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda e: e.checkin.checkin("u", NOW, mode="luck"), [env, other]))
        assert sum(bool(r["ok"]) for r in results) == 1
        assert other.points.balance("u") == 1
        assert other.checkin.checkin("u", NOW, mode="luck")["saved_result"]["points"] == 1
    finally:
        other_db.close()


def test_legacy_settlement_and_balance_are_untouched(env, monkeypatch):
    env.registry.save("checkin", config={"dual_checkin": False})
    old = env.checkin.checkin("u", NOW)
    env.registry.save("checkin", config={"dual_checkin": True})
    before = env.points.balance("u")
    def no_draw(*args): raise AssertionError("old daily receipt cannot be rerolled")
    monkeypatch.setattr("app.modules.dual_checkin.draw", no_draw)
    result = env.checkin.checkin("u", NOW, mode="luck")
    assert not result["ok"] and "旧规则" in result["reason"]
    assert result["saved_result"]["points"] == old["points"]
    assert env.points.balance("u") == before


@pytest.mark.parametrize("chat", [VIEWER, GROUP])
@pytest.mark.parametrize("text", ["运气签到", "/luckcheckin", "/luckcheckin@MediaDeckDemoBot"])
def test_actual_bot_received_commands_preserve_reply_and_show_net(bot_env, monkeypatch, chat, text):  # noqa: F811
    e = bot_env
    e.services.registry.save("checkin", config={"dual_checkin": True})
    proof(e.db, "u1", now=time.time())
    monkeypatch.setattr("app.modules.dual_checkin.draw", lambda *args: 130 if args[3].endswith(":luck") else 999999)
    async def run():
        await e.bot._dispatch_update({"message": message("签到", chat=chat)})
        assert "结果可能更低" in posted(e)[-1]["text"]
        await e.bot._dispatch_update({"message": message(text, chat=chat, mid=302)})
        assert "净调整 -7" in posted(e)[-1]["text"]
        await e.bot._dispatch_update({"message": message(text, chat=chat, mid=303)})
        assert "最终奖励：<b>3" in posted(e)[-1]["text"]
        assert posted(e)[-1]["reply_parameters"]["message_id"] == 303
    asyncio.run(run())


def test_bot_member_renew_code_and_level_card_use_same_services(bot_env):  # noqa: F811
    from app.modules.levels import DAY, LevelService, defaults
    from app.modules.watch_rewards import ActivityService
    e = bot_env
    levels = LevelService(e.db, e.services.members, e.services.points, ActivityService(e.db, e.services.points))
    e.services.members.bind_levels(levels)
    e.members.bind_levels(levels)
    e.services.points.add('u1', 100, 'admin.adjust')
    config = defaults(); config['enabled'] = True
    levels.save_config(config, 0)
    now = int(time.time())
    e.db.execute("INSERT INTO activity_members(emby_user_id,enrolled_at) VALUES('u1',?)", (now,))
    e.db.execute("UPDATE members SET expires_at=? WHERE emby_user_id='u1'", (now+DAY,))
    for i in range(3): proof(e.db, 'u1', now=now-i*DAY, seconds=1600, key='renew'+str(i))
    before = e.services.points.balance('u1')
    async def run():
        await e.bot._dispatch_update({'message': message('/activity')})
        assert '/renew30' in posted(e)[-1]['text']
        await e.bot._dispatch_update({'message': message('/webaccount', mid=302)})
        assert '一次性登录码' in posted(e)[-1]['text']
        await e.bot._dispatch_update({'message': message('/renew30', mid=303)})
        assert '已入账' in posted(e)[-1]['text']
        await e.bot._dispatch_update({'message': message('/renew30', mid=303)})
        assert e.services.points.balance('u1') == before-80
    asyncio.run(run())
    card = e.bot._account_card(e.services.members.get('u1'))
    assert 'Lv1 布衣' in card and '续费预算 80' in card and '今日观影奖励' in card
