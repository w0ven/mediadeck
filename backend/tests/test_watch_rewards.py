"""Silent settlement acceptance: proof union, cutover, durability and atomic money."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from app.core.db import Database
from app.core.store import SettingsStore
from app.modules.economy_rules import BEIJING, watch_progress
from app.modules.plugins import PluginRegistry
from app.modules.plugins_builtin import PluginContext, register_builtin
from app.modules.points import PointsService
from app.modules.watch_rewards import ActivityService, level_for, reward_total

BASE = datetime(2026, 11, 2, 12, tzinfo=BEIJING).timestamp()


def proof(db, start, end, key="play", uid="u"):
    db.execute("INSERT OR IGNORE INTO watch_verified_samples "
               "(run_key,emby_user_id,started_at,ended_at,seconds,position_start,position_end) "
               "VALUES(?,?,?,?,?,0,?)", (key, uid, start, end, end-start, end-start))


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "activity.db")
    points = PointsService(db)
    service = ActivityService(db, points)
    yield db, points, service
    db.close()


@pytest.mark.parametrize("seconds,expected", [(0, (0, 0, 0)), (299, (0, 0, 0)),
    (300, (1, 8, 5)), (1199, (3, 24, 15)), (1200, (4, 32, 20)),
    (1499, (4, 32, 20)), (1500, (5, 34, 25)), (3600, (12, 48, 60)), (7200, (12, 48, 60))])
def test_rule_boundaries(seconds, expected):
    assert reward_total(seconds) == expected


@pytest.mark.parametrize("growth,level,price", [(0, 1, 80), (119, 1, 80), (120, 2, 75),
    (360, 3, 70), (900, 4, 65), (1800, 5, 60), (3600, 6, 55), (7200, 7, 50)])
def test_level_boundaries(growth, level, price):
    assert level_for(growth)["level"] == level
    assert level_for(growth)["renewal_price"] == price


def test_four_weekly_episodes_cover_lv1_budget(env):
    db, points, service = env
    service.activate(BASE)
    for i in range(4):
        proof(db, BASE+i*7*86400, BASE+i*7*86400+1200, str(i))
    service.settle(BASE+22*86400)
    assert points.balance("u") == 128
    status = service.summary("u", BASE+22*86400)
    assert status["level"]["growth"] == 80
    assert status["qualification"]["eligible"]
    assert status["qualification"]["days"] == 4
    assert 128 + 4 * 10 == 168
    assert 128 + 4 * 1 == 132  # worst permitted luck is still above 80


def test_cutover_is_explicit_and_no_history_backfill(env):
    db, points, service = env
    proof(db, BASE-7200, BASE, "old")
    assert not service.summary("u", BASE)["active"]
    assert not service.settle(BASE)["active"]
    service.activate(BASE)
    service.activate(BASE-86400)  # cannot move immutable cutover backwards
    proof(db, BASE-300, BASE+300, "cross")
    proof(db, BASE-3600, BASE-10, "late-old")
    service.settle(BASE+300)
    assert points.balance("u") == 8
    assert service.summary("u", BASE+300)["today"]["verified_seconds"] == 300
    assert service.summary("u", BASE+300)["effective_at"] == BASE


def test_fragments_overlap_late_duplicate_and_cross_midnight(env):
    db, points, service = env
    midnight = BASE + 12*3600
    service.activate(BASE)
    proof(db, midnight-350, midnight+350, "one")
    proof(db, midnight-150, midnight+100, "two")
    service.settle(midnight+350)
    assert points.balance("u") == 16
    proof(db, midnight-400, midnight+100, "late")
    proof(db, midnight-350, midnight+350, "one")  # collector duplicate
    service.settle(midnight+350)
    rows = db.query("SELECT * FROM activity_watch_days ORDER BY day")
    assert [r["verified_seconds"] for r in rows] == [400, 350]
    assert points.balance("u") == 16
    assert watch_progress(db, "u", midnight+350)[1] == 350


@pytest.mark.parametrize("minutes", [60, 120])
def test_daily_caps(env, minutes):
    db, points, service = env
    service.activate(BASE)
    proof(db, BASE, BASE+minutes*60)
    proof(db, BASE, BASE+minutes*60, "second-device")
    service.settle(BASE+minutes*60)
    assert points.balance("u") == 48
    assert service.summary("u", BASE+minutes*60)["level"]["growth"] == 60


def test_money_failure_rolls_back_all_watermarks(env, monkeypatch):
    db, points, service = env
    service.activate(BASE)
    proof(db, BASE, BASE+600)
    original = points._apply
    def fail(*args):
        original(*args)
        raise RuntimeError("injected")
    monkeypatch.setattr(points, "_apply", fail)
    with pytest.raises(RuntimeError, match="injected"):
        service.settle(BASE+600)
    assert points.balance("u") == 0
    assert not db.query("SELECT * FROM growth_ledger")
    assert not db.query("SELECT * FROM activity_watch_days")
    assert not db.query("SELECT * FROM activity_watch_intervals")
    assert len(db.query("SELECT * FROM activity_watch_pending")) == 1
    monkeypatch.setattr(points, "_apply", original)
    assert service.settle(BASE+600)["points"] == 16


def test_restart_retention_and_concurrent_connections(env):
    db, points, service = env
    service.activate(BASE)
    proof(db, BASE, BASE+300)
    second = Database(db.path)
    try:
        other = ActivityService(second, PointsService(second))
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(lambda s: s.settle(BASE+600), [service, other]))
        assert sum(r["points"] for r in result) == 8
        # Raw proof retention cannot erase the reward watermarks or pending proof.
        proof(db, BASE+300, BASE+600)
        db.execute("DELETE FROM watch_verified_samples")
        second._migrate()
        assert other.settle(BASE+600)["points"] == 8
        proof(db, BASE, BASE+600, "late-replay")
        assert other.settle(BASE+600)["points"] == 0
        assert points.balance("u") == 16
    finally:
        second.close()


def test_preview_never_settles_and_growth_survives_spending(env):
    db, points, service = env
    service.activate(BASE)
    proof(db, BASE, BASE+600)
    assert service.summary("u", BASE+600)["today"]["points"] == 0
    assert points.balance("u") == 0
    service.settle(BASE+600)
    points.add("u", -16, "shop.redeem")
    assert points.balance("u") == 0
    assert service.summary("u", BASE+600)["level"]["growth"] == 10


def test_rolling_qualification_clips_partial_days(env):
    db, _, service = env
    service.activate(BASE)
    for i, seconds in enumerate([600, 600, 3600]):
        proof(db, BASE+i*86400, BASE+i*86400+seconds, str(i))
    service.settle(BASE+3*86400)
    assert service.summary("u", BASE+3*86400)["qualification"]["eligible"]
    assert not service.summary("u", BASE+30*86400+1)["qualification"]["eligible"]


@pytest.mark.asyncio
async def test_registered_scheduler_not_ui_pays_and_defaults_off(env, tmp_path, monkeypatch):
    db, points, service = env
    store = SettingsStore(tmp_path / "settings.json")
    ctx = PluginContext(db=db, points=points, activity=service, store=store)
    registry = register_builtin(PluginRegistry(store, db), ctx)
    monkeypatch.setattr("time.time", lambda: BASE)
    assert "watch_rewards" in registry.ids()
    assert not registry.enabled("watch_rewards")
    await registry.run_now("watch_rewards")
    assert not service.summary("u", BASE)["active"]
    registry.save("watch_rewards", enabled=True, config={"activate_rewards": True})
    await registry.tick(BASE+60)
    proof(db, BASE, BASE+1200)
    monkeypatch.setattr("time.time", lambda: BASE+1200)
    await registry.tick(BASE+1200)
    assert points.balance("u") == 32
    assert registry.last_result("watch_rewards")["ok"]
    registry.save("watch_rewards", enabled=False)
    proof(db, BASE+1200, BASE+1500)
    await registry.run_now("watch_rewards")
    assert points.balance("u") == 32
