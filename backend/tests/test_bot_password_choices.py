"""Local actual Bot handlers; credential assertions use only synthetic test values."""

import json
import time
from html import escape

import pytest
from test_request_bot import bot as request_bot  # noqa: F401
from test_request_bot import message, run

from app.modules.registration import RegistrationService
from app.modules.telegram import TelegramBot


class Emby:
    def __init__(self):
        self.created = []
        self.passwords = []
        self.deleted = []
        self.fail = False
        self.raise_secret = False

    async def create_user(self, username):
        self.created.append(username)
        return {"Id": "new-" + username}

    async def set_user_password(self, uid, password):
        self.passwords.append((uid, password))
        if self.raise_secret:
            raise RuntimeError(password)
        return not self.fail

    async def delete_user(self, uid):
        self.deleted.append(uid)
        return True


@pytest.fixture
def bot(request):
    old = request.getfixturevalue("request_bot")
    old.cfg.update(default_group_id="standard", register_days=30)
    reg = RegistrationService(old.db, old._groups, lambda: old.cfg)
    emby = Emby()
    cache = []
    b = TelegramBot(
        lambda: old.cfg,
        old.members,
        emby,
        db=old.db,
        requests=old.service,
        groups=old._groups,
        tmdb=old._tmdb,
        registration=reg,
        on_password_changed=lambda: cache.append(True),
    )
    b._call = old.transport.call
    b.transport = old.transport
    b.db = old.db
    b.members = old.members
    b.emby = emby
    b.cache_changes = cache
    return b


def tap(bot, data, chat="900", mid=None, actor=None, group=False):
    mid = mid or bot._panel.get(str(chat), 101)
    run(
        bot._handle_callback(
            {
                "id": "cb",
                "data": data,
                "from": {"id": actor or chat},
                "message": {
                    "chat": {"id": chat, "type": "supergroup" if group else "private"},
                    "message_id": mid,
                },
            }
        )
    )


def choose(bot, action, chat="900"):
    p = bot._pending[str(chat)][2]
    data = f"pw:{p['nonce']}:{action}"
    tap(bot, data, chat, p["message_id"])
    return data, p["message_id"]


def text(bot, chat="900"):
    row = bot.transport.messages[(str(chat), bot._panel[str(chat)])]
    return row.get("text") or row.get("caption", "")


def begin_reset(bot, chat="900"):
    message(bot, "/me", chat)
    tap(bot, "resetpw", chat)


def begin_register(bot, via="admin", chat="910"):
    if via == "admin":
        bot._registration.grant_admin(chat)
        message(bot, "/start", chat)
        tap(bot, "register", chat)
    elif via == "invite":
        code = bot._registration.issue_invite("u1")["code"]
        message(bot, "/start " + code, chat)
    elif via == "redeem":
        code = bot._registration.generate_redeem("standard", 30)[0]["code"]
        message(bot, "/start " + code, chat)
    else:
        code = bot._registration.issue_gift(
            chat, "admin1", origin={"chat_id": "-10055", "message_id": 11, "bot_id": "1234567"}
        )["gift_code"]
        message(bot, "/start " + code, chat)
    assert bot._pending[chat][0] == "username"
    message(bot, "ChosenUser", chat)
    assert bot._pending[chat][0] == "password_choice"


@pytest.mark.parametrize("mode", ["random", "custom"])
def test_reset_choice_confirmation_exact_password_once_private_no_audit_secret(
    bot, mode, monkeypatch
):
    generated = []

    def random_password():
        generated.append(True)
        return "SyntheticRandom123"

    monkeypatch.setattr("app.modules.telegram.generate_password", random_password)
    begin_reset(bot)
    assert "随机生成" in str(bot.transport.messages[("900", bot._panel["900"])])
    assert not generated and not bot.emby.passwords
    choose(bot, mode)
    secret = "  <&Unique 9>  " if mode == "custom" else "SyntheticRandom123"
    if mode == "custom":
        message(bot, secret)
    assert bot._pending["900"][0] == "password_confirm" and not bot.emby.passwords
    assert escape(secret) in text(bot)
    callback, mid = choose(bot, "confirm")
    tap(bot, callback, mid=mid)
    assert bot.emby.passwords == [("u1", secret)]
    assert bot.cache_changes == [True]
    assert escape(secret) in text(bot) and "u1" in text(bot)
    assert "900" not in bot._pending
    assert secret not in str(bot.db.query("SELECT * FROM audit_log"))
    assert secret not in str(bot.db.query("SELECT * FROM request_cards"))
    assert secret not in str(bot.db.query("SELECT * FROM meta"))
    assert secret not in bot._last_error
    for method, p in bot.transport.calls:
        assert secret not in json.dumps(p.get("reply_markup", {}), ensure_ascii=False)
        if secret in p.get("text", ""):
            assert str(p.get("chat_id")) == "900"
    assert len(generated) == (1 if mode == "random" else 0)


