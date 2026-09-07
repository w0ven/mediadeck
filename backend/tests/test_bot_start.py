"""/start must open the bot for everybody.

It is the first message every ordinary member ever sends, so routing it through
the admin gate answered the very first contact with "no permission" -- and
admins fared no better, because there is no _cmd_start to dispatch to.
"""
from __future__ import annotations

import asyncio
import time

from app.modules.telegram import TelegramBot

FAKE_CRED = "123456:synthetic-not-a-real-token"


class _Members:
    def __init__(self, linked: dict | None = None) -> None:
        self._linked = linked or {}

    def find_by_telegram(self, tg_user_id: str):
        return self._linked.get(str(tg_user_id))

    def find_by_username(self, username: str):
        return None

    def list(self, **kw):
        return list(self._linked.values())

    def devices(self, user_id):
        return []


def _bot(members=None) -> TelegramBot:
    cfg = {"enabled": True, "bot_token": FAKE_CRED, "allow_admin_grant": True,
           "allow_invite": True, "allow_redeem": True, "register_days": 30,
           "max_users": 0, "require_group": "", "default_group_id": "",
           "emby_public_url": "https://emby.example"}
    bot = TelegramBot(lambda: cfg, members or _Members())
    bot.sent = []  # type: ignore[attr-defined]

    async def fake_send(chat, text, keyboard=None):
        bot.sent.append(text)  # type: ignore[attr-defined]
        return True

    bot.send = fake_send  # type: ignore[assignment]
    return bot


def _member(**kw):
    row = {"emby_user_id": "u1", "username": "someone", "status": "active",
           "expires_at": int(time.time()) + 86400, "roles": []}
    row.update(kw)
    return row


def _start(bot, tg_user_id="999"):
    asyncio.run(bot._handle_command(1, tg_user_id, "someone", "/start"))
    return bot.sent[-1]  # type: ignore[attr-defined]


def test_linked_member_gets_their_home_screen_not_a_refusal():
    bot = _bot(_Members({"999": _member()}))
    text = _start(bot)
    assert "无权限" not in text
    assert "someone" in text


def test_unlinked_visitor_gets_the_guest_screen():
    """A brand-new chat has no member row at all; it must still be welcomed."""
    bot = _bot()
    text = _start(bot)
    assert "无权限" not in text
    assert "账号服务" in text


def test_admin_start_is_not_answered_as_an_unknown_command():
    bot = _bot(_Members({"999": _member(roles=["admin"])}))
    text = _start(bot)
    assert "无权限" not in text and "未知命令" not in text
    assert "someone" in text


def test_start_with_botname_suffix_still_opens():
    """Group clients send /start@thebot; the suffix must not change routing."""
    bot = _bot(_Members({"999": _member()}))
    asyncio.run(bot._handle_command(1, "999", "someone", "/start@deckbot"))
    assert "无权限" not in bot.sent[-1]  # type: ignore[attr-defined]


def test_real_admin_commands_are_still_refused_for_members():
    """The permission gate itself must stay intact."""
    bot = _bot(_Members({"999": _member()}))
    asyncio.run(bot._handle_command(1, "999", "someone", "/renewall 30"))
    assert "无权限" in bot.sent[-1]  # type: ignore[attr-defined]


def test_help_still_falls_back_to_home_for_members():
    bot = _bot(_Members({"999": _member()}))
    asyncio.run(bot._handle_command(1, "999", "someone", "/help"))
    assert "无权限" not in bot.sent[-1]  # type: ignore[attr-defined]
