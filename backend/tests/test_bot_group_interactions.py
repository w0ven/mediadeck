"""Group chat interactions: allowlist, session isolation, button re-check."""
from __future__ import annotations

import asyncio
import time

import pytest
from test_admin_commands import bot as base_bot  # noqa: F401

from app.core.db import Database
from app.modules.stats import StatsService
from app.modules.telegram import TelegramBot, GROUP_BRIEF_TTL

GROUP = -1003939238239
OTHER = -1001111111111
ADMIN = "900"
ALICE = "901"
OTHER_ADMIN = "902"


@pytest.fixture(name="bot")
def group_bot(request):
    b = request.getfixturevalue("base_bot")
    cfg = dict(b._cfg())
    cfg["group_interaction_chats"] = [str(GROUP)]
    b._config = lambda: cfg
    b._registration._config = b._cfg
    b._bot_username = "cola_embybot"
    b.calls = []
    b.next_mid = 100
    b.fail_send = False

    b.members.upsert("admin2", "otheradmin", {"group_id": "standard"}, actor="test")
    b.members.set_roles("admin2", ["admin"], actor="test")
    b.members.bind_telegram("admin2", OTHER_ADMIN, "other_admin", actor="test")

    async def call(method, payload=None, timeout=20):
        payload = payload or {}
        b.calls.append((method, payload))
        if method == "sendMessage":
            if b.fail_send:
                return None
            b.next_mid += 1
            return {"message_id": b.next_mid}
        if method == "editMessageText":
            return {"message_id": payload["message_id"]}
        if method in ("deleteMessage", "setMyCommands", "deleteMyCommands",
                      "answerCallbackQuery", "editMessageReplyMarkup"):
            return True
        return True

    b._call = call
    b.send = TelegramBot.send.__get__(b, TelegramBot)
    b._edit = TelegramBot._edit.__get__(b, TelegramBot)
    return b


def run(coro):
    return asyncio.run(coro)


def _msg(text, *, chat=GROUP, user=ADMIN, username="rootadmin",
         chat_type="supergroup", thread=None, reply=None, sender_chat=None,
         first="T"):
    message = {
        "chat": {"id": chat, "type": chat_type},
        "from": {"id": int(user) if str(user).lstrip("-").isdigit() else user,
                 "username": username, "first_name": first},
        "text": text,
    }
    if thread is not None:
        message["message_thread_id"] = thread
        message["is_topic_message"] = True
    if reply is not None:
        message["reply_to_message"] = reply
    if sender_chat is not None:
        message["sender_chat"] = sender_chat
    return message


def _cb(data, *, chat=GROUP, user=ADMIN, mid=101, chat_type="supergroup",
        thread=None):
    message = {
        "chat": {"id": chat, "type": chat_type},
        "message_id": mid,
    }
    if thread is not None:
        message["message_thread_id"] = thread
        message["is_topic_message"] = True
    return {
        "id": "cb1", "data": data,
        "message": message,
        "from": {"id": int(user) if str(user).lstrip("-").isdigit() else user,
                 "username": "rootadmin", "first_name": "T"},
    }


def texts(bot):
    return [p.get("text", "") for m, p in bot.calls if m in ("sendMessage", "editMessageText")]


def last_text(bot):
    rows = texts(bot)
    return rows[-1] if rows else ""


def test_empty_allowlist_ignores_even_the_production_group(bot):
    cfg = dict(bot._cfg())
    cfg["group_interaction_chats"] = []
    bot._config = lambda: cfg
    run(bot._handle_message(_msg("/me", user=ALICE, username="alice_tg")))
    assert bot.calls == []


def test_unauthorized_group_is_silent(bot):
    run(bot._handle_message(_msg("/me", chat=OTHER, user=ALICE, username="alice_tg")))
    run(bot._handle_message(_msg("/kk 901", chat=OTHER)))
    assert bot.calls == []


def test_ordinary_group_chat_is_ignored(bot):
    run(bot._handle_message(_msg("大家好", user=ALICE, username="alice_tg")))
    run(bot._handle_message(_msg("GOODCODE12", user=ALICE, username="alice_tg")))
    assert bot.calls == []


def test_other_bot_commands_are_ignored(bot):
    run(bot._handle_message(_msg("/kk@oldembybot 901")))
    run(bot._handle_message(_msg("/me@someoneelse", user=ALICE, username="alice_tg")))
    assert bot.calls == []


def test_anonymous_and_channel_senders_are_ignored(bot):
    run(bot._handle_message(_msg("/kk 901", sender_chat={"id": GROUP, "type": "supergroup"})))
    run(bot._handle_message(_msg("/me", sender_chat={"id": -100, "type": "channel"})))
    assert bot.calls == []


