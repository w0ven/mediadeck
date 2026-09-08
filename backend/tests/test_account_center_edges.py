"""Data-integrity edges for account center; no external systems involved."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from test_bot_account_center import base_bot, bot, cb, msg, request  # noqa: F401
from test_metering import _env, _svc

from app.main import app
from app.modules.stats import legacy_watch_fingerprint


def test_known_retired_reporter_remains_degraded_until_resolved(tmp_path):
    now = time.time()
    svc = _svc(tmp_path, expected=["active"])
    svc.ingest(_env("active", "boot", 1, [], observed_at=now))
    svc.ingest(_env("retired", "old", 1, [], observed_at=now - 86400))
    snap = svc.snapshot("u", now=now)
    assert snap["coverage"]["degraded"]
    assert any(n["name"] == "retired" and not n["ok"] for n in snap["coverage"]["nodes"])


def test_healthy_expected_nodes_without_user_data_is_not_fake_zero(tmp_path):
    now = time.time()
    svc = _svc(tmp_path, expected=["de1", "nc1"])
    for n in ("de1", "nc1"):
        svc.ingest(_env(n, "boot", 1, [], observed_at=now))
    snap = svc.snapshot("u", now=now)
    assert not snap["coverage"]["degraded"]
    assert snap["measurement_status"] == "no_usage_records"
    assert snap["measured_used_bytes"] is None


def test_main_expected_nodes_excludes_disabled_never_reporter(monkeypatch):
    with TestClient(app) as client:
        from types import SimpleNamespace

        monkeypatch.setattr(
            app.state.settings_service,
            "nodes",
            lambda: [
                SimpleNamespace(name="ca1", enabled=False),
                SimpleNamespace(name="de1", enabled=True),
            ],
        )
        app.state.metering.ingest(_env("de1", "boot", 1, [], observed_at=time.time()))
        rows = client.get("/api/metering", auth=("admin", "change-me")).json()["totals"][
            "coverage"
        ]["nodes"]
        assert [r["name"] for r in rows] == ["de1"]


def seed_watch(bot):
    now = int(time.time())
    start = now - 10 * 86400
    bot.db.execute(
        "INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES(?,?,?,?)",
        ("u1", 300, start, start + 300),
    )
    return now, start


def test_legacy_history_disjoint_import_rollups_and_retry_after_prune(bot):
    now, boundary = seed_watch(bot)
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
    assert s["recorded_seconds"] == 2000 and s["seconds_30d"] == 1000
    assert bot._stats.top_watchers(hours=720)[0]["seconds"] == 1000
    bot._stats.prune(400)
    assert bot._stats.watch_summary("u1")["recorded_seconds"] == 2000
    assert bot._stats.import_legacy_watch(rows, source=src) == 0
    assert bot.db.one("SELECT COUNT(*) n FROM watch_legacy_events")["n"] == 1
    changed = [{**rows[0], "event_id": "another-source"}]
    with pytest.raises(ValueError):
        bot._stats.import_legacy_watch(changed, source=legacy_watch_fingerprint(changed))


@pytest.mark.parametrize("case", ["overlap", "unknown_user", "bad_seconds", "duplicate"])
def test_invalid_legacy_data_never_partially_imports(bot, case):
    now, boundary = seed_watch(bot)
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


def test_live_time_appears_once_then_moves_to_finished_totals(bot):
    now, boundary = seed_watch(bot)
    bot._stats.bind_live_watch(lambda: [{"user_id": "u1", "started_at": now - 100, "seconds": 80}])
    assert bot._stats.watch_summary("u1")["recorded_seconds"] == 380
    assert bot._stats.top_watchers(hours=720)[0]["seconds"] == 380
    bot.db.execute(
        "INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES(?,?,?,?)",
        ("u1", 80, now - 100, now),
    )
    bot._stats.bind_live_watch(list)
    assert bot._stats.watch_summary("u1")["recorded_seconds"] == 380


def test_revoked_reviewer_during_presence_check_cannot_approve(bot):
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


def test_verified_request_duplicates_do_not_fan_out_again(bot):
    request(bot)
    notices = len(bot.db.query("SELECT * FROM tg_rebind_notices"))
    asyncio.run(bot._handle_message(msg("/rebind")))
    asyncio.run(bot._handle_message(msg("alice private-test-only")))
    assert len(bot.db.query("SELECT * FROM tg_requests")) == 1
    assert len(bot.db.query("SELECT * FROM tg_rebind_notices")) == notices


def test_logo_failure_falls_back_without_deleting_working_menu(bot):
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
