"""New commands open at the bottom; an operation stays on its own panel."""
import asyncio
import time

import pytest
from test_admin_commands import bot as base_bot  # noqa: F401

from app.modules.telegram import TelegramBot


@pytest.fixture(name="bot")
def clean_bot(request):
    b = request.getfixturevalue("base_bot")
    b.calls = []
    b.next_mid = 100
    b.fail_send = False
    b.fail_delete = False
    b.password_changes = []
    b._registration._config = b._cfg
    b._bot_username = "deck_test_bot"

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
        if method == "deleteMessage" and b.fail_delete:
            return None
        return True

    async def password(user_id, value):
        b.password_changes.append((user_id, value))
        return True

    b._call = call
    b._emby.set_user_password = password
    b.send = TelegramBot.send.__get__(b, TelegramBot)
    b._edit = TelegramBot._edit.__get__(b, TelegramBot)
    return b


def run(coro):
    return asyncio.run(coro)


def command(bot, text="/start", chat="900"):
    run(bot._handle_command(chat, chat, "test", text))


def tap(bot, data, chat="901", mid=77):
    run(bot._run_callback(data, chat, mid, "cb", chat, "test",
                          {"chat": {"id": chat, "type": "private"}}))


def test_new_command_sends_before_deleting_only_previous_menu(bot):
    command(bot)
    old = bot._panel["900"]
    bot.calls.clear()
    command(bot, "/me")
    assert [m for m, _ in bot.calls] == ["sendMessage", "deleteMessage"]
    assert bot.calls[-1][1]["message_id"] == old
    assert bot._panel["900"] != old


def test_failed_send_keeps_old_menu(bot):
    command(bot)
    old = bot._panel["900"]
    bot.calls.clear()
    bot.fail_send = True
    command(bot)
    assert [m for m, _ in bot.calls] == ["sendMessage"]
    assert bot._panel["900"] == old


def test_failed_delete_disarms_old_buttons(bot):
    command(bot)
    bot.calls.clear()
    bot.fail_delete = True
    command(bot)
    assert [m for m, _ in bot.calls] == ["sendMessage", "deleteMessage", "editMessageReplyMarkup"]
    assert bot.calls[-1][1]["reply_markup"] == {"inline_keyboard": []}


def test_notifications_never_become_the_cleanup_target(bot):
    command(bot)
    old = bot._panel["900"]
    run(bot.send("900", "notification"))
    run(bot.send_message("900", "uploader notice", [[{"text": "claim", "callback_data": "req_claim:1"}]]))
    bot.calls.clear()
    command(bot)
    assert bot.calls[-1] == ("deleteMessage", {"chat_id": "900", "message_id": old})


def test_buttons_and_show_keep_current_operation_message(bot):
    command(bot, chat="901")
    mid = bot._panel["901"]
    bot.calls.clear()
    tap(bot, "me", mid=mid)
    run(bot._show("901", "input accepted", bot.info_menu()))
    assert [m for m, _ in bot.calls] == ["editMessageText", "editMessageText"]
    assert all(p["message_id"] == mid for _, p in bot.calls)


def test_password_requires_confirmation_and_double_tap_is_noop(bot):
    tap(bot, "resetpw")
    assert bot.password_changes == []
    nonce = bot._pending["901"][2]["nonce"]
    tap(bot, f"resetpw_ok:{nonce}")
    assert len(bot.password_changes) == 1
    changes = len(bot.calls)
    tap(bot, f"resetpw_ok:{nonce}")
    assert len(bot.password_changes) == 1
    assert len(bot.calls) == changes  # keep the successful password visible
    assert bot.password_changes[0][1] not in str(bot.members.audit_log())


@pytest.mark.parametrize("interruption", ["cancel", "command", "expired", "wrong_message", "wrong_nonce"])
def test_password_confirmation_is_bound_and_cancelable(bot, interruption):
    tap(bot, "resetpw")
    nonce = bot._pending["901"][2]["nonce"]
    mid = 77
    if interruption == "cancel":
        tap(bot, "me")
    elif interruption == "command":
        command(bot, chat="901")
    elif interruption == "expired":
        kind, _, extra = bot._pending["901"]
        bot._pending["901"] = (kind, time.time() - 1, extra)
    elif interruption == "wrong_message":
        mid = 78
    else:
        nonce = "wrong"
    tap(bot, f"resetpw_ok:{nonce}", mid=mid)
    assert bot.password_changes == []