def test_allowed_group_start_is_private_entry_only(bot):
    run(bot._handle_message(_msg("/start GIFTABC1234567", user=ALICE, username="alice_tg")))
    assert "请私聊" in last_text(bot)
    assert "t.me/cola_embybot" in last_text(bot)
    assert bot._pending == {}


def test_group_myinfo_is_own_brief_card(bot):
    run(bot._handle_message(_msg("/myinfo", user=ALICE, username="alice_tg")))
    body = last_text(bot)
    assert "alice" in body
    assert "状态" in body and "有效期" in body
    assert "密码" not in body and "token" not in body
    assert "选择要查看的内容" not in body
    assert bot._panel[f"g:{GROUP}:0:{ALICE}"]


def test_group_me_does_not_show_other_people(bot):
    run(bot._handle_message(_msg("/me", user=ALICE, username="alice_tg")))
    assert "root" not in last_text(bot)
    assert "alice" in last_text(bot)


def test_group_rank_is_rolling_watch_time_not_calendar_today(bot):
    now = int(time.time())
    bot.db.execute(
        "INSERT INTO play_events (emby_user_id,username,item_id,item_name,item_type,"
        "series_name,device_id,client,play_method,node,remote_ip,bytes,seconds,"
        "started_at,ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u1", "alice", "i1", "Show", "Episode", "Show", "d", "app", "Direct",
         "n", "1.1.1.1", 999999, 7200, now - 3600, now - 3500))
    bot.db.execute(
        "INSERT INTO play_events (emby_user_id,username,item_id,item_name,item_type,"
        "series_name,device_id,client,play_method,node,remote_ip,bytes,seconds,"
        "started_at,ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("admin1", "root", "i2", "Old", "Movie", "", "d", "app", "Direct",
         "n", "1.1.1.1", 1, 36000, now - 48 * 3600, now - 47 * 3600))
    bot._stats = StatsService(bot.db)
    run(bot._handle_message(_msg("/rank", user=ALICE, username="alice_tg")))
    body = last_text(bot)
    assert "近 24 小时" in body
    assert "alice" in body and "2.0 小时" in body
    assert "root" not in body
    assert "今日" not in body
    mid = bot._panel[f"g:{GROUP}:0:{ALICE}"]
    run(bot._handle_callback(_cb("rank:720", user=ALICE, mid=mid)))
    assert "近 30 天" in last_text(bot)
    assert "root" in last_text(bot)


def test_private_rank_and_myinfo_aliases_still_work(bot):
    run(bot._handle_command("900", ADMIN, "rootadmin", "/rank"))
    assert "观看时长榜" in last_text(bot)
    bot.calls.clear()
    run(bot._handle_command("901", ALICE, "alice_tg", "/myinfo"))
    assert "alice" in last_text(bot)
    assert "选择要查看的内容" in last_text(bot)


def test_reply_kk_opens_admin_card_in_the_group(bot):
    reply = {"from": {"id": int(ALICE), "username": "alice_tg", "is_bot": False}}
    run(bot._handle_message(_msg("/kk", reply=reply)))
    body = last_text(bot)
    assert "alice" in body
    assert "状态" in body
    assert "密码" not in body
    key = f"g:{GROUP}:0:{ADMIN}"
    assert bot._pending[key][0] == "admin_user"
    assert all(p.get("chat_id") == GROUP for _, p in bot.calls)


def test_kk_numeric_id_gifts_bound_link_claimed_only_in_private(bot):
    run(bot._handle_message(_msg("/kk 777")))
    mid = bot._panel[f"g:{GROUP}:0:{ADMIN}"]
    run(bot._handle_callback(_cb("admin_gift", mid=mid)))
    nonce = bot._pending[f"g:{GROUP}:0:{ADMIN}"][2]["nonce"]
    run(bot._handle_callback(_cb(f"admin_gift_ok:{nonce}", mid=mid)))
    grant = bot._registration.get_grant("777")
    assert grant and grant["gift_code"]
    assert "t.me/cola_embybot?start=" + grant["gift_code"] in last_text(bot)
    assert bot.members.find_by_telegram("777") is None
    bot.calls.clear()
    run(bot._handle_message(_msg("/start " + grant["gift_code"], chat=GROUP, user="777",
                                 username="newbie")))
    assert "请私聊" in last_text(bot)
    assert bot._pending.get("777", (None,))[0] != "username"
    bot.calls.clear()
    run(bot._handle_message(_msg("/start " + grant["gift_code"], chat="777", user="777",
                                 username="newbie", chat_type="private")))
    assert bot._pending["777"][0] == "username"