@pytest.mark.parametrize("via", ["admin", "invite", "gift", "redeem"])
@pytest.mark.parametrize("mode", ["random", "custom"])
def test_all_registration_entries_choose_password_then_final_confirm(bot, via, mode):
    begin_register(bot, via)
    assert not bot.emby.created
    choose(bot, mode, "910")
    if mode == "custom":
        message(bot, "  Reg <& 09>  ", "910")
    secret = bot._pending["910"][2]["password"]
    assert bot._pending["910"][0] == "password_confirm" and not bot.emby.created
    assert "ChosenUser" in text(bot, "910") and "30 天" in text(bot, "910")
    callback, mid = choose(bot, "confirm", "910")
    tap(bot, callback, "910", mid)
    assert bot.emby.created == ["ChosenUser"]
    assert bot.emby.passwords == [("new-ChosenUser", secret)]
    assert bot.members.find_by_telegram("910")["emby_user_id"] == "new-ChosenUser"
    assert "注册成功" in text(bot, "910") and escape(secret) in text(bot, "910")
    assert not bot._pending.get("910")
    assert secret not in str(bot.db.query("SELECT * FROM audit_log"))
    for method, p in bot.transport.calls:
        if str(p.get("chat_id", "")).startswith("-"):
            assert secret not in str(p) and escape(secret) not in str(p)
    if via in ("admin", "gift"):
        assert bot._registration.get_grant("910")["used_at"]


@pytest.mark.parametrize("purpose", ["register", "reset"])
def test_custom_validation_retry_back_cancel_and_no_secret_modification(bot, purpose):
    chat = "910" if purpose == "register" else "900"
    begin_register(bot) if purpose == "register" else begin_reset(bot)
    choose(bot, "custom", chat)
    mid = bot._panel[chat]
    message(bot, "abc", chat)
    assert "至少 6 位" in text(bot, chat) and bot._panel[chat] == mid
    assert bot._pending[chat][0] == "password_custom"
    message(bot, "GIFTabcdefghijkl", chat)
    assert bot._pending[chat][0] == "password_confirm", "password was misrouted as gift credential"
    assert not bot.emby.passwords and not bot.emby.created
    choose(bot, "choose", chat)
    assert "password" not in bot._pending[chat][2]
    choose(bot, "custom", chat)
    message(bot, " /start ", chat)
    assert bot._pending[chat][2]["password"] == " /start "
    callback = bot._pending[chat][2]
    choose(bot, "cancel", chat)
    tap(bot, f"pw:{callback['nonce']}:confirm", chat, callback["message_id"])
    assert not bot.emby.passwords and not bot.emby.created and chat not in bot._pending


@pytest.mark.parametrize(
    "interruption",
    ["command", "return", "expired", "wrong_card", "wrong_actor", "group", "rebound"],
)
def test_reset_confirmation_guard_and_private_input_only(bot, interruption):
    begin_reset(bot)
    choose(bot, "custom")
    message(bot, "SecretExactly89")
    p = bot._pending["900"][2]
    callback = f"pw:{p['nonce']}:confirm"
    mid = p["message_id"]
    chat = "900"
    actor = "900"
    if interruption == "command":
        message(bot, "/cancel")
    elif interruption == "return":
        tap(bot, "me")
    elif interruption == "expired":
        bot._pending["900"] = ("password_confirm", time.time() - 1, p)
    elif interruption == "wrong_card":
        mid += 1000
    elif interruption == "wrong_actor":
        actor = "901"
    elif interruption == "group":
        chat = "-10055"
    else:
        bot.members.unbind_telegram("u1")
    tap(bot, callback, chat, mid, actor, group=interruption == "group")
    assert not bot.emby.passwords