def test_chat_commands_replace_legacy_scope_and_actual_language(bot):
    run(bot._sync_chat_commands("900", "900", "zh-hans"))
    assert len(bot.calls) == 2
    for method, payload in bot.calls:
        assert method == "setMyCommands"
        assert payload["scope"] == {"type": "chat", "chat_id": "900"}
        assert {c["command"] for c in payload["commands"]} == {
            "start", "me", "rank", "help", "rules", "manage", "kk"}
    assert [p["language_code"] for _, p in bot.calls] == ["", "zh"]
    run(bot._sync_chat_commands("900", "900", "zh-hans"))
    assert len(bot.calls) == 2
    bot.members.set_roles("admin1", [], actor="test")
    run(bot._sync_chat_commands("900", "900", "zh-hans"))
    assert {c["command"] for c in bot.calls[-1][1]["commands"]} == {
        "start", "me", "rank", "help", "rules"}


def test_changing_bot_identity_never_deletes_other_bots_menu(bot):
    command(bot)
    cfg = dict(bot._cfg())
    cfg["bot_token"] = "999999:placeholder-only"
    bot._config = lambda: cfg
    bot.calls.clear()
    command(bot)
    assert [m for m, _ in bot.calls] == ["sendMessage"]


def test_menu_id_survives_restart_without_storing_credentials(bot):
    command(bot)
    old = bot._panel["900"]
    bot._panel.clear()
    bot._menu_panel.clear()
    bot.calls.clear()
    command(bot)
    assert bot.calls[-1] == ("deleteMessage", {"chat_id": "900", "message_id": old})
    assert bot._token() not in str(bot.db.query("SELECT * FROM meta"))


def gift(bot, recipient="777"):
    command(bot, f"/kk {recipient}")
    mid = bot._panel["900"]
    tap(bot, "admin_gift", chat="900", mid=mid)
    nonce = bot._pending["900"][2]["nonce"]
    tap(bot, f"admin_gift_ok:{nonce}", chat="900", mid=mid)
    return bot._registration.get_grant(recipient), mid, nonce


def test_kk_gift_is_bound_confirmation_and_not_direct_account_creation(bot):
    command(bot, "/kk 777")
    mid = bot._panel["900"]
    assert "尚未注册" in bot.calls[-1][1]["text"]
    assert bot._registration.get_grant("777") is None
    tap(bot, "admin_gift", chat="900", mid=mid)
    assert bot._registration.get_grant("777") is None
    nonce = bot._pending["900"][2]["nonce"]
    bot.calls.clear()
    tap(bot, f"admin_gift_ok:{nonce}", chat="900", mid=mid)
    grant = bot._registration.get_grant("777")
    assert grant["gift_days"] == 30
    assert grant["gift_group_id"] == "standard"
    assert bot.members.find_by_telegram("777") is None
    assert all(p.get("chat_id") == "900" for _, p in bot.calls)  # no recipient DM
    assert all(m == "editMessageText" for m, _ in bot.calls)
    assert "t.me/deck_test_bot?start=" + grant["gift_code"] in bot.calls[-1][1]["text"]
    assert grant["gift_code"] not in str(bot.members.audit_log())
    tap(bot, f"admin_gift_ok:{nonce}", chat="900", mid=mid)
    assert bot._registration.get_grant("777")["gift_code"] == grant["gift_code"]