def test_two_admins_and_topics_do_not_cross_sessions(bot):
    run(bot._handle_message(_msg("/kk 901", thread=11)))
    first = bot._panel[f"g:{GROUP}:11:{ADMIN}"]
    run(bot._handle_message(_msg("/kk 901", user=OTHER_ADMIN, username="other_admin", thread=12)))
    second = bot._panel[f"g:{GROUP}:12:{OTHER_ADMIN}"]
    assert first != second
    assert bot._pending[f"g:{GROUP}:11:{ADMIN}"][2]["user_id"] == "u1"
    assert bot._pending[f"g:{GROUP}:12:{OTHER_ADMIN}"][2]["user_id"] == "u1"
    bot.calls.clear()
    before = bot.members.get("u1")["expires_at"]
    run(bot._handle_callback(_cb("admin_renew:30", user=OTHER_ADMIN, mid=first, thread=11)))
    assert [m for m, _ in bot.calls if m != "answerCallbackQuery"] == []
    assert bot.members.get("u1")["expires_at"] == before
    run(bot._handle_callback(_cb("admin_renew:30", mid=first, thread=11)))
    after = bot.members.get("u1")["expires_at"]
    assert after >= (before or 0) + 29 * 86400


def test_bystander_button_cannot_edit_or_run_admin(bot):
    run(bot._handle_message(_msg("/kk 901")))
    mid = bot._panel[f"g:{GROUP}:0:{ADMIN}"]
    bot.calls.clear()
    run(bot._handle_callback(_cb("admin_rm", user=ALICE, mid=mid)))
    assert [m for m, _ in bot.calls if m != "answerCallbackQuery"] == []
    assert bot.emby.deleted == []
    run(bot._handle_callback(_cb("admin_rm", mid=mid)))
    assert "确认删除" in last_text(bot)
    nonce = bot._pending[f"g:{GROUP}:0:{ADMIN}"][2]["nonce"]
    bot.calls.clear()
    run(bot._handle_callback(_cb(f"rm_self:{nonce}", user=ALICE, mid=mid)))
    assert bot.emby.deleted == []
    run(bot._handle_callback(_cb("admin_cancel", mid=mid)))
    assert "已取消" in last_text(bot)
    assert bot.members.get("u1") is not None
    run(bot._handle_callback(_cb(f"rm_self:{nonce}", mid=mid)))
    assert bot.emby.deleted == []


def test_rmemby_cancel_and_replay_do_not_delete(bot):
    run(bot._handle_message(_msg("/rmemby alice")))
    mid = bot._panel[f"g:{GROUP}:0:{ADMIN}"]
    nonce = bot._pending[f"g:{GROUP}:0:{ADMIN}"][2]["nonce"]
    run(bot._handle_callback(_cb("admin_cancel", mid=mid)))
    assert bot.members.get("u1") is not None
    run(bot._handle_callback(_cb(f"rm_self:{nonce}", mid=mid)))
    assert bot.emby.deleted == []
    run(bot._handle_message(_msg("/rmemby alice")))
    mid = bot._panel[f"g:{GROUP}:0:{ADMIN}"]
    nonce = bot._pending[f"g:{GROUP}:0:{ADMIN}"][2]["nonce"]
    run(bot._handle_callback(_cb(f"rm_self:{nonce}", mid=mid)))
    assert bot.emby.deleted == ["u1"]
    bot.calls.clear()
    run(bot._handle_callback(_cb(f"rm_self:{nonce}", mid=mid)))
    assert bot.emby.deleted == ["u1"]


def test_group_renew_and_score_accept_reply_or_args(bot):
    before = bot.members.get("u1")["expires_at"] or int(time.time())
    reply = {"from": {"id": int(ALICE), "username": "alice_tg", "is_bot": False}}
    run(bot._handle_message(_msg("/renew 30", reply=reply)))
    assert bot.members.get("u1")["expires_at"] >= before + 29 * 86400
    run(bot._handle_message(_msg("/score alice +8")))
    assert bot.points.balance("u1") == 8
    run(bot._handle_message(_msg("/prouser alice")))
    key = f'g:{GROUP}:0:{ADMIN}'
    saved = bot._pending[key][2]['group_confirm']
    run(bot._handle_callback(_cb('admin_group_apply:keep:'+saved['nonce'], mid=bot._panel[key])))
    assert bot.members.get("u1")["group_id"] == "whitelist"
    run(bot._handle_message(_msg("/revuser alice")))
    saved = bot._pending[key][2]['group_confirm']
    run(bot._handle_callback(_cb('admin_group_apply:apply_group:'+saved['nonce'], mid=bot._panel[key])))
    assert bot.members.get("u1")["group_id"] == "standard"


def test_group_idle_replies_are_not_swallowed(bot):
    run(bot._handle_message(_msg("/kk 901")))
    mid = bot._panel[f"g:{GROUP}:0:{ADMIN}"]
    run(bot._handle_callback(_cb("admin_score", mid=mid)))
    bot.calls.clear()
    run(bot._handle_message(_msg("闲聊一句", user=ALICE, username="alice_tg")))
    assert bot.calls == []
    run(bot._handle_message(_msg("+12")))
    assert bot.calls == []
    assert bot.points.balance("u1") == 0
    run(bot._handle_message(_msg("+12", reply={"message_id": mid, "from": {"id": 1}})))
    assert bot.points.balance("u1") == 12