def test_password_input_not_routed_into_request_or_other_bot_command(bot):
    begin_reset(bot)
    choose(bot, "custom")
    message(bot, "/mySecret@another_bot")
    assert bot._pending["900"][2]["password"] == "/mySecret@another_bot"
    choose(bot, "confirm")
    assert bot.emby.passwords[0][1] == "/mySecret@another_bot"
    assert not bot.db.query("SELECT * FROM request_events")


def test_registration_cancel_then_return_username_and_admission_recheck(bot):
    begin_register(bot)
    choose(bot, "username", "910")
    assert bot._pending["910"][0] == "username"
    message(bot, "DifferentUser", "910")
    choose(bot, "custom", "910")
    message(bot, "Selected901", "910")
    bot._registration.revoke_grant("910")
    choose(bot, "confirm", "910")
    assert not bot.emby.created and not bot.members.find_by_telegram("910")


def test_adapter_exception_never_leaks_secret_to_status_audit_or_group(bot):
    bot.emby.raise_secret = True
    begin_reset(bot)
    choose(bot, "custom")
    message(bot, "InvisibleBackendSecret123")
    choose(bot, "confirm")
    assert "InvisibleBackendSecret123" not in bot._last_error
    assert "InvisibleBackendSecret123" not in str(bot.db.query("SELECT * FROM audit_log"))
    assert "InvisibleBackendSecret123" not in text(bot)
    assert not bot.cache_changes


@pytest.mark.parametrize("purpose", ["register", "reset"])
def test_password_draft_not_persisted_on_restart_and_old_buttons_cannot_execute(bot, purpose):
    chat = "910" if purpose == "register" else "900"
    begin_register(bot) if purpose == "register" else begin_reset(bot)
    choose(bot, "custom", chat)
    message(bot, "ShortLivedSecret98", chat)
    p = bot._pending[chat][2]
    restored = TelegramBot(
        bot._cfg,
        bot.members,
        bot.emby,
        db=bot.db,
        groups=bot._groups,
        registration=bot._registration,
        requests=bot._requests,
    )
    restored._call = bot.transport.call
    tap(restored, f"pw:{p['nonce']}:confirm", chat, p["message_id"])
    assert not bot.emby.created and not bot.emby.passwords
    assert "ShortLivedSecret98" not in str(bot.db.query("SELECT * FROM meta"))
    if purpose == "register":
        assert not bot._registration.get_grant("910")["used_at"]


def test_membership_recheck_return_cancels_old_password_confirmation(bot):
    begin_reset(bot)
    choose(bot, "custom")
    message(bot, "DiscardAfterGuide90")
    p = bot._pending["900"][2]

    async def allowed(*a):
        return True

    bot.membership.gate = allowed
    tap(bot, "membership_recheck", mid=p["message_id"])
    assert "900" not in bot._pending
    tap(bot, f"pw:{p['nonce']}:confirm", mid=p["message_id"])
    assert not bot.emby.passwords


def test_group_custom_text_cannot_create_or_set_password(bot):
    begin_reset(bot)
    choose(bot, "custom")
    run(
        bot._handle_message(
            {
                "chat": {"id": "-10055", "type": "supergroup"},
                "from": {"id": "900"},
                "text": "NeverInPublic123",
            }
        )
    )
    assert not bot.emby.passwords
    for method, p in bot.transport.calls:
        if str(p.get("chat_id", "")).startswith("-"):
            assert "NeverInPublic123" not in str(p)


@pytest.mark.parametrize("mode", ["random", "custom"])
def test_registration_password_failure_keeps_qualification_and_removes_remote_orphan(bot, mode):
    begin_register(bot)
    choose(bot, mode, "910")
    if mode == "custom":
        message(bot, "FailedRegSecret98", "910")
    bot.emby.fail = True
    choose(bot, "confirm", "910")
    assert not bot.members.find_by_telegram("910")
    assert not bot._registration.get_grant("910")["used_at"]
    assert bot.emby.deleted == ["new-ChosenUser"]
    assert "注册未完成" in text(bot, "910")
