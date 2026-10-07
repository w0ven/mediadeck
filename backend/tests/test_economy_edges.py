"""Boundary/uncertainty and live-policy integration checks for the economy."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_economy import NOW, parallel, proof, titles_env
from test_economy import env as economy_env  # noqa: F401

from app.core.config import settings
from app.main import _inventory_expired_effects, app
from app.modules.economy_rules import DEFAULT_CARDS, PPM, day_bounds
from app.modules.shop import ShopError


@pytest.fixture
def env(request):
    return request.getfixturevalue("economy_env")


@pytest.mark.parametrize(
    "roll,kind",
    [
        (0, "invite_card"),
        (4999, "invite_card"),
        (5000, "bandwidth_card"),
        (7999, "bandwidth_card"),
        (8000, "streams_card"),
        (9999, "streams_card"),
        (10000, None),
        (PPM - 1, None),
    ],
)
def test_default_drop_cumulative_boundaries_exactly_one_item(env, monkeypatch, roll, kind):
    proof(env.db, "u")
    monkeypatch.setattr(
        "app.modules.plugins_points.draw",
        lambda secret, user, day, domain, size: (
            11 if domain == "base" else roll if domain == "drop" else PPM - 1
        ),
    )
    result = env.checkin.checkin("u")
    assert result["multiplier"] == 1
    assert (result["drop_spec"]["kind"] if result["drop_spec"] else None) == kind
    assert len(env.bag.items("u")) == (1 if kind else 0)


def test_missing_persistent_secret_refuses_atomically_without_implicit_random_key(env, monkeypatch):
    proof(env.db, "u")
    monkeypatch.setenv("MEDIADECK_CHECKIN_SECRET", "")
    settings.cache_clear()
    with pytest.raises(ValueError, match="密钥未配置"):
        env.checkin.checkin("u")
    assert not env.points.ledger("u") and not env.db.query("SELECT * FROM checkins")
    assert not env.bag.items("u")


def test_concurrent_different_purchases_cannot_overspend(env):
    env.points.add("u", 600, "admin.adjust")
    item = env.shop.create(dict(DEFAULT_CARDS[0], enabled=True))

    def buy(e):
        try:
            return e.shop.redeem("u", item["id"])
        except ValueError:
            return {"ok": False}

    results = parallel(env, buy)
    assert sum(r["ok"] for r in results) == 1
    assert (
        env.points.balance("u") == 100 and len(env.bag.items("u")) == len(env.shop.orders("u")) == 1
    )


def test_concurrent_different_transfers_cannot_bypass_daily_cap(env):
    env.points.add("u", 1000, "admin.adjust")

    def send(e):
        try:
            return e.transfer.transfer("u", "v", 100)
        except ValueError:
            return {"ok": False}

    results = parallel(env, send)
    assert sum(r["ok"] for r in results) == 5
    assert env.points.balance("u") == env.points.balance("v") == 500


def test_purchase_rechecks_confirmed_spec_inside_transaction(env):
    env.points.add("u", 1000, "admin.adjust")
    item = env.shop.create(dict(DEFAULT_CARDS[0], enabled=True))
    env.shop.update(item["id"], {"cost": 600})
    with pytest.raises(ShopError, match="重新确认"):
        env.shop.redeem("u", item["id"], expected_spec=item, request_id="old-confirmation")
    assert env.points.balance("u") == 1000 and not env.bag.items("u")


def test_beijing_daily_transfer_reset_and_old_idempotency_receipt_does_not_resend(env):
    env.points.add("u", 2000, "admin.adjust")
    _, start, _ = day_bounds(NOW)
    env.clock[0] = start + 86400 - 0.001
    first = env.transfer.transfer("u", "v", 500, request_id="yesterday")
    env.clock[0] = start + 86400
    env.transfer.transfer("u", "v", 500, request_id="today")
    assert env.transfer.transfer("u", "v", 500, request_id="yesterday") == first
    with pytest.raises(ValueError, match="每日"):
        env.transfer.transfer("u", "v", 1)
    assert env.points.balance("v") == 1000


def test_title_card_concurrent_use_creates_only_one_title(env):
    env.points.add("u", 1000, "admin.adjust")
    item = env.shop.create(dict(DEFAULT_CARDS[3], enabled=True, duration_days=1))
    env.shop.redeem("u", item["id"])
    card = env.bag.items("u")[0]
    results = parallel(env, lambda e: e.bag.use("u", card["id"], title="多路观影"))
    assert all(r == results[0] for r in results)
    assert env.db.one("SELECT COUNT(*) n FROM member_titles")["n"] == 1


def test_lost_ack_then_expiry_clears_attempted_system_tag_after_restart(env):
    titles, bot = titles_env(env)
    title = titles.grant("u", "短期称号", 1)
    bot.lose_ack = True
    result = asyncio.run(titles.wear("u", title["title_id"]))
    assert not result["ok"] and bot.tag == "短期称号"
    env.clock[0] += 86400
    bot.lose_ack = False
    from app.modules.titles import TitleService

    titles = TitleService(env.db, env.members, bot, lambda: env.registry.config("inventory"))
    asyncio.run(titles.sync())
    assert bot.tag == "" and titles.states("u")[0]["status"] == "synced"


def test_expiration_sync_retries_without_overwriting_base_or_consuming_another_card(env):
    env.points.add("u", 1000, "admin.adjust")
    item = env.shop.create(dict(DEFAULT_CARDS[1], enabled=True, duration_days=1))
    env.shop.redeem("u", item["id"])
    card = env.bag.items("u")[0]
    env.bag.use("u", card["id"])
    env.clock[0] += 86400
    callback = AsyncMock(return_value={"ok": False, "remote_ok": False})
    asyncio.run(env.bag.reconcile_expired(callback))
    assert callback.call_args.args == ("u", {"bandwidth_card"})
    row = env.bag.items("u")[0]
    assert row["expiry_synced_at"] is None and "待重试" in row["expiry_error"]
    assert env.members.get("u")["bandwidth_limit_kbps"] == 20 * 1024
    callback.return_value = {"ok": True, "remote_ok": True}
    env.clock[0] += 60
    asyncio.run(env.bag.reconcile_expired(callback))
    assert env.bag.items("u")[0]["expiry_synced_at"] == int(env.clock[0])
    asyncio.run(env.bag.reconcile_expired(callback))
    assert callback.call_count == 2
    assert env.members.get("u")["overrides"]["bandwidth_limit_kbps"] == 20 * 1024


def test_bandwidth_expiry_actual_path_invalidates_old_signed_rates_and_terminates_old_urls(
    monkeypatch,
):
    with TestClient(app):
        app.state.cache.set("rate:u", 30 * 1024)
        remote = AsyncMock(return_value=1)
        monkeypatch.setattr(app.state.enforcement, "terminate_users", remote)
        result = asyncio.run(_inventory_expired_effects("u", {"bandwidth_card"}))
        assert result["ok"] is True
        assert app.state.cache.get("rate:u") is None
        assert remote.call_args.args[0] == {"u"} and remote.call_args.kwargs["strict"] is True


def test_additive_migration_preserves_legacy_ledger_and_streak_history(env):
    env.points.add("u", 100, "admin.adjust")
    yesterday = day_bounds(NOW - 86400)[0]
    env.db.execute(
        "INSERT INTO checkins(emby_user_id,day,streak,points,created_at) VALUES(?,?,?,?,?)",
        ("u", yesterday, 29, 42, int(NOW - 86400)),
    )
    ledger_before = env.points.ledger("u")
    row_before = env.db.one("SELECT * FROM checkins")
    env.db._migrate()
    assert env.points.ledger("u") == ledger_before
    assert env.db.one("SELECT * FROM checkins") == row_before
    proof(env.db, "u")
    result = env.checkin.checkin("u")
    assert result["streak"] == 30 and result['streak_percent'] == 50
    assert result["bonus"] == max(result['base'], 0) * 50 // 100
    assert env.points.balance("u") == 100 + result["points"]
    assert env.db.one("SELECT * FROM checkins WHERE day=?", (yesterday,)) == row_before