def test_new_group_command_clears_only_same_actor_menu(bot):
    run(bot._handle_message(_msg("/me", user=ALICE, username="alice_tg")))
    alice_mid = bot._panel[f"g:{GROUP}:0:{ALICE}"]
    run(bot._handle_message(_msg("/kk 901")))
    admin_mid = bot._panel[f"g:{GROUP}:0:{ADMIN}"]
    bot.calls.clear()
    run(bot._handle_message(_msg("/rank")))
    deleted = [p["message_id"] for m, p in bot.calls if m == "deleteMessage"]
    assert admin_mid in deleted
    assert alice_mid not in deleted


def test_group_command_scopes_are_chat_and_chat_member_not_telegram_admins(bot):
    run(bot._sync_chat_commands(GROUP, ADMIN, "", group=True))
    scopes = [p["scope"]["type"] for m, p in bot.calls if m == "setMyCommands"]
    assert "chat" in scopes and "chat_member" in scopes
    assert "all_chat_administrators" not in scopes
    member_cmds = next(p["commands"] for m, p in bot.calls
                       if m == "setMyCommands" and p["scope"]["type"] == "chat")
    admin_cmds = next(p["commands"] for m, p in bot.calls
                      if m == "setMyCommands" and p["scope"]["type"] == "chat_member")
    assert {c["command"] for c in member_cmds} == {"start", "me", "rank", "rules", "help"}
    assert {"kk", "renew", "rmemby", "score"} <= {c["command"] for c in admin_cmds}
    bot.calls.clear()
    bot.invalidate_commands()
    run(bot._install_commands())
    methods = [m for m, _ in bot.calls]
    assert "deleteMyCommands" in methods
    assert any(p.get("scope", {}).get("type") == "all_chat_administrators"
               for m, p in bot.calls if m == "deleteMyCommands")
    assert any(p.get("scope", {}).get("chat_id") == GROUP
               for m, p in bot.calls if m == "setMyCommands")


def test_watch_ranking_uses_play_events_window(tmp_path):
    db = Database(tmp_path / "stats.db")
    now = int(time.time())
    db.execute("INSERT INTO members (emby_user_id,username,status,created_at,updated_at) "
               "VALUES ('u1','alice','active',0,0)")
    db.execute("INSERT INTO members (emby_user_id,username,status,created_at,updated_at) "
               "VALUES ('u2','bob','active',0,0)")
    db.execute(
        "INSERT INTO play_events (emby_user_id,username,item_id,item_name,item_type,"
        "series_name,device_id,client,play_method,node,remote_ip,bytes,seconds,"
        "started_at,ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u1", "alice", "i", "A", "Movie", "", "", "", "", "", "", 9_000_000, 100,
         now - 100, now))
    db.execute(
        "INSERT INTO play_events (emby_user_id,username,item_id,item_name,item_type,"
        "series_name,device_id,client,play_method,node,remote_ip,bytes,seconds,"
        "started_at,ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u2", "bob", "i", "B", "Movie", "", "", "", "", "", "", 10, 500,
         now - 100, now))
    db.execute(
        "INSERT INTO play_events (emby_user_id,username,item_id,item_name,item_type,"
        "series_name,device_id,client,play_method,node,remote_ip,bytes,seconds,"
        "started_at,ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u2", "bob", "i", "Old", "Movie", "", "", "", "", "", "", 10, 9999,
         now - 40 * 3600, now - 39 * 3600))
    rows = StatsService(db).top_watchers(hours=24, limit=10)
    assert [r["username"] for r in rows] == ["bob", "alice"]
    assert rows[0]["seconds"] == 500


def test_brief_card_cleanup_deletes_bot_message_only(bot, monkeypatch):
    sleeps = []

    async def fake_sleep(_delay):
        sleeps.append(_delay)

    monkeypatch.setattr("app.modules.telegram.asyncio.sleep", fake_sleep)
    run(bot._handle_message(_msg("/me", user=ALICE, username="alice_tg")))
    mid = bot._panel[f"g:{GROUP}:0:{ALICE}"]
    pending = list(bot._in_flight)
    for task in pending:
        task.cancel()
    if pending:
        run(asyncio.gather(*pending, return_exceptions=True))
    bot._in_flight.clear()
    bot.calls.clear()
    sleeps.clear()
    run(bot._expire_own_card(GROUP, mid, delay=GROUP_BRIEF_TTL))
    assert sleeps == [GROUP_BRIEF_TTL]
    assert bot.calls == [("deleteMessage", {"chat_id": GROUP, "message_id": mid})]
