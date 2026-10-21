"""Data-integrity edges for account center; no external systems involved."""

import asyncio
import time

import pytest
from test_bot_account_center import base_bot, bot, cb, msg, request  # noqa: F401

from app.modules.stats import legacy_watch_fingerprint


def seed_watch(bot):  # noqa: F811 - pytest fixture injection
    now = int(time.time())
    start = now - 10 * 86400
    bot.db.execute(
        "INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES(?,?,?,?)",
        ("u1", 300, start, start + 300),
    )
    return now, start


def test_legacy_history_disjoint_import_rollups_and_retry_after_prune(bot):  # noqa: F811 - pytest fixture injection
    now, _boundary = seed_watch(bot)
    rows = [
        {
            "event_id": "old1",
            "emby_user_id": "u1",
            "seconds": 700,
            "started_at": now - 20 * 86400,
            "ended_at": now - 20 * 86400 + 900,
        },
        {
            "event_id": "old2",
            "emby_user_id": "u1",
            "seconds": 1000,
            "started_at": now - 500 * 86400,
            "ended_at": now - 500 * 86400 + 1100,
        },
    ]
    src = legacy_watch_fingerprint(rows)
    assert bot._stats.import_legacy_watch(rows, source=src) == 2
    s = bot._stats.watch_summary("u1", now=now)
    assert s['recorded_seconds'] == 2000
    assert s['seconds_30d'] == 1000
    assert s['historical_unverified_seconds'] == 2000
    assert bot._stats.top_watchers(hours=720) == []
    bot._stats.prune(400)
    assert bot._stats.watch_summary('u1')['historical_unverified_seconds'] == 2000
    assert bot._stats.import_legacy_watch(rows, source=src) == 0
    assert bot.db.one("SELECT COUNT(*) n FROM watch_legacy_events")["n"] == 1
    changed = [{**rows[0], "event_id": "another-source"}]
    with pytest.raises(ValueError):
        bot._stats.import_legacy_watch(changed, source=legacy_watch_fingerprint(changed))


@pytest.mark.parametrize("case", ["overlap", "unknown_user", "bad_seconds", "duplicate"])
def test_invalid_legacy_data_never_partially_imports(bot, case):  # noqa: F811 - pytest fixture injection
    _now, boundary = seed_watch(bot)
    good = {
        "event_id": "old1",
        "emby_user_id": "u1",
        "seconds": 100,
        "started_at": boundary - 1000,
        "ended_at": boundary - 800,
    }
    bad = {**good, "event_id": "bad"}
    if case == "overlap":
        bad.update(started_at=boundary - 10, ended_at=boundary + 200)
    if case == "unknown_user":
        bad["emby_user_id"] = "deleted-user"
    if case == "bad_seconds":
        bad["seconds"] = 99999
    if case == "duplicate":
        bad = good.copy()
    rows = [good, bad]
    with pytest.raises(ValueError):
        bot._stats.import_legacy_watch(rows, source=legacy_watch_fingerprint(rows))
    assert bot.db.query("SELECT * FROM watch_legacy_events") == []
    assert bot.db.query("SELECT * FROM watch_legacy_baselines") == []


def test_live_time_appears_once_then_moves_to_finished_totals(bot):  # noqa: F811 - pytest fixture injection
    now, _boundary = seed_watch(bot)
    bot._stats.bind_live_watch(lambda: [{"user_id": "u1", "started_at": now - 100, "seconds": 80}])
    assert bot._stats.watch_summary('u1')['recorded_seconds'] == 300  # live estimates are not recorded
    assert bot._stats.top_watchers(hours=720) == []
    bot.db.execute(
        "INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES(?,?,?,?)",
        ("u1", 80, now - 100, now),
    )
    bot._stats.bind_live_watch(list)
    assert bot._stats.watch_summary('u1')['recorded_seconds'] == 380


def test_revoked_reviewer_during_presence_check_cannot_approve(bot):  # noqa: F811 - pytest fixture injection
    r = request(bot)
    notice = bot.db.one("SELECT * FROM tg_rebind_notices")

    async def users():
        bot.members.set_roles("admin1", [], actor="test")
        return [{"Id": "u1"}]

    bot._emby.list_users = users
    asyncio.run(
        bot._handle_callback(
            cb(
                f"tg_rebind_review:{r['id']}:yes",
                user="900",
                chat=-100777,
                mid=notice["message_id"],
            )
        )
    )
    assert bot.members.get("u1")["tg_user_id"] == "901"


def test_verified_request_duplicates_do_not_fan_out_again(bot):  # noqa: F811 - pytest fixture injection
    request(bot)
    notices = len(bot.db.query("SELECT * FROM tg_rebind_notices"))
    asyncio.run(bot._handle_message(msg("/rebind")))
    asyncio.run(bot._handle_message(msg("alice private-test-only")))
    assert len(bot.db.query("SELECT * FROM tg_requests")) == 1
    assert len(bot.db.query("SELECT * FROM tg_rebind_notices")) == notices


def test_logo_failure_falls_back_without_deleting_working_menu(bot):  # noqa: F811 - pytest fixture injection
    bot._cfg()["menu_logo_url"] = "https://example.com/logo.png"
    original = bot._call

    async def call(method, payload=None, timeout=20):
        if method == "sendPhoto":
            return None
        return await original(method, payload, timeout)

    bot._call = call
    asyncio.run(bot._handle_message(msg("/start", user="901")))
    assert bot._panel["901"] > 0
    assert any(m == "sendMessage" for m, _ in bot.calls)
    assert not bot._photo_panels