@pytest.mark.parametrize("action", ["cancel", "command", "demote", "expired", "wrong_message"])
def test_gift_confirmation_does_not_survive_cancel_or_permission_change(bot, action):
    command(bot, "/kk 777")
    mid = bot._panel["900"]
    tap(bot, "admin_gift", chat="900", mid=mid)
    nonce = bot._pending["900"][2]["nonce"]
    if action == "cancel":
        tap(bot, "admin", chat="900", mid=mid)
    elif action == "command":
        command(bot)
    elif action == "demote":
        bot.members.set_roles("admin1", [], actor="test")
    elif action == "expired":
        kind, _, extra = bot._pending["900"]
        bot._pending["900"] = (kind, time.time() - 1, extra)
    else:
        mid += 1
    tap(bot, f"admin_gift_ok:{nonce}", chat="900", mid=mid)
    assert bot._registration.get_grant("777") is None


def test_kk_existing_tg_opens_user_card_and_keeps_delete_on_same_message(bot):
    command(bot, "/kk 901")
    mid = bot._panel["900"]
    assert "alice" in bot.calls[-1][1]["text"]
    tap(bot, "admin_rm", chat="900", mid=mid)
    assert bot.emby.deleted == []
    assert bot.calls[-1][0] == "editMessageText"
    assert bot.calls[-1][1]["message_id"] == mid
    nonce = bot._pending["900"][2]["nonce"]
    tap(bot, "admin_ok", chat="900", mid=mid)  # old generic confirm cannot delete
    tap(bot, "rm_self:old", chat="900", mid=mid)
    assert bot.emby.deleted == []
    tap(bot, f"rm_self:{nonce}", chat="900", mid=mid)
    assert bot.emby.deleted == ["u1"]
    assert bot.members.get("u1") is None


def test_recipient_gift_link_registration_is_single_message_and_one_use(bot):
    grant, _, _ = gift(bot)
    created = []

    async def create(name):
        created.append(name)
        return {"Id": "gifted1"}

    bot._emby.create_user = create
    bot.calls.clear()
    command(bot, "/start " + grant["gift_code"], chat="777")
    mid = bot._panel["777"]
    assert bot._pending["777"][0] == "username"
    assert bot._registration.get_grant("777")["used_at"] is None
    run(bot._handle_message({"chat": {"id": "777", "type": "private"},
                             "from": {"id": "777"}, "text": "newperson"}))
    member = bot.members.find_by_telegram("777")
    assert created == ["newperson"]
    assert member["emby_user_id"] == "gifted1"
    assert member["register_via"] == "admin"
    assert abs(member["expires_at"] - time.time() - 30 * 86400) < 5
    assert bot._registration.get_grant("777")["used_at"]
    assert [m for m, _ in bot.calls] == ["sendMessage", "editMessageText", "editMessageText"]
    assert all(p.get("message_id") == mid for m, p in bot.calls if m == "editMessageText")
    assert not bot._registration.resolve("777", grant["gift_code"]).allowed


def test_gift_link_cannot_be_claimed_by_other_pre_authorised_user(bot):
    grant, _, _ = gift(bot)
    bot._registration.grant_admin("778", "test")
    command(bot, "/start " + grant["gift_code"], chat="778")
    assert bot._pending.get("778", (None,))[0] != "username"
    assert "不属于" in bot.calls[-1][1]["text"]
    assert bot._registration.get_grant("777")["used_at"] is None


def test_gift_revalidation_before_account_creation(bot):
    grant, _, _ = gift(bot)
    command(bot, "/start " + grant["gift_code"], chat="777")
    bot._registration.revoke_grant("777")
    created = []

    async def create(name):
        created.append(name)
        return {"Id": "should-not-exist"}

    bot._emby.create_user = create
    run(bot._handle_message({"chat": {"id": "777", "type": "private"},
                             "from": {"id": "777"}, "text": "newperson"}))
    assert created == []
    assert "失效" in bot.calls[-1][1]["text"]


def test_private_forward_lookup_and_group_privacy(bot):
    command(bot, "/kk")
    run(bot._handle_message({"chat": {"id": "900", "type": "private"},
                             "from": {"id": "900"}, "text": "hello",
                             "forward_origin": {"type": "user", "sender_user": {"id": 777}}}))
    assert bot._pending["900"][2]["tg_id"] == "777"
    bot.calls.clear()
    run(bot._handle_message({"chat": {"id": -10, "type": "group"},
                             "from": {"id": "900"}, "text": "/kk 901"}))
    assert bot.calls == []
