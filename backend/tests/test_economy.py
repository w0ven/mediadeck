"""Behavioral contracts: real sampler evidence, independent SQLite connections,
immutable purchased cards, non-destructive expiration, and Telegram uncertainty.
No production writes or live Telegram delivery are performed here.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.adapters.mock import MockEmby
from app.core.config import settings
from app.core.db import Database
from app.main import app
from app.modules.economy_rules import (
    BEIJING,
    DEFAULT_CARDS,
    PPM,
    activity_for,
    day_bounds,
    draw,
    encode,
    unbiased_map,
    watch_progress,
)
from app.modules.groups import GroupService
from app.modules.inventory import InventoryService, validate_title
from app.modules.member_rewards import grant_reward
from app.modules.members import MemberService
from app.modules.plugins import PluginRegistry
from app.modules.plugins_builtin import PluginContext, register_builtin
from app.modules.plugins_points import CHECKIN_BASE_SIZE, checkin_base
from app.modules.points import PointsService
from app.modules.shop import ShopError, ShopService
from app.modules.titles import TitleService
from app.modules.usage import UsageSampler

SECRET = "test-only-persistent-secret-material-0123456789"
NOW = datetime(2026, 10, 10, 12, tzinfo=BEIJING).timestamp()  # Saturday


class Store:
    def __init__(self):
        self.data = {}

    def section(self, key):
        return self.data.get(key, {})

    def set_section(self, key, value):
        self.data[key] = value


def build(db, store=None):
    groups = GroupService(db)
    members = MemberService(db, groups)
    points = PointsService(db)
    shop = ShopService(db, members, points)
    ctx = PluginContext(db=db, members=members, points=points, shop=shop, store=store or Store())
    registry = register_builtin(PluginRegistry(ctx.store, db), ctx)
    bag = InventoryService(db, members, shop, lambda: registry.config("inventory"))
    return SimpleNamespace(
        db=db,
        groups=groups,
        members=members,
        points=points,
        shop=shop,
        registry=registry,
        bag=bag,
        checkin=registry.get("checkin"),
        transfer=registry.get("points_transfer"),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIADECK_CHECKIN_SECRET", SECRET)
    settings.cache_clear()
    clock = [NOW]
    monkeypatch.setattr("time.time", lambda: clock[0])
    db = Database(tmp_path / "economy.db")
    db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(NOW - 86400),))
    e = build(db)
    e.groups.seed_defaults()
    for uid in ("u", "v"):
        e.members.upsert(uid, uid, {"group_id": "standard"})
        e.members.set_overrides(uid, {"bandwidth_limit_kbps": 20 * 1024, "max_streams": 2})
    e.registry.save("checkin", enabled=True)
    e.registry.save("points_transfer", enabled=True)
    e.clock = clock
    yield e
    db.close()


def proof(db, user, now=NOW, seconds=600, key=None):
    # Isolated settlement fixture; tests below separately exercise the sampler.
    start = now - seconds
    db.execute(
        "INSERT INTO watch_verified_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            key or user + str(now),
            user,
            "film",
            "Demo",
            "Movie",
            "",
            "test",
            "",
            start,
            now,
            seconds,
            0,
            seconds,
            1,
        ),
    )


def play(at, pos, sid="s", device="d", play_id="p", paused=False, heartbeat=None):
    return {
        "Id": sid,
        "UserId": "u",
        "DeviceId": device,
        "PlaySessionId": play_id,
        "LastActivityDate": datetime.fromtimestamp(
            at if heartbeat is None else heartbeat, UTC
        ).isoformat(),
        "NowPlayingItem": {"Id": "film", "Name": "Demo", "Type": "Movie", "RunTimeTicks": 3600 * 10000000},
        "PlayState": {"IsPaused": paused, "PositionTicks": pos * 10000000, "PlaybackRate": 1},
    }


def test_unbiased_rejection_boundaries_and_all_41_endpoints():
    accepted = [unbiased_map(i, 41, bits=8) for i in range(256)]
    assert accepted[:41] == list(range(41))
    assert accepted[246:] == [None] * 10
    assert all(accepted.count(i) == 6 for i in range(41))
    assert unbiased_map(0, 41) - 10 == -10 and unbiased_map(40, 41) - 10 == 30
    limit = 2**256 - (2**256 % 41)
    assert unbiased_map(limit - 1, 41) == 40
    assert unbiased_map(limit, 41) is None
    assert unbiased_map(2**256 - 1, PPM) is None


def test_hmac_determinism_domain_separation_and_range():
    samples = [draw(SECRET, str(i), "2026-10-10", "base", 41) - 10 for i in range(600)]
    assert set(samples) == set(range(-10, 31))
    assert draw(SECRET, "u", "2026-10-10", "base", 41) == draw(
        SECRET, "u", "2026-10-10", "base", 41
    )
    assert draw(SECRET, "u", "2026-10-10", "lucky", PPM) != draw(
        SECRET, "u", "2026-10-10", "drop", PPM
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"double_ppm": -1},
        {"double_ppm": PPM + 1},
        {"double_ppm": 0.5},
        {"multiplier": 0},
        {
            "drops": encode([{"ppm": PPM, "spec": DEFAULT_CARDS[0]}, {"ppm": 1, "spec": DEFAULT_CARDS[1]}])
        },
        {"drops": encode([{"ppm": 0.5, "spec": DEFAULT_CARDS[0]}])},
        {"streak_tiers": "[]"},
        {"streak_tiers": encode([{"days": 2, "bonus": 1}])},
        {"streak_tiers": encode([{"days": 1, "bonus": 0}, {"days": 1, "bonus": 2}])},
        {
            "holidays": encode([{"date": "2026-10-10", "name": "a"}, {"date": "2026-10-10", "name": "b"}])
        },
        {"holidays": encode([{"date": "2026-02-30", "name": "bad"}])},
    ],
)
def test_invalid_config_is_rejected_without_saving(env, changes):
    before = env.registry.config("checkin")
    with pytest.raises(ValueError):
        env.registry.save("checkin", config=changes)
    assert env.registry.config("checkin") == before


def test_holiday_overrides_weekend_without_stacking_and_annual_lunar_dates(env):
    config = env.registry.config("checkin")
    holidays = json.loads(config["holidays"])
    assert {"2026-02-17", "2027-02-06"} <= {r["date"] for r in holidays}
    env.registry.save(
        "checkin",
        config={
            "holidays": encode([{"date": "2026-10-10", "name": "特别节日", "double_ppm": 0, "multiplier": 3}])
        },
    )
    activity = activity_for(env.registry.config("checkin"), NOW)
    assert (
        activity["event"] == "holiday"
        and activity["double_ppm"] == 0
        and activity["multiplier"] == 3
    )
    assert activity_for(config, NOW)["event"] == "weekend"
    assert activity_for(config, NOW - 86400)["event"] == "normal"


def test_two_five_minute_plays_sum_to_gate_and_duplicate_heartbeats_do_not(env):
    sampler = UsageSampler(env.db, env.members, MockEmby())
    first = NOW - 300
    for seconds in range(0, 301, 30):
        sessions = [
            play(first + seconds, seconds),
            play(first + seconds, seconds, "s2", "d2", "p2"),
        ]
        sampler._sample(sessions + sessions, first + seconds, None)
        if seconds == 270:
            result = env.checkin.checkin("u", now=first + seconds)
            assert not result["ok"] and result["watched_seconds"] == 540
    assert watch_progress(env.db, "u", NOW)[1] == 600
    assert env.checkin.checkin("u", now=NOW)["ok"]


def test_cross_day_clips_each_interval_in_beijing_not_process_timezone(env):
    _, midnight, _ = day_bounds(NOW)
    env.db.execute(
        "UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(midnight - 60),)
    )
    sampler = UsageSampler(env.db, env.members, MockEmby())
    for offset, pos in ((-30, 0), (0, 30), (30, 60)):
        sampler._sample([play(midnight + offset, pos)], midnight + offset, None)
    assert watch_progress(env.db, "u", midnight + 30)[1] == 30
    assert watch_progress(env.db, "u", midnight - 0.001)[1] == pytest.approx(29.999)
    assert env.checkin.checkin("u", now=midnight + 30)["watched_seconds"] == 30


def test_pause_seek_strong_exit_frozen_snapshot_never_ghost_counts(env):
    sampler = UsageSampler(env.db, env.members, MockEmby())
    for offset, pos, paused in (
        (0, 0, False),
        (30, 30, False),
        (60, 30, True),
        (90, 30, False),
        (120, 60, False),
        (150, 900, False),
    ):
        sampler._sample([play(NOW + offset, pos, paused=paused)], NOW + offset, None)
    assert watch_progress(env.db, "u", NOW + 150)[1] == 60
    for offset in (180, 210, 3600, 43200):
        sampler._sample([play(NOW + offset, 900, heartbeat=NOW + 150)], NOW + offset, None)
    assert watch_progress(env.db, "u", NOW + 43200)[1] == 0  # new Beijing day
    assert env.db.one("SELECT seconds FROM watch_verified_totals")["seconds"] == 60
    sampler._sample([], NOW + 43300, None)
    assert env.db.one("SELECT seconds FROM watch_verified_totals")["seconds"] == 60


def test_successful_stopped_is_immediate_even_with_later_cached_progress_and_restart(env):
    sampler = UsageSampler(env.db, env.members, MockEmby())
    sampler._sample([play(NOW, 0)], NOW, None)
    sampler._sample([play(NOW + 30, 30)], NOW + 30, None)
    asyncio.run(sampler.playback_report("u", "d", {"PlaySessionId": "p"}, stopped=True))
    for offset in (60, 90, 120):
        sampler._sample([play(NOW + offset, offset)], NOW + offset, None)
    assert env.db.one("SELECT seconds FROM watch_verified_totals")["seconds"] == 30
    sampler = UsageSampler(env.db, env.members, MockEmby())
    sampler._sample([play(NOW + 150, 150)], NOW + 150, None)
    sampler._sample([play(NOW + 180, 180)], NOW + 180, None)
    assert env.db.one("SELECT seconds FROM watch_verified_totals")["seconds"] == 30
    asyncio.run(sampler.playback_report("u", "d", {"PlaySessionId": "p"}, stopped=False))
    sampler._sample([play(NOW + 210, 210)], NOW + 210, None)
    sampler._sample([play(NOW + 240, 240)], NOW + 240, None)
    assert env.db.one("SELECT seconds FROM watch_verified_totals")["seconds"] == 60
    asyncio.run(sampler.playback_report("u", "d", {"PlaySessionId": "old"}, stopped=True))
    sampler._sample([play(NOW + 270, 270)], NOW + 270, None)
    assert env.db.one("SELECT seconds FROM watch_verified_totals")["seconds"] == 90


def test_599_seconds_refused_then_exactly_600_permitted(env):
    proof(env.db, "u", seconds=599)
    result = env.checkin.checkin("u")
    assert not result["ok"] and result["watched_seconds"] == 599
    proof(env.db, "u", now=NOW - 599, seconds=1, key="last-one")
    assert env.checkin.checkin("u")["ok"]


def test_negative_base_not_multiplied_drop_independent_and_snapshot_stable(env):
    day = day_bounds(NOW)[0]
    user = next(str(i) for i in range(100000) if checkin_base(draw(SECRET, str(i), day, "base", CHECKIN_BASE_SIZE)) == -10)
    env.members.upsert(user, user, {"group_id": "standard"})
    proof(env.db, user)
    env.registry.save(
        "checkin", config={"double_ppm": PPM, "drops": encode([{"ppm": PPM, "spec": DEFAULT_CARDS[0]}])}
    )
    result = env.checkin.checkin(user)
    assert (
        result["base"] == -10
        and result["multiplier"] == 1
        and result["points"] == result["balance"] == -10
    )
    assert len(env.bag.items(user)) == 1
    env.registry.save(
        "checkin", config={"multiplier": 5, "streak_tiers": encode([{"days": 1, "bonus": 100}])}
    )
    again = env.checkin.checkin(user)
    assert not again["ok"] and again["saved_result"] == result
    with pytest.raises(ValueError):
        env.transfer.transfer(user, "v", 1)
    item = env.shop.create(dict(DEFAULT_CARDS[0], enabled=True))
    with pytest.raises(ValueError):
        env.shop.redeem(user, item["id"])
    assert len(env.bag.items(user)) == 1
    # Credits must work while still negative.
    env.points.add(user, 1, "transfer.in")
    assert env.points.balance(user) == -9


def test_positive_lucky_before_streak_and_one_drop(env):
    day = day_bounds(NOW)[0]
    user = next(str(i) for i in range(1000) if checkin_base(draw(SECRET, str(i), day, "base", CHECKIN_BASE_SIZE)) > 0)
    env.members.upsert(user, user, {"group_id": "standard"})
    proof(env.db, user)
    env.registry.save(
        "checkin",
        config={
            "double_ppm": PPM,
            "multiplier": 2,
            "streak_tiers": encode([{"days": 1, "bonus": 7}]),
            "drops": encode([{"ppm": PPM, "spec": DEFAULT_CARDS[0]}]),
        },
    )
    result = env.checkin.checkin(user)
    assert result["points"] == result["base"] * 2 + result["base"] * 7 // 100 and result["multiplier"] == 2
    assert result['streak_percent'] == 7
    assert len(env.bag.items(user)) == 1
    assert json.loads(env.db.one("SELECT result_json FROM checkins")["result_json"]) == result


def parallel(e, fn, n=8):
    # Distinct connections share the same on-disk database: tests catch missing
    # BEGIN IMMEDIATE even if a single-instance RLock test would pass.
    connections = [Database(e.db.path) for _ in range(n)]
    workers = [build(db, e.registry._store) for db in connections]
    try:
        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(fn, workers))
    finally:
        for db in connections:
            db.close()


def test_concurrent_checkin_exactly_once_across_connections(env):
    proof(env.db, "u")
    results = parallel(env, lambda e: e.checkin.checkin("u", now=NOW))
    assert sum(r["ok"] for r in results) == 1
    assert len(env.points.ledger("u")) == 1
    assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 1


def test_concurrent_purchase_and_use_snapshot_limit_and_atomic_failure(env):
    env.points.add("u", 2000, "admin.adjust")
    item = env.shop.create(dict(DEFAULT_CARDS[0], enabled=True, per_user_limit=1))
    results = parallel(env, lambda e: e.shop.redeem("u", item["id"], request_id="purchase-1"))
    assert all(r == results[0] for r in results)
    assert env.points.balance("u") == 1500 and len(env.bag.items("u")) == 1
    env.shop.update(item["id"], {"amount": 1, "cost": 800, "name": "changed"})
    card = env.bag.items("u")[0]
    assert card["spec"]["cost"] == 500 and card["spec"]["name"] == "邀请码卡"
    results = parallel(env, lambda e: e.bag.use("u", card["id"]))
    assert all(r == results[0] for r in results)
    assert env.members.get("u")["invite_quota"] == 1
    with pytest.raises(ShopError):
        env.shop.redeem("u", item["id"], request_id="purchase-2")
    assert env.points.balance("u") == 1500
    with pytest.raises(ValueError):
        env.bag.use("v", card["id"])


def test_independent_expiration_keeps_base_and_later_admin_changes(env):
    env.points.add("u", 2000, "admin.adjust")
    cards = []
    for amount, days in ((10, 1), (20, 2)):
        item = env.shop.create(
            dict(DEFAULT_CARDS[1], amount=amount, duration_days=days, enabled=True)
        )
        env.shop.redeem("u", item["id"])
        card = env.bag.items("u")[0]
        assert env.members.get("u")["bandwidth_limit_kbps"] == 20 * 1024 + (
            10 * 1000 if cards else 0
        )
        env.bag.use("u", card["id"])
        cards.append(card["id"])
    assert env.members.get("u")["bandwidth_limit_kbps"] == 20 * 1024 + 30 * 1000
    # A legacy permanent boost while cards are active must not bake them in.
    grant_reward(env.db, env.members, "u", "bandwidth", 5)
    assert env.members.get("u")["overrides"]["bandwidth_limit_kbps"] == 25 * 1024
    env.clock[0] = NOW + 86400
    assert env.members.get("u")["bandwidth_limit_kbps"] == 25 * 1024 + 20 * 1000
    env.members.set_overrides("u", {"bandwidth_limit_kbps": 40 * 1024})
    assert env.members.get("u")["bandwidth_limit_kbps"] == 40 * 1024 + 20 * 1000
    env.clock[0] = NOW + 2 * 86400
    assert env.members.get("u")["bandwidth_limit_kbps"] == 40 * 1024
    assert env.members.get("u")["overrides"]["bandwidth_limit_kbps"] == 40 * 1024


def test_streams_additive_cap_refusal_does_not_waste_card_and_failure_atomic(env):
    env.registry.save("inventory", config={"streams_cap": 3})
    env.points.add("u", 1800, "admin.adjust")
    item = env.shop.create(dict(DEFAULT_CARDS[2], enabled=True))
    for _ in range(2):
        env.shop.redeem("u", item["id"])
    a, b = env.bag.items("u")
    env.bag.use("u", a["id"])
    assert env.members.get("u")["max_streams"] == 3
    with pytest.raises(ValueError, match="上限"):
        env.bag.use("u", b["id"])
    assert next(r for r in env.bag.items("u") if r["id"] == b["id"])["used_at"] is None
    assert env.points.balance("u") == 600
    env.clock[0] += 31 * 86400
    assert env.members.get("u")["max_streams"] == 2
    env.bag.use("u", b["id"])
    assert env.members.get("u")["max_streams"] == 3


def test_purchase_fulfillment_failure_rolls_back_debit_order_and_receipt(env, monkeypatch):
    env.points.add("u", 1000, "admin.adjust")
    item = env.shop.create(dict(DEFAULT_CARDS[0], enabled=True))

    def fail(*a, **kw):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(InventoryService, "add", fail)
    with pytest.raises(RuntimeError):
        env.shop.redeem("u", item["id"], request_id="fail")
    assert env.points.balance("u") == 1000 and not env.shop.orders("u")
    assert not env.db.query("SELECT * FROM economy_receipts")


def test_concurrent_transfer_atomic_both_ledgers_idempotent_and_caps(env):
    env.points.add("u", 1000, "admin.adjust")
    env.registry.save("points_transfer", config={"fee_percent": 10})
    results = parallel(
        env, lambda e: e.transfer.transfer("u", "v", 500, request_id="transfer-1", expected_fee=50)
    )
    assert all(r == results[0] for r in results)
    assert env.points.balance("u") == 500 and env.points.balance("v") == 450
    assert len(env.points.ledger("v")) == 1 and len(env.points.ledger("u")) == 2
    with pytest.raises(ValueError, match="不同操作"):
        env.transfer.transfer("u", "v", 1, request_id="transfer-1")
    with pytest.raises(ValueError, match="每日"):
        env.transfer.transfer("u", "v", 1, request_id="transfer-2")
    with pytest.raises(ValueError, match="自己"):
        env.transfer.transfer("u", "u", 1)


def test_transfer_failure_rolls_back_sender_and_receiver_restriction_is_configurable(
    env, monkeypatch
):
    env.points.add("u", 100, "admin.adjust")
    env.members.upsert("v", "v", {"status": "suspended"})
    env.transfer.transfer("u", "v", 1)  # default receivers unrestricted
    env.registry.save("points_transfer", config={"restrict_receivers": True})
    with pytest.raises(ValueError):
        env.transfer.transfer("u", "v", 1)
    env.registry.save("points_transfer", config={"restrict_receivers": False})
    original = env.points._apply

    def fail(conn, user, *args):
        if user == "v":
            raise RuntimeError("receiver failure")
        return original(conn, user, *args)

    monkeypatch.setattr(env.points, "_apply", fail)
    with pytest.raises(RuntimeError):
        env.transfer.transfer("u", "v", 20, request_id="failed")
    assert env.points.balance("u") == 99 and env.points.balance("v") == 1
    env.members.upsert("u", "u", {"status": "suspended"})
    with pytest.raises(ValueError, match="失效"):
        env.transfer.transfer("u", "v", 1)


class FakeBot:
    def __init__(self):
        self.tag = ""
        self.permission = True
        self.fail = False
        self.lose_ack = False
        self.calls = []

    def _group_allowlist(self):
        return ["-1001"]

    async def _call(self, method, payload=None):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 900}
        if method == "getChatMember":
            if payload["user_id"] == 900:
                return {"status": "administrator", "can_manage_tags": self.permission}
            return {"status": "member", "tag": self.tag}
        if method == "setChatMemberTag":
            if self.fail:
                return None
            self.tag = payload["tag"]
            if self.lose_ack:
                return None
            return True
        raise AssertionError("unexpected Telegram method " + method)


def titles_env(e):
    e.db.execute("UPDATE members SET tg_user_id='123' WHERE emby_user_id='u'")
    bot = FakeBot()
    titles = TitleService(e.db, e.members, bot, lambda: e.registry.config("inventory"))
    return titles, bot


@pytest.mark.parametrize(
    "tag",
    [
        "😀",
        "A❤️",
        "1️⃣",
        "🇨🇳",
        "hello\u200dworld",
        "管理员",
        "AdMiN",
        "http example",
        "www",
        "abc.com",
        "@test",
        "x\nY",
        "a" * 17,
        "",
    ],
)
def test_title_validation_rejects_emoji_urls_impersonation_controls(tag):
    with pytest.raises(ValueError):
        validate_title(tag)


def test_title_card_creation_limited_and_permanent_switch_free_cancel(env):
    titles, bot = titles_env(env)
    env.points.add("u", 1000, "admin.adjust")
    item = env.shop.create(dict(DEFAULT_CARDS[3], duration_days=1, enabled=True))
    env.shop.redeem("u", item["id"])
    card = env.bag.items("u")[0]
    with pytest.raises(ValueError):
        env.bag.use("u", card["id"], title="Admin")
    assert env.bag.items("u")[0]["used_at"] is None
    result = env.bag.use("u", card["id"], title="观影达人")
    assert result["expires_at"] == NOW + 86400
    assert env.bag.use("u", card["id"], title="观影达人") == result
    second = titles.grant("u", "永久称号", 0)
    asyncio.run(titles.wear("u", result["title_id"]))
    assert bot.tag == "观影达人"
    asyncio.run(titles.wear("u", second["title_id"]))
    assert bot.tag == "永久称号"
    assert sum(r["worn"] for r in titles.titles("u")) == 1
    asyncio.run(titles.wear("u", None))
    assert bot.tag == "" and env.points.balance("u") == 800


def test_title_tg_failure_retry_and_lost_ack_recovery(env):
    titles, bot = titles_env(env)
    title = titles.grant("u", "影视迷", 1)
    bot.fail = True
    result = asyncio.run(titles.wear("u", title["title_id"]))
    assert not result["ok"] and result["states"][0]["status"] == "pending" and bot.tag == ""
    bot.fail = False
    bot.lose_ack = True
    asyncio.run(titles.sync("u"))
    assert bot.tag == "影视迷" and titles.states("u")[0]["status"] == "pending"
    bot.lose_ack = False
    asyncio.run(titles.sync("u"))
    assert titles.states("u")[0]["status"] == "synced"
    assert sum(m == "setChatMemberTag" for m, p in bot.calls) == 2
    env.clock[0] += 86400
    asyncio.run(titles.sync())
    assert bot.tag == "" and titles.states("u")[0]["status"] == "synced"


def test_title_expiry_and_revoke_do_not_overwrite_later_admin_hand_edit(env):
    titles, bot = titles_env(env)
    first = titles.grant("u", "观影迷", 1)
    asyncio.run(titles.wear("u", first["title_id"]))
    bot.tag = "管理手改"
    env.clock[0] += 86400
    asyncio.run(titles.sync())
    assert bot.tag == "管理手改" and titles.states("u")[0]["status"] == "protected"
    calls = len(bot.calls)
    asyncio.run(titles.sync("u"))
    assert len(bot.calls) == calls
    second = titles.grant("u", "永久拥有", 0)
    result = asyncio.run(titles.wear("u", second["title_id"]))
    assert not result["ok"] and bot.tag == "管理手改"
    asyncio.run(titles.revoke("u", second["title_id"]))
    assert bot.tag == "管理手改"


def test_missing_tg_permission_is_pending_never_promotes_admin(env):
    titles, bot = titles_env(env)
    bot.permission = False
    title = titles.grant("u", "普通称号")
    result = asyncio.run(titles.wear("u", title["title_id"]))
    assert not result["ok"] and "can_manage_tags" in result["states"][0]["error"]
    assert not any(m == "setChatMemberTag" for m, p in bot.calls)
    assert not any(
        m in ("promoteChatMember", "setChatAdministratorCustomTitle") for m, p in bot.calls
    )


def test_admin_backend_config_grant_revoke_and_authentication():
    with TestClient(app) as client:
        assert client.get("/api/economy/members/u").status_code == 401
        app.state.members.upsert("u", "viewer", {"group_id": "standard"})
        auth = ("admin", "change-me")
        response = client.post(
            "/api/economy/members/u/titles", auth=auth, json={"tag": "后台授予", "days": 0}
        )
        assert response.status_code == 200
        tid = response.json()["title_id"]
        response = client.get("/api/economy/members/u", auth=auth)
        assert response.json()["titles"][0]["tag"] == "后台授予"
        assert client.delete(f"/api/economy/members/u/titles/{tid}", auth=auth).status_code == 200
        assert (
            client.get("/api/economy/members/u", auth=auth).json()["titles"][0]["revoked_at"]
            is not None
        )
        assert len(client.get("/api/shop/items", auth=auth).json()) == 5
        assert (
            client.post(
                "/api/shop/items", auth=auth, json=dict(DEFAULT_CARDS[3], cost=199)
            ).status_code
            == 400
        )
