"""Account-center regressions: real local SQLite, mocked Telegram/Emby only."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from test_admin_commands import bot as base_bot  # noqa: F401

from app.main import app
from app.modules.bot_views import quota_lines
from app.modules.stats import StatsService
from app.modules.telegram import TelegramBot

GROUP = -100777
NEW = "903"


def run(coro):
    return asyncio.run(coro)


def msg(text, user=NEW):
    return {
        "chat": {"id": int(user), "type": "private"},
        "from": {"id": int(user), "username": "new_user"},
        "message_id": 80,
        "text": text,
    }


def cb(data, user=NEW, chat=None, mid=101, photo=False):
    chat = int(user) if chat is None else chat
    message = {
        "chat": {"id": chat, "type": "supergroup" if chat < 0 else "private"},
        "message_id": mid,
    }
    if photo:
        message["photo"] = [{"file_id": "logo-image"}]
    return {"id": "callback", "data": data, "from": {"id": int(user)}, "message": message}


@pytest.fixture
def bot(base_bot):
    b = base_bot
    config = {**b._cfg(), "group_interaction_chats": [str(GROUP)]}
    b._config = lambda: config
    b._stats = StatsService(b.db)
    b.calls = []
    b.next_mid = 100

    async def call(method, payload=None, timeout=20):
        b.calls.append((method, payload or {}))
        if method in ("sendMessage", "sendPhoto"):
            b.next_mid += 1
            return {"message_id": b.next_mid, "chat": {"id": int(payload["chat_id"])}}
        if method == "editMessageText":
            return {"message_id": payload["message_id"]}
        return True

    b._call = call
    b.send = TelegramBot.send.__get__(b, TelegramBot)
    b._edit = TelegramBot._edit.__get__(b, TelegramBot)

    async def authenticate(username, password):
        return (
            {"Id": "u1", "Name": "alice"}
            if username == "alice" and password == "private-test-only"
            else None
        )

    async def users():
        return [{"Id": "u1"}, {"Id": "admin1"}]

    b._emby.authenticate_user = authenticate
    b._emby.list_users = users
    return b


def request(bot):
    run(bot._handle_message(msg("/rebind")))
    run(bot._handle_message(msg("alice private-test-only")))
    return bot.db.one("SELECT * FROM tg_requests WHERE kind='rebind'")


def test_claim_commands_callbacks_and_helpers_cannot_create_requests(bot):
    run(bot._handle_message(msg("/claim")))
    run(bot._handle_callback(cb("claim")))
    assert not bot._create_request("bind", NEW, "new", "alice")
    assert not bot._create_request("rebind", NEW, "new", "alice")
    assert bot.db.query("SELECT * FROM tg_requests") == []
    assert "claim" not in str(bot.guest_menu())


def test_legacy_pending_closed_without_rebinding_on_upgrade(bot):
    bot.db.execute(
        "INSERT INTO tg_requests(kind,tg_user_id,wanted_username,created_at) VALUES('bind',?,'alice',0)",
        (NEW,),
    )
    before = bot.db.one("SELECT * FROM members WHERE emby_user_id='u1'")
    bot.db._migrate()
    assert bot.db.one("SELECT status FROM tg_requests")["status"] == "closed"
    assert bot.db.one("SELECT * FROM members WHERE emby_user_id='u1'") == before
    with pytest.raises(ValueError):
        bot.review_request(1, True)


def test_rebind_password_private_no_echo_and_group_notice_not_menu(bot):
    r = request(bot)
    assert r["verified_at"] and r["emby_user_id"] == "u1" and r["old_tg_user_id"] == "901"
    assert bot.members.get("u1")["tg_user_id"] == "901"
    assert ("deleteMessage", {"chat_id": 903, "message_id": 80}) in bot.calls
    assert "private-test-only" not in str(bot.calls)
    assert "private-test-only" not in str(bot.db.query("SELECT * FROM tg_requests"))
    notices = bot.db.query("SELECT * FROM tg_rebind_notices")
    assert len(notices) == 1 and notices[0]["chat_id"] == str(GROUP)
    assert not any(str(GROUP) in key for key in bot._menu_panel)


def test_wrong_password_and_five_attempt_limit(bot):
    run(bot._handle_message(msg("/rebind")))
    for _ in range(6):
        run(bot._handle_message(msg("alice invalid-private-input")))
    assert bot.db.query("SELECT * FROM tg_requests") == []
    assert any("频繁" in p.get("text", "") for _, p in bot.calls)
    assert "invalid-private-input" not in str(bot.calls)


def test_group_cannot_start_or_submit_password_flow(bot):
    run(bot._handle_callback(cb("rebind", chat=GROUP)))
    assert not bot._pending
    assert bot.db.query("SELECT * FROM tg_requests") == []


def test_group_admin_approval_once_preserves_entitlements(bot):
    before = bot.db.one("SELECT * FROM members WHERE emby_user_id='u1'")
    r = request(bot)
    notice = bot.db.one("SELECT * FROM tg_rebind_notices")
    data = f"tg_rebind_review:{r['id']}:yes"
    run(bot._handle_callback(cb(data, user="901", chat=GROUP, mid=notice["message_id"])))
    assert bot.members.get("u1")["tg_user_id"] == "901"
    run(bot._handle_callback(cb(data, user="900", chat=GROUP, mid=notice["message_id"])))
    after = bot.db.one("SELECT * FROM members WHERE emby_user_id='u1'")
    assert after["tg_user_id"] == NEW
    for key in before:
        if key not in ("tg_user_id", "tg_username", "tg_bound_at", "updated_at"):
            assert before[key] == after[key]
    calls = len([1 for method, _ in bot.calls if method == "sendMessage"])
    run(bot._handle_callback(cb(data, user="900", chat=GROUP, mid=notice["message_id"])))
    assert len([1 for method, _ in bot.calls if method == "sendMessage"]) == calls
    assert (
        bot.db.one("SELECT COUNT(*) n FROM audit_log WHERE action='telegram.rebind.approved'")["n"]
        == 1
    )


def test_binding_changed_or_new_tg_occupied_rejects_approval(bot):
    r = request(bot)
    bot.members.bind_telegram("u1", "904", "moved", actor="test")
    result = run(bot.review_rebind(r["id"], True, "admin"))
    assert not result["approved"] and result["status"] == "conflict"
    assert bot.members.get("u1")["tg_user_id"] == "904"


def test_approval_missing_remote_does_not_write(bot):
    r = request(bot)

    async def gone():
        return [{"Id": "admin1"}]

    bot._emby.list_users = gone
    with pytest.raises(ValueError):
        run(bot.review_rebind(r["id"], True, "admin"))
    assert bot.members.get("u1")["tg_user_id"] == "901"


def test_rejection_keeps_binding_and_updates_group_card(bot):
    r = request(bot)
    result = run(bot.review_rebind(r["id"], False, "admin"))
    assert result["status"] == "rejected"
    assert bot.members.get("u1")["tg_user_id"] == "901"
    assert any(
        m == "editMessageText" and p.get("reply_markup") == {"inline_keyboard": []}
        for m, p in bot.calls
    )


def test_expired_request_and_forged_card_do_not_move_binding(bot):
    r = request(bot)
    run(
        bot._handle_callback(cb(f"tg_rebind_review:{r['id']}:yes", user="900", chat=GROUP, mid=999))
    )
    assert bot.members.get("u1")["tg_user_id"] == "901"
    bot.db.execute("UPDATE tg_requests SET expires_at=1 WHERE id=?", (r["id"],))
    assert run(bot.review_rebind(r["id"], True, "admin"))["status"] == "expired"


def test_logo_home_replaced_cleanly_before_multistep_text(bot):
    bot._cfg()["menu_logo_url"] = "https://example.com/logo.png"
    run(bot._handle_message(msg("/start", user="901")))
    home = bot._panel["901"]
    assert bot.calls[-1][0] == "sendPhoto"
    run(bot._handle_callback(cb("usage", user="901", mid=home, photo=True)))
    panel = bot._panel["901"]
    assert panel != home
    assert ("deleteMessage", {"chat_id": 901, "message_id": home}) in bot.calls
    before = bot.next_mid
    run(bot._handle_callback(cb("usage", user="901", mid=panel)))
    assert bot.next_mid == before and bot._panel["901"] == panel


def test_logo_invalid_url_rejected_and_omitted_save_preserved():
    with TestClient(app) as c:
        auth = ("admin", "change-me")
        invalid = c.post(
            "/api/settings/telegram",
            auth=auth,
            json={
                "menu_logo_url": "javascript:alert(1)",
                "allow_admin_grant": False,
                "allow_invite": False,
                "allow_redeem": False,
            },
        )
        assert invalid.status_code == 422 and "Logo" in invalid.text
        # Closed channels allow storing artwork before enabling a bot.
        body = {
            "menu_logo_url": "https://example.com/logo.png",
            "allow_admin_grant": False,
            "allow_invite": False,
            "allow_redeem": False,
        }
        assert (
            c.post("/api/settings/telegram", auth=auth, json=body).json()["menu_logo_url"]
            == body["menu_logo_url"]
        )
        assert (
            c.post("/api/settings/telegram", auth=auth, json={"register_days": 30}).json()[
                "menu_logo_url"
            ]
            == body["menu_logo_url"]
        )
        assert (
            c.post("/api/settings/telegram", auth=auth, json={"menu_logo_url": ""}).json()[
                "menu_logo_url"
            ]
            == ""
        )


def test_unknown_measured_never_uses_old_estimate_or_zero_percent():
    m = {
        "quota_source": "measured",
        "traffic_used_bytes": 999999,
        "traffic_quota_bytes": 1024,
        "measured_used_bytes": None,
        "metering": {"measurement_status": "no_usage_records", "coverage": {"degraded": False}},
    }
    text = "\n".join(quota_lines(m))
    assert "已用：<b>暂未测得</b>" in text and "剩余：<b>暂无法确认</b>" in text
    assert "%" not in text and "999" not in text and "估算" not in text
    m["measured_used_bytes"] = 512
    text = "\n".join(quota_lines(m))
    assert "已用：<b>512 B</b>" in text and "剩余：<b>512 B</b>" in text


def test_personal_and_admin_watch_totals_survive_prune(bot):
    now = int(time.time())
    for seconds, start in ((100, now - 1000), (200, now - 40 * 86400), (300, now - 500 * 86400)):
        bot.db.execute(
            "INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES(?,?,?,?)",
            ("u1", seconds, start, start + seconds),
        )
    s = bot._stats.watch_summary("u1", now=now)
    assert s["recorded_seconds"] == 600 and s["seconds_30d"] == 100
    bot._stats.prune(400)
    assert bot._stats.watch_summary("u1")["recorded_seconds"] == 600
    assert "近30天：<b>1分</b>" in bot._user_card(bot.members.get("u1"))
    assert "累计已记录：10分" in bot._admin_details(bot.members.get("u1"))
    assert "近30天观看" in bot._usage_text(bot.members.get("u1"))
    bot._stats.bind_live_watch(lambda: [{"user_id": "u1", "seconds": 70, "started_at": now - 100}])
    assert bot._stats.watch_summary("u1")["recorded_seconds"] == 670
    bot.db._migrate()
    assert bot._stats.watch_summary("u1")["recorded_seconds"] == 670
