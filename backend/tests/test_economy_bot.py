"""Actual Telegram update dispatch exercises user-facing economy entry points."""

import asyncio

import pytest

from test_economy import build, proof, FakeBot, SECRET
from test_tg_interaction_context import VIEWER, GROUP, command, click
from test_tg_interaction_context import env as interaction_env  # noqa: F401

from app.modules.titles import TitleService
from app.core.config import settings
from app.modules.economy_rules import DEFAULT_CARDS


@pytest.fixture
def env(request, monkeypatch):
    e = request.getfixturevalue("interaction_env")
    monkeypatch.setenv("MEDIADECK_CHECKIN_SECRET", SECRET)
    settings.cache_clear()
    services = build(e.db)
    services.registry.save("checkin", enabled=True)
    services.registry.save("points_transfer", enabled=True)
    e.bot.bind_plugins(services.registry)
    e.bot._inventory = services.bag
    tagbot = FakeBot()
    e.bot._titles = TitleService(
        e.db, e.members, e.bot, lambda: services.registry.config("inventory")
    )
    tagbot._group_allowlist = lambda: [str(GROUP)]
    original = e.tg.call

    async def call(method, payload=None, **kwargs):
        if method in ("getMe", "getChatMember", "setChatMemberTag"):
            return await tagbot._call(method, payload)
        return await original(method, payload, **kwargs)

    e.bot._call = call
    e.services = services
    e.tags = tagbot
    return e


def test_bot_purchase_use_title_create_wear_cancel_and_repeat_callbacks(env):
    env.services.points.add("u1", 2000, "admin.adjust")
    invite = env.services.shop.create(dict(DEFAULT_CARDS[0], enabled=True))
    title = env.services.shop.create(dict(DEFAULT_CARDS[3], enabled=True, duration_days=1))

    async def run():
        mid = await command(env, "/start", chat=VIEWER, user=VIEWER)
        await click(env, "bag", mid, chat=VIEWER, user=VIEWER)
        assert {"inventory", "titles", "shop"} <= {
            b["callback_data"] for b in env.tg.actions(VIEWER, mid)
        }
        await click(env, "shop", mid, chat=VIEWER, user=VIEWER)
        await click(env, f"buy:{invite['id']}", mid, chat=VIEWER, user=VIEWER)
        await asyncio.gather(
            *[click(env, f"buyok:{invite['id']}", mid, chat=VIEWER, user=VIEWER) for _ in range(3)]
        )
        assert len(env.services.bag.items("u1")) == 1
        assert env.members.get("u1")["invite_quota"] == 0  # purchase ONLY enters bag
        await click(env, "inventory", mid, chat=VIEWER, user=VIEWER)
        card = env.services.bag.items("u1")[0]
        await asyncio.gather(
            *[click(env, f"card:{card['id']}", mid, chat=VIEWER, user=VIEWER) for _ in range(3)]
        )
        assert env.members.get("u1")["invite_quota"] == 1
        await click(env, f"buy:{title['id']}", mid, chat=VIEWER, user=VIEWER)
        await click(env, f"buyok:{title['id']}", mid, chat=VIEWER, user=VIEWER)
        title_card = env.services.bag.items("u1")[0]
        await click(env, f"card:{title_card['id']}", mid, chat=VIEWER, user=VIEWER)
        await command(env, "观影达人", chat=VIEWER, user=VIEWER)
        rows = env.bot._titles.titles("u1")
        assert (
            len(rows) == 1
            and rows[0]["tag"] == "观影达人"
            and rows[0]["expires_at"] > rows[0]["created_at"]
        )
        mid = env.bot._panel[env.bot._session_key(VIEWER, VIEWER, group=False)]
        await click(env, "titles", mid, chat=VIEWER, user=VIEWER)
        await click(env, f"wear:{rows[0]['id']}", mid, chat=VIEWER, user=VIEWER)
        assert env.tags.tag == "观影达人" and "synced" in env.tg.text(VIEWER, mid)
        await click(env, "wear:none", mid, chat=VIEWER, user=VIEWER)
        assert env.tags.tag == ""

    asyncio.run(run())
    assert env.services.points.balance("u1") == 1350


def test_bot_transfer_confirmation_contains_zero_fee_received_and_executes_once(env):
    env.services.points.add("u1", 100, "admin.adjust")

    async def run():
        mid = await command(env, "/start", chat=VIEWER, user=VIEWER)
        await click(env, "transfer", mid, chat=VIEWER, user=VIEWER)
        await command(env, "ViewerB", chat=VIEWER, user=VIEWER)
        await command(env, "20", chat=VIEWER, user=VIEWER)
        mid = env.bot._panel[env.bot._session_key(VIEWER, VIEWER, group=False)]
        text = env.tg.text(VIEWER, mid)
        assert "ViewerB" in text and "手续费：0" in text and "对方到账 20" in text
        await asyncio.gather(
            *[click(env, "transfer_ok", mid, chat=VIEWER, user=VIEWER) for _ in range(3)]
        )

    asyncio.run(run())
    assert env.services.points.balance("u1") == 130 and env.services.points.balance("u2") == 20
    assert len(env.services.points.ledger("u2")) == 1


def test_bot_checkin_shows_progress_and_negative_delta_without_plus_minus(env, monkeypatch):
    async def run():
        mid = await command(env, "/start", chat=VIEWER, user=VIEWER)
        await click(env, "checkin", mid, chat=VIEWER, user=VIEWER)
        assert "/600" in env.tg.text(VIEWER, mid)
        import time

        proof(env.db, "u1", now=time.time())
        # Pin only the base draw to the negative endpoint: sampler eligibility
        # and actual transactional plugin path still execute normally.
        original = __import__("app.modules.plugins_points", fromlist=["draw"]).draw
        monkeypatch.setattr(
            "app.modules.plugins_points.draw",
            lambda secret, user, day, domain, size: (
                0 if domain == "base" else original(secret, user, day, domain, size)
            ),
        )
        await click(env, "checkin", mid, chat=VIEWER, user=VIEWER)
        text = env.tg.text(VIEWER, mid)
        assert "-10" in text and "+-10" not in text and "签到成功" in text

    asyncio.run(run())


def test_owned_card_and_title_pagers_keep_old_items_reachable_and_messages_bounded(env):
    with env.db.write() as conn:
        for index in range(25):
            env.services.bag.add(
                conn, "u1", dict(DEFAULT_CARDS[0], name="长商品" + ("字" * 77)), "test", 1
            )
    for index in range(21):
        env.bot._titles.grant("u1", f"称号{index}")

    async def run():
        mid = await command(env, "/start", chat=VIEWER, user=VIEWER)
        await click(env, "inventory", mid, chat=VIEWER, user=VIEWER)
        assert "1/3页" in env.tg.text(VIEWER, mid)
        assert len(env.tg.text(VIEWER, mid).encode("utf-16-le")) // 2 < 4096
        await click(env, "invpage:2", mid, chat=VIEWER, user=VIEWER)
        assert "3/3页" in env.tg.text(VIEWER, mid)
        assert "card:1" in {b["callback_data"] for b in env.tg.actions(VIEWER, mid)}
        await click(env, "titlespage:1", mid, chat=VIEWER, user=VIEWER)
        assert "2/2页" in env.tg.text(VIEWER, mid)
        assert "wear:1" in {b["callback_data"] for b in env.tg.actions(VIEWER, mid)}
        await click(env, "wear:1", mid, chat=VIEWER, user=VIEWER)
        assert env.tags.tag == "称号0"

    asyncio.run(run())
