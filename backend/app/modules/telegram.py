"""Telegram bot: registration, account self-service and rankings.

The bot is the front door for new members. Someone who has never been here
registers in the chat and walks away with a working Emby account; someone who
already has one lands on their own status. The keyboard is chosen from that
state on every render, so neither audience is offered buttons that lead
nowhere.

Registration creates the Emby account directly. There is no code to copy from
a panel, because the chat itself already proves who is asking: the Telegram id
is the identity, and it is recorded as the owner at creation time. That leaves
exactly two cases needing human review, and they both go through the approval
queue rather than the registration path:

- someone whose Emby account predates the bot and wants to claim it
- someone moving their account to a different Telegram id

Both are attempts to take control of an account the requester cannot otherwise
prove they own, so an operator decides.

Passwords are generated, never typed. A chat transcript is not a safe place to
put one, and a password the member chose in a hurry is the one they reuse.

Polling, not webhooks. A webhook needs a public HTTPS route into the panel;
long polling reaches out instead, so the panel stays reachable only from where
it already was.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import string
import time
from typing import Any

import httpx

from app.core.errors import ConfigError
from app.modules.groups import WHITELIST_GROUP_ID
from app.modules.requests import RequestError
from app.modules.shop import ShopError
from app.modules.tmdb import parse_link, poster_url

API_ROOT = "https://api.telegram.org"

# Telegram closes an idle long poll itself; this only has to be shorter than
# the client timeout so a hung socket is noticed rather than waited on forever.
POLL_TIMEOUT = 25
HTTP_TIMEOUT = POLL_TIMEOUT + 10

# A username has to survive being an Emby login and a path component.
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,19}$")

# Invite codes are 8 characters, redeem cards 12. A guest who pastes one
# should not have to find the register button first.
CREDENTIAL_LENGTHS = {8, 12}

BACK_HOME: list[list[dict[str, str]]] = [
    [{"text": "◀ 返回", "callback_data": "home"}],
]


def looks_like_credential(raw: str) -> bool:
    """True when the text is the shape of an invite code or a card.

    This is a shape check, not a validity check: a mistyped code still looks
    like a code, and the registration service is what says so.
    """
    compact = "".join(ch for ch in str(raw or "").strip().upper() if ch.isalnum())
    return len(compact) in CREDENTIAL_LENGTHS

# Registration conversation state is intentionally short-lived: an abandoned
# half-finished signup should not hold a slot or confuse the next /start.
PENDING_TTL = 600.0

REQUEST_KINDS = ("bind", "rebind")

# Status marks for the member's own request list.
REQUEST_STATUS_ICONS = {
    "open": "🕓", "claimed": "🔧", "done": "✅", "rejected": "❌",
}

# Callbacks whose handler sends its own answerCallbackQuery, because it has
# something to say. Everything else is acked immediately.
SELF_ANSWERING_CALLBACKS = ("req_claim:", "req_done:", "req_fail:")

ADMIN_HELP = """🛠 <b>管理员命令</b>

<b>查询</b>
<code>/kk 用户</code> 查看账号详情
<code>/req [open|claimed]</code> 最近 10 条求片

<b>用户组</b>
<code>/prouser 用户</code> 移入白名单（永不过期）
<code>/revuser 用户</code> 移回默认组

<b>有效期</b>
<code>/renew 用户 天数</code> 续期
<code>/renewall 天数</code> 全员续期（需确认）

<b>积分与发放</b>
<code>/score 用户 ±数量</code> 调整积分
<code>/scoreall 数量</code> 全员加分（需确认）
<code>/gift 用户 traffic|days|bandwidth|invite 数量</code> 直接发放
<code>/invite 用户 次数</code> 增加邀请名额

<b>账号</b>
<code>/rm 用户</code> 删号（默认只删本人；连带邀请人需单独确认）
<code>/code 套餐id 天数 数量</code> 生成卡密
<code>/auth TelegramID</code> 预授权注册

<i>用户可写 Emby 用户名或 @Telegram 用户名。</i>"""


def generate_password(length: int = 12) -> str:
    """Passwords are issued, not chosen: the member never types one in chat."""
    pool = string.ascii_letters + string.digits
    return "".join(secrets.choice(pool) for _ in range(length))


def _fmt_expiry(expires_at: int | None) -> str:
    if not expires_at:
        return "永久"
    left = expires_at - int(time.time())
    if left <= 0:
        return "已过期"
    days = left // 86400
    if days >= 1:
        return f"{days} 天后到期"
    return f"{max(1, left // 3600)} 小时内到期"


def _as_request_id(data: str) -> int:
    """Trailing id of a ``req_*:<id>`` callback. 0 when malformed."""
    _, _, tail = str(data or "").partition(":")
    return int(tail) if tail.isdigit() else 0


def _fmt_bytes(n: int | None) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class TelegramBot:
    """Long-polling bot bound to the panel's member records."""

    def __init__(self, config_provider: Any, members: Any, emby: Any = None,
                 stats: Any = None, db: Any = None,
                 registration: Any = None, points: Any = None,
                 shop: Any = None, plugins: Any = None,
                 scheduler: Any = None, requests: Any = None,
                 tmdb: Any = None, groups: Any = None) -> None:
        self._config = config_provider
        self._members = members
        self._emby = emby
        self._stats = stats
        self._db = db
        self._registration = registration
        self._points = points
        self._shop = shop
        # The registry, not the plugins themselves: whether a feature is on is
        # an operator decision that can change between two taps of the same
        # keyboard, so it is read at render time rather than captured here.
        self._plugins = plugins
        self._scheduler = scheduler
        self._requests = requests
        self._tmdb = tmdb
        self._groups = groups
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._last_error = ""
        self._last_poll_at = 0.0
        self._started_at = 0.0
        # chat id -> what the bot is waiting for, with a deadline
        self._pending: dict[str, tuple[str, float, dict[str, Any]]] = {}
        # chat id -> the one panel message this conversation is editing.
        # A request that keeps adding replies is what made 求片 feel messy.
        self._panel: dict[str, int] = {}
        self._bot_username = ""
        self._commands_installed = False
        # One client, reused. Opening TLS to api.telegram.org from this host
        # costs a few hundred milliseconds on a good day and a few seconds on a
        # bad one; a tap that answers the callback and then edits the message
        # used to pay that twice, every time.
        self._http: httpx.AsyncClient | None = None
        self._http_token = ""
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._in_flight: set[asyncio.Task] = set()
        self._chat_locks: dict[str, asyncio.Lock] = {}

    def bind_plugins(self, registry: Any) -> None:
        """Late-bind the plugin registry.

        The bot is constructed before the plugins are registered -- they take
        it as a context member so they can message people -- so the dependency
        runs both ways and one of them has to be attached afterwards.
        """
        self._plugins = registry

    # -- config ---------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        return self._config() or {}

    def _token(self) -> str:
        return str(self._cfg().get("bot_token") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled")) and bool(self._token())

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done()),
            "enabled": self.enabled,
            "last_poll_at": int(self._last_poll_at) or None,
            "last_error": self._last_error,
            "pending_conversations": len(self._pending),
            "started_at": int(self._started_at) or None,
        }

    # -- transport ------------------------------------------------------------

    async def _close_http(self) -> None:
        client = self._http
        self._http = None
        self._http_token = ""
        self._http_loop = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _client(self) -> httpx.AsyncClient | None:
        """Keep-alive client for api.telegram.org, recreated if the token changes."""
        token = self._token()
        if not token:
            await self._close_http()
            return None
        loop = asyncio.get_running_loop()
        if (self._http is None or self._http_token != token
                or self._http_loop is not loop):
            await self._close_http()
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(HTTP_TIMEOUT, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
            )
            self._http_token = token
            self._http_loop = loop
        return self._http

    async def _call(self, method: str, payload: dict[str, Any] | None = None,
                    timeout: float = 20) -> Any:
        auth_part = self._token()
        if not auth_part:
            return None
        client = await self._client()
        if client is None:
            return None
        url = f"{API_ROOT}/bot{auth_part}/{method}"
        try:
            r = await client.post(url, json=payload or {}, timeout=timeout)
            body = r.json()
        except Exception as exc:  # noqa: BLE001 - surfaced through status
            # The token is in the URL, so raw exception text is not safe to keep.
            self._last_error = f"{type(exc).__name__}: 请求失败"
            return None
        if not body.get("ok"):
            self._last_error = str(body.get("description") or "Telegram 拒绝了请求")
            return None
        self._last_error = ""
        return body.get("result")

    async def verify(self) -> dict[str, Any]:
        """Check the credential by asking who the bot is. Never echoes it."""
        me = await self._call("getMe", timeout=15)
        if not me:
            return {"ok": False, "error": self._last_error or "无法连接 Telegram"}
        self._bot_username = str(me.get("username") or "")
        await self._install_commands()
        return {"ok": True, "username": self._bot_username,
                "name": me.get("first_name", ""), "id": me.get("id")}

    async def _install_commands(self) -> None:
        """Put /start and /help on Telegram's command menu, once."""
        if self._commands_installed or not self._token():
            return
        result = await self._call("setMyCommands", {
            "commands": [
                {"command": "start", "description": "打开账号服务"},
                {"command": "help", "description": "使用说明"},
            ],
        }, timeout=15)
        if result is not None:
            self._commands_installed = True

    async def _ensure_identity(self) -> None:
        if self._bot_username and self._commands_installed:
            return
        me = await self._call("getMe", timeout=15)
        if not me:
            return
        self._bot_username = str(me.get("username") or "")
        await self._install_commands()

    def _start_link(self, code: str) -> str:
        """t.me deep link that lands a guest on this code. Empty if unknown."""
        user = self._bot_username
        compact = "".join(ch for ch in str(code or "").strip() if ch.isalnum())
        if not user or not compact:
            return ""
        return f"https://t.me/{user}?start={compact}"

    def _touch_panel(self, chat_id: Any, message_id: Any) -> None:
        if chat_id is None or not message_id:
            return
        try:
            self._panel[str(chat_id)] = int(message_id)
        except (TypeError, ValueError):
            return

    async def send(self, chat_id: str | int, text: str,
                   keyboard: list[list[dict[str, str]]] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = await self._call("sendMessage", payload)
        if isinstance(result, dict) and result.get("message_id"):
            self._touch_panel(chat_id, result["message_id"])
            return True
        return result is not None

    async def _answer_callback(self, callback_id: str, text: str = "") -> None:
        # Telegram shows a spinner until this lands. Keep it short so a hung
        # ack cannot sit in front of the real reply.
        await self._call("answerCallbackQuery",
                         {"callback_query_id": callback_id, "text": text},
                         timeout=10)

    async def send_message(self, chat_id: str | int, text: str,
                           keyboard: list[list[dict[str, str]]] | None = None
                           ) -> int | None:
        """Like send(), but returns the message id.

        The request fan-out needs it: when one uploader claims, every other
        uploader's message has to be edited, and without its id there is no
        way to reach it.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = await self._call("sendMessage", payload)
        if isinstance(result, dict) and result.get("message_id"):
            self._touch_panel(chat_id, result["message_id"])
            return result.get("message_id")
        return None

    async def send_photo(self, chat_id: str | int, photo: str, caption: str,
                         keyboard: list[list[dict[str, str]]] | None = None
                         ) -> bool:
        """Poster + caption. Falls back to text if the upload is refused:
        a member who asked for a film should get the confirmation either way.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id, "photo": photo, "caption": caption,
            "parse_mode": "HTML",
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        if await self._call("sendPhoto", payload) is not None:
            return True
        return await self.send(chat_id, caption, keyboard)

    async def _edit(self, chat_id: str | int, message_id: int, text: str,
                    keyboard: list[list[dict[str, str]]] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = await self._call("editMessageText", payload)
        if result is not None or "not modified" in (self._last_error or "").lower():
            self._touch_panel(chat_id, message_id)
            return True
        return False

    async def _show(self, chat_id: Any, text: str,
                    keyboard: list[list[dict[str, str]]] | None = None) -> None:
        """Keep the conversation on one bot message: edit it, or send the first."""
        mid = self._panel.get(str(chat_id))
        if mid and await self._edit(chat_id, mid, text, keyboard):
            return
        await self.send(chat_id, text, keyboard)

    # -- group membership -----------------------------------------------------

    async def in_required_group(self, tg_user_id: str) -> tuple[bool, str]:
        """Is this user in the group registration requires?

        Returns (allowed, reason). With no group configured everyone passes.

        A lookup failure passes too: Telegram being unreachable, or the bot not
        being an administrator of the group, must not silently close
        registration for everyone. The operator sees the reason instead.
        """
        chat = str(self._cfg().get("require_group") or "").strip()
        if not chat:
            return True, ""
        result = await self._call(
            "getChatMember", {"chat_id": chat, "user_id": int(tg_user_id)},
            timeout=15)
        if result is None:
            return True, "group-check-unavailable"
        status = str((result or {}).get("status") or "")
        if status in ("creator", "administrator", "member", "restricted"):
            return True, status
        return False, status or "left"

    # -- menus ----------------------------------------------------------------

    def _group_join_button(self) -> dict[str, str] | None:
        """A t.me button when the required group has a public handle."""
        chat = str(self._cfg().get("require_group") or "").strip()
        if chat.startswith("@"):
            return {"text": "📣 加入官方群", "url": f"https://t.me/{chat[1:]}"}
        if chat.startswith("https://t.me/"):
            return {"text": "📣 加入官方群", "url": chat}
        return None

    def guest_menu(self) -> list[list[dict[str, str]]]:
        """No account yet: register if a channel is open, or claim one that exists."""
        rows: list[list[dict[str, str]]] = []
        if self._registration_open():
            rows.append([{"text": "🆕 注册账号", "callback_data": "register"}])
        rows.append([
            {"text": "🔗 认领已有账号", "callback_data": "claim"},
            {"text": "❓ 使用说明", "callback_data": "help"},
        ])
        join = self._group_join_button()
        if join:
            rows.append([join])
        return rows

    def _plugin_on(self, plugin_id: str) -> bool:
        """Is this points feature switched on right now?

        A button for a disabled feature is worse than no button: it promises
        something and then explains why it cannot. So the keyboard is built
        from the current answer, every time it is drawn.
        """
        if self._plugins is None:
            return False
        try:
            return bool(self._plugins.enabled(plugin_id))
        except Exception:  # noqa: BLE001 - a broken registry hides the button
            return False

    def member_menu(self) -> list[list[dict[str, str]]]:
        """Top level: identity, backpack, and whatever points features are on.

        Two entry points rather than ten buttons. The old flat menu grew a row
        every time something was added, and a member looking for their expiry
        date had to read past invite codes to find it.
        """
        rows: list[list[dict[str, str]]] = [
            [{"text": "👤 我的信息", "callback_data": "me"},
             {"text": "🎒 背包", "callback_data": "bag"}],
        ]
        points_row = []
        if self._plugin_on("checkin"):
            points_row.append({"text": "✅ 签到", "callback_data": "checkin"})
        if self._plugin_on("points_transfer"):
            points_row.append({"text": "💸 转账", "callback_data": "transfer"})
        if points_row:
            rows.append(points_row)
        rows.append([{"text": "🎬 求片", "callback_data": "req_new"},
                     {"text": "🏆 排行", "callback_data": "top"}])
        return rows

    def _with_admin_row(self, rows: list[list[dict[str, str]]],
                        member: dict[str, Any] | None) -> list[list[dict[str, str]]]:
        if self.is_admin(member):
            rows.append([{"text": "🛠 管理", "callback_data": "admin"}])
        return rows

    @staticmethod
    def info_menu() -> list[list[dict[str, str]]]:
        """Everything about this one account, one level down."""
        return [
            [{"text": "📋 账号状态", "callback_data": "me_status"},
             {"text": "💰 积分", "callback_data": "me_points"}],
            [{"text": "📡 线路", "callback_data": "me_nodes"},
             {"text": "📺 设备", "callback_data": "devices"}],
            [{"text": "📊 观看统计", "callback_data": "usage"},
             {"text": "📋 我的求片", "callback_data": "my_requests"}],
            [{"text": "🔑 重置密码", "callback_data": "resetpw"}],
            [{"text": "◀ 返回", "callback_data": "home"}],
        ]

    @staticmethod
    def bag_menu() -> list[list[dict[str, str]]]:
        """What the member owns or can spend."""
        return [
            [{"text": "🎫 我的邀请码", "callback_data": "invites"},
             {"text": "🎁 兑换商城", "callback_data": "shop"}],
            [{"text": "📜 兑换记录", "callback_data": "orders"}],
            [{"text": "◀ 返回", "callback_data": "home"}],
        ]

    def _member_for_chat(self, tg_user_id: str) -> dict[str, Any] | None:
        return self._members.find_by_telegram(str(tg_user_id))

    @staticmethod
    def _status_label(member: dict[str, Any]) -> str:
        return {
            "active": "✅ 正常", "suspended": "⛔ 已停用",
            "expired": "⌛ 已过期", "exhausted": "📵 已超额",
            "pending": "🕓 待开通",
        }.get(str(member.get("status") or ""), str(member.get("status") or "未知"))

    def _help_text(self, member: dict[str, Any] | None) -> str:
        if member:
            return (
                "❓ <b>使用说明</b>\n\n"
                "· <b>我的信息</b>：状态、有效期、设备、线路和密码\n"
                "· <b>背包</b>：邀请码、积分兑换\n"
                "· <b>求片</b>：发送影片链接即可提交\n"
                "· 发送 /start 随时回到首页\n\n"
                "遇到问题请联系管理员。"
            )
        cfg = self._cfg()
        channels = []
        if cfg.get("allow_invite", True):
            channels.append("邀请码")
        if cfg.get("allow_redeem", True):
            channels.append("卡密")
        how = "或".join(channels) if channels else "管理员授权"
        return (
            "❓ <b>使用说明</b>\n\n"
            f"· <b>注册账号</b>：发送{how}，再选一个用户名，密码由系统生成\n"
            "· 也可以直接发送邀请码，或打开朋友给的注册链接\n"
            "· <b>认领已有账号</b>：老账号关联到这个 Telegram，需管理员确认\n\n"
            "遇到问题请联系管理员。"
        )

    def _home(self, tg_user_id: str, tg_name: str) -> tuple[str, list[list[dict[str, str]]]]:
        member = self._member_for_chat(tg_user_id)
        if not member:
            return self._guest_home(tg_name), self.guest_menu()
        bits = [self._status_label(member)]
        group = str(member.get("group_name") or "").strip()
        if group:
            bits.append(group)
        bits.append(_fmt_expiry(member.get("expires_at")))
        lines = [
            f"🎬 <b>{member.get('username') or '成员'}</b>",
            " · ".join(bits),
        ]
        user_id = str(member.get("emby_user_id") or "")
        balance = self._balance(user_id)
        if balance or self._plugin_on("checkin") or self._plugin_on("points_transfer"):
            lines.append(f"积分 <b>{balance}</b>")
        server = str(self._cfg().get("emby_public_url") or "").strip()
        if server:
            lines.append(f"服务器：{server}")
        return "\n".join(lines), self._with_admin_row(self.member_menu(), member)

    def _guest_home(self, tg_name: str) -> str:
        cfg = self._cfg()
        open_ = self._registration_open()
        channels = []
        if cfg.get("allow_invite", True):
            channels.append("邀请码")
        if cfg.get("allow_redeem", True):
            channels.append("卡密")
        lines = [f"👋 你好，{tg_name}\n"]
        if open_:
            lines.append("这里是影视库账号服务，<b>开放注册中</b>。\n")
            if channels:
                how = "或".join(channels)
                lines.append(f"没有账号：用{how}即可开通")
            else:
                lines.append("管理员已授权的用户可以直接注册。")
            lines.append("已有账号：点「认领已有账号」关联到这个 Telegram")
        else:
            lines.append("这里是影视库账号服务，<b>当前暂停注册</b>。\n")
            lines.append("已有账号可以认领；新用户请稍后再来，或联系管理员。")
        if str(cfg.get("require_group") or "").strip():
            lines.append("\n注册前需要先加入官方群组。")
        return "\n".join(lines)

    # -- registration ---------------------------------------------------------

    def _sweep_pending(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for chat, (_, deadline, _) in list(self._pending.items()):
            if deadline <= now:
                self._pending.pop(chat, None)

    def registration_slots(self) -> tuple[int, int]:
        """(used, cap). A cap of 0 means unlimited."""
        cap = int(self._cfg().get("max_users") or 0)
        used = 0
        if self._db is not None:
            with contextlib.suppress(Exception):
                row = self._db.one("SELECT COUNT(*) AS n FROM members")
                used = int((row or {}).get("n") or 0)
        return used, cap

    def _registration_open(self) -> bool:
        """Is any channel open? Three closed switches is a closed door."""
        cfg = self._cfg()
        return any(bool(cfg.get(key, True)) for key in
                   ("allow_admin_grant", "allow_invite", "allow_redeem"))

    async def _registration_blocked(self, tg_user_id: str) -> str:
        """Why this user may not register right now, or '' if they may.

        This is the gate that applies to *everyone*, whatever channel they came
        through: slots, group membership, Emby being reachable. Which channel
        admits them is a separate question, answered by RegistrationService.
        """
        if not self._registration_open():
            return "当前暂停注册，请稍后再来或联系管理员。"
        if self._emby is None:
            return "后台未连接 Emby，暂时无法开户。"
        used, cap = self.registration_slots()
        if cap and used >= cap:
            return f"注册名额已满（{used}/{cap}），请联系管理员。"
        allowed, _status = await self.in_required_group(tg_user_id)
        if not allowed:
            return "需要先加入官方群组才能注册。"
        return ""

    _USERNAME_PROMPT = (
        "🆕 <b>注册账号</b>\n\n请直接发送你想要的用户名：\n\n"
        "· 3–20 个字符，字母开头\n"
        "· 只能用字母、数字和下划线\n\n"
        "<i>密码由系统生成，不需要你输入。10 分钟内有效。</i>")

    def _credential_prompt(self) -> str:
        cfg = self._cfg()
        kinds = []
        if cfg.get("allow_invite", True):
            kinds.append("邀请码（8 位，老用户生成）")
        if cfg.get("allow_redeem", True):
            kinds.append("卡密（12 位，管理员发放）")
        detail = "\n".join(f"· {item}" for item in kinds) or "· 请发送管理员给你的凭证"
        return (
            "🎟 <b>注册账号</b>\n\n请发送你的凭证：\n\n"
            f"{detail}\n\n"
            "<i>大小写不敏感，10 分钟内有效。发送 /start 可取消。</i>"
        )

    async def _start_registration(self, chat_id: Any, tg_user_id: str,
                                   credential: str | None = None) -> None:
        """Pre-authorised users skip straight to the username.

        Asking someone the operator already named for a code they were never
        given is a dead end they cannot get out of. A code already in hand —
        pasted, or carried in /start — skips the prompt the other way.
        """
        if self._member_for_chat(tg_user_id):
            await self.send(chat_id, "你已经有账号了。", self.member_menu())
            return
        blocked = await self._registration_blocked(tg_user_id)
        if blocked:
            await self.send(chat_id, f"🚫 {blocked}", self.guest_menu())
            return
        self._sweep_pending()

        admission = self._resolve(tg_user_id, None)
        if admission is not None and admission.allowed:
            self._pending[str(chat_id)] = (
                "username", time.time() + PENDING_TTL,
                {"admission": admission})
            await self.send(chat_id, self._USERNAME_PROMPT, BACK_HOME)
            return

        if self._registration is None:
            # No registration service wired (older deployments / tests): fall
            # back to the plain username step rather than blocking everyone.
            self._pending[str(chat_id)] = ("username", time.time() + PENDING_TTL, {})
            await self.send(chat_id, self._USERNAME_PROMPT, BACK_HOME)
            return

        if credential:
            await self._submit_credential(chat_id, tg_user_id, credential)
            return

        self._pending[str(chat_id)] = (
            "credential", time.time() + PENDING_TTL, {})
        await self.send(chat_id, self._credential_prompt(), BACK_HOME)

    def _resolve(self, tg_user_id: str, credential: str | None) -> Any:
        """Ask the registration service for a verdict, tolerating its absence."""
        if self._registration is None:
            return None
        try:
            return self._registration.resolve(tg_user_id, credential)
        except Exception:  # noqa: BLE001 - a member must not see a stack trace
            self._last_error = "注册通道解析失败"
            return None

    async def _submit_credential(self, chat_id: Any, tg_user_id: str,
                                 credential: str) -> None:
        """Validate the code, then move to the username step.

        The credential is checked but *not* spent here: the account does not
        exist yet, and a failure after this point must leave the code good.
        """
        admission = self._resolve(tg_user_id, credential)
        if admission is None:
            self._pending.pop(str(chat_id), None)
            await self.send(chat_id, "🚫 注册暂时不可用，请稍后再试。",
                            self.guest_menu())
            return
        if not admission.allowed:
            # The conversation stays open: a mistyped code should cost one
            # message, not the whole flow.
            await self.send(
                chat_id,
                f"❌ {admission.reason}\n\n请重新发送邀请码或卡密，或点下面的按钮返回。",
                BACK_HOME)
            return
        self._pending[str(chat_id)] = (
            "username", time.time() + PENDING_TTL, {"admission": admission})
        await self.send(chat_id, f"✅ {admission.reason}\n\n" + self._USERNAME_PROMPT,
                        BACK_HOME)

    async def _finish_registration(self, chat_id: Any, tg_user_id: str,
                                   tg_username: str, username: str,
                                   admission: Any = None) -> None:
        username = username.strip()
        if not USERNAME_RE.match(username):
            await self.send(chat_id,
                            "❌ 用户名不符合要求：3–20 字符、字母开头、只含字母数字下划线。\n"
                            "请重新发送一个。", BACK_HOME)
            return

        # Re-check at the moment of creation, not only when the conversation
        # started: a slot can fill or registration can close while someone is
        # still typing.
        blocked = await self._registration_blocked(tg_user_id)
        if blocked:
            self._pending.pop(str(chat_id), None)
            await self.send(chat_id, f"🚫 {blocked}", self.guest_menu())
            return

        await self.send(chat_id, "⏳ 正在创建账号…")
        password = generate_password()
        try:
            created = await self._emby.create_user(username)
        except Exception:  # noqa: BLE001 - message is for a member, not a dev
            created = None
        if not created or not created.get("Id"):
            self._pending.pop(str(chat_id), None)
            await self.send(
                chat_id,
                "❌ 创建失败，可能是用户名已被占用。请点「注册账号」换一个再试。",
                self.guest_menu())
            return

        emby_id = str(created["Id"])
        with contextlib.suppress(Exception):
            await self._emby.set_user_password(emby_id, password)

        cfg = self._cfg()
        now = int(time.time())
        # The admission decides the terms when there is one: a card bought for
        # a better group must not silently downgrade to the default plan.
        days = int(cfg.get("register_days") or 0)
        group_id = str(cfg.get("default_group_id") or "")
        via, inviter_id = "admin", ""
        if admission is not None:
            via = str(getattr(admission, "via", "") or "admin")
            inviter_id = str(getattr(admission, "inviter_id", "") or "")
            group_id = str(getattr(admission, "group_id", "") or group_id)
            days = int(getattr(admission, "days", 0) or 0)

        payload: dict[str, Any] = {
            "status": "active",
            "register_via": via,
            "inviter_id": inviter_id,
            "register_at": now,
        }
        if group_id:
            payload["group_id"] = group_id
        if days > 0:
            payload["expires_at"] = now + days * 86400

        self._members.upsert(emby_id, username, payload, actor="telegram")
        self._members.bind_telegram(emby_id, tg_user_id, tg_username,
                                    actor="telegram")
        # Only now: the account exists and the chat is linked, so spending the
        # credential can no longer strand someone who paid for it.
        if admission is not None and self._registration is not None:
            with contextlib.suppress(Exception):
                self._registration.consume(admission, emby_id)
        self._pending.pop(str(chat_id), None)

        server = str(cfg.get("emby_public_url") or "").strip()
        lines = [
            "✅ <b>注册成功</b>\n",
            f"用户名：<code>{username}</code>",
            f"密码：<code>{password}</code>",
        ]
        if server:
            lines.append(f"服务器：{server}")
        if days > 0:
            lines.append(f"有效期：{days} 天")
        else:
            lines.append("有效期：永久")
        lines.append("\n<i>请立刻保存密码，这条消息不会再发第二次。</i>")
        await self.send(chat_id, "\n".join(lines), self.member_menu())

    # -- member invites -------------------------------------------------------

    async def _invites_view(self, chat_id: Any, message_id: int,
                            member: dict[str, Any], mint: bool = False) -> None:
        """A member's own invite codes, and the slots they have left.

        Minting is a member-visible spend: one slot becomes one single-use
        code. Showing the remaining count next to the button is what stops the
        obvious "why did nothing happen" when they are out.
        """
        if self._registration is None:
            await self._edit(chat_id, message_id, "邀请功能暂未开放。",
                             self.bag_menu())
            return
        user_id = str(member.get("emby_user_id") or "")
        notice = ""
        if mint:
            try:
                issued = self._registration.spend_quota_for_invite(user_id)
                notice = f"✅ 新邀请码：<code>{issued.get('code', '')}</code>\n\n"
            except Exception as exc:  # noqa: BLE001 - shown to a member
                notice = f"❌ {exc}\n\n"

        quota = 0
        codes: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            quota = self._registration.invite_quota(user_id)
            codes = self._registration.list_invites(user_id, limit=10)

        lines = [f"{notice}🎫 <b>我的邀请码</b>\n", f"剩余名额：<b>{quota}</b>"]
        if codes:
            lines.append("")
            for row in codes:
                left = int(row.get("uses_left") or 0)
                if row.get("revoked"):
                    tail = "已作废"
                elif left <= 0:
                    tail = "已用完"
                else:
                    tail = f"剩 {left} 次 · {_fmt_expiry(row.get('expires_at'))}"
                lines.append(f"<code>{row.get('code', '')}</code> · {tail}")
                link = self._start_link(str(row.get("code") or ""))
                if link and left > 0 and not row.get("revoked"):
                    lines.append(f"    {link}")
        else:
            lines.append("\n你还没有生成过邀请码。")
        if self._bot_username:
            lines.append("\n<i>把链接发给朋友，点开即可注册。</i>")
        else:
            lines.append("\n<i>把邀请码发给朋友，他们注册时填写即可。</i>")

        keyboard: list[list[dict[str, str]]] = []
        if quota > 0:
            keyboard.append(
                [{"text": f"➕ 生成新码（剩 {quota}）",
                  "callback_data": "invite_new"}])
        keyboard.append([{"text": "◀ 返回", "callback_data": "bag"}])
        await self._edit(chat_id, message_id, "\n".join(lines), keyboard)

    # -- points ---------------------------------------------------------------

    def _plugin(self, plugin_id: str) -> Any:
        if self._plugins is None:
            return None
        with contextlib.suppress(Exception):
            return self._plugins.get(plugin_id)
        return None

    def _balance(self, user_id: str) -> int:
        if self._points is None:
            return 0
        with contextlib.suppress(Exception):
            return int(self._points.balance(user_id))
        return 0

    def _points_text(self, member: dict[str, Any]) -> str:
        """Balance plus the last few rows that produced it.

        The history is the point: a number on its own invites 'where did my
        points go', and the answer is already written down.
        """
        user_id = str(member.get("emby_user_id") or "")
        lines = [f"💰 <b>我的积分</b>\n\n当前余额：<b>{self._balance(user_id)}</b>"]
        rows: list[dict[str, Any]] = []
        if self._points is not None:
            with contextlib.suppress(Exception):
                rows = self._points.ledger(user_id, limit=5)
        if rows:
            lines.append("\n<b>最近流水</b>")
            for row in rows:
                delta = int(row.get("delta") or 0)
                when = time.strftime("%m-%d %H:%M",
                                     time.localtime(row.get("created_at") or 0))
                sign = "+" if delta > 0 else ""
                lines.append(
                    f"{when} · {row.get('reason_label') or row.get('reason')} "
                    f"· <b>{sign}{delta}</b>")
        else:
            lines.append("\n还没有积分记录。")
        return "\n".join(lines)

    async def _checkin(self, chat_id: Any, message_id: int,
                       member: dict[str, Any]) -> None:
        plugin = self._plugin("checkin")
        if plugin is None or not self._plugin_on("checkin"):
            await self._edit(chat_id, message_id, "签到功能未开启。",
                             self.member_menu())
            return
        user_id = str(member.get("emby_user_id") or "")
        try:
            result = plugin.checkin(user_id)
        except Exception as exc:  # noqa: BLE001 - shown to a member
            await self._edit(chat_id, message_id, f"❌ 签到失败：{exc}",
                             self.member_menu())
            return
        if not result.get("ok"):
            await self._edit(
                chat_id, message_id,
                f"📅 {result.get('reason') or '今天已签到'}\n\n"
                f"当前余额：<b>{result.get('balance', self._balance(user_id))}</b>",
                self.member_menu())
            return
        bonus = int(result.get("bonus") or 0)
        extra = f"（含连签奖励 +{bonus}）" if bonus else ""
        await self._edit(
            chat_id, message_id,
            f"✅ <b>签到成功</b>\n\n获得积分：<b>+{result.get('points')}</b>{extra}\n"
            f"连续签到：<b>{result.get('streak')}</b> 天\n"
            f"当前余额：<b>{result.get('balance')}</b>",
            self.member_menu())

    async def _nodes_text(self) -> str:
        """Which line a member would be served from, and how busy it is.

        Utilisation is shown as a percentage rather than stream counts: the
        capacity of a node is an operator concept, and '3 streams' means
        nothing without it.
        """
        if self._scheduler is None:
            return "📡 <b>线路</b>\n\n暂无线路信息。"
        nodes: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            nodes = self._scheduler.snapshot()
        if not nodes:
            return "📡 <b>线路</b>\n\n暂无线路信息。"
        lines = ["📡 <b>线路</b>\n"]
        for node in nodes:
            percent = round(float(node.get("utilisation") or 0) * 100)
            if not node.get("enabled", True) or node.get("manually_disabled"):
                mark = "⛔ 维护中"
            elif not node.get("ok", True):
                mark = "⚠️ 不可用"
            elif percent >= 90:
                mark = f"🔴 {percent}%"
            elif percent >= 60:
                mark = f"🟡 {percent}%"
            else:
                mark = f"🟢 {percent}%"
            lines.append(f"{node.get('name') or '-'} · {mark}")
        lines.append("\n<i>水位越低越空闲，系统会自动为你选择线路。</i>")
        return "\n".join(lines)

    # -- shop -----------------------------------------------------------------

    async def _shop_view(self, chat_id: Any, message_id: int,
                         member: dict[str, Any]) -> None:
        if self._shop is None:
            await self._edit(chat_id, message_id, "商城暂未开放。", self.bag_menu())
            return
        items: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            items = self._shop.items(enabled_only=True)
        balance = self._balance(str(member.get("emby_user_id") or ""))
        if not items:
            await self._edit(
                chat_id, message_id,
                f"🎁 <b>兑换商城</b>\n\n当前余额：<b>{balance}</b>\n\n"
                "暂时没有上架的商品。", self.bag_menu())
            return
        lines = [f"🎁 <b>兑换商城</b>\n\n当前余额：<b>{balance}</b>\n"]
        keyboard: list[list[dict[str, str]]] = []
        for item in items:
            lines.append(
                f"· <b>{item['name']}</b> · {item['cost']} 分"
                + (f"\n  {item['description']}" if item.get("description") else ""))
            keyboard.append([{
                "text": f"{item['name']} · 消耗 {item['cost']} 分",
                "callback_data": f"buy:{item['id']}",
            }])
        keyboard.append([{"text": "◀ 返回", "callback_data": "bag"}])
        await self._edit(chat_id, message_id, "\n".join(lines), keyboard)

    async def _shop_confirm(self, chat_id: Any, message_id: int,
                            item_id: str) -> None:
        """Ask before spending. Points are earned slowly and spent in one tap."""
        item = None
        if self._shop is not None:
            with contextlib.suppress(Exception):
                item = self._shop.get(int(item_id))
        if not item or not item.get("enabled"):
            await self._edit(chat_id, message_id, "该商品已下架。", self.bag_menu())
            return
        await self._edit(
            chat_id, message_id,
            f"确定用 <b>{item['cost']}</b> 积分兑换 <b>{item['name']}</b>？\n\n"
            f"内容：{item['amount']}{item.get('unit') or ''} · "
            f"{item.get('kind_label') or ''}",
            [[{"text": "✅ 确认兑换", "callback_data": f"buyok:{item['id']}"},
              {"text": "取消", "callback_data": "shop"}]])

    async def _shop_redeem(self, chat_id: Any, message_id: int,
                           member: dict[str, Any], item_id: str) -> None:
        if self._shop is None:
            await self._edit(chat_id, message_id, "商城暂未开放。", self.bag_menu())
            return
        try:
            result = self._shop.redeem(
                str(member.get("emby_user_id") or ""), int(item_id),
                actor="telegram")
        except Exception as exc:  # noqa: BLE001 - the reason is for the member
            await self._edit(chat_id, message_id, f"❌ 兑换失败：{exc}",
                             self.bag_menu())
            return
        item = result.get("item") or {}
        await self._edit(
            chat_id, message_id,
            f"✅ <b>兑换成功</b>\n\n商品：{item.get('name')}\n"
            f"发放：{result.get('granted')}\n"
            f"消耗：{result.get('cost')} 分\n"
            f"余额：<b>{result.get('balance')}</b>",
            self.bag_menu())

    async def _orders_view(self, chat_id: Any, message_id: int,
                           member: dict[str, Any]) -> None:
        rows: list[dict[str, Any]] = []
        if self._shop is not None:
            with contextlib.suppress(Exception):
                rows = self._shop.orders(
                    user_id=str(member.get("emby_user_id") or ""), limit=10)
        if not rows:
            await self._edit(chat_id, message_id,
                             "📜 <b>兑换记录</b>\n\n你还没有兑换过任何商品。",
                             self.bag_menu())
            return
        lines = ["📜 <b>兑换记录</b>\n"]
        for row in rows:
            when = time.strftime("%m-%d %H:%M",
                                 time.localtime(row.get("created_at") or 0))
            lines.append(
                f"{when} · {row.get('item_name') or '-'} · -{row.get('cost')} 分")
        await self._edit(chat_id, message_id, "\n".join(lines), self.bag_menu())

    # -- transfer -------------------------------------------------------------

    async def _transfer_start(self, chat_id: Any, message_id: int) -> None:
        if not self._plugin_on("points_transfer"):
            await self._edit(chat_id, message_id, "转账功能未开启。",
                             self.member_menu())
            return
        self._pending[str(chat_id)] = (
            "transfer_to", time.time() + PENDING_TTL, {})
        await self._edit(
            chat_id, message_id,
            "💸 <b>积分转账</b>\n\n请发送对方的 <b>Emby 用户名</b>。\n\n"
            "<i>10 分钟内有效，发送 /start 可取消。</i>")

    async def _transfer_pick_target(self, chat_id: Any,
                                    member: dict[str, Any],
                                    username: str) -> None:
        target = None
        with contextlib.suppress(Exception):
            target = self._members.find_by_username(username.strip())
        if not target:
            await self._show(chat_id, f"❌ 找不到用户「{username}」，请确认后重试。",
                             BACK_HOME)
            return
        if str(target.get("emby_user_id")) == str(member.get("emby_user_id")):
            await self._show(chat_id, "❌ 不能转给自己。", BACK_HOME)
            return
        self._pending[str(chat_id)] = (
            "transfer_amount", time.time() + PENDING_TTL,
            {"to_id": str(target.get("emby_user_id")),
             "to_name": str(target.get("username") or username)})
        balance = self._balance(str(member.get("emby_user_id") or ""))
        await self._show(
            chat_id,
            f"收款人：<b>{target.get('username') or username}</b>\n"
            f"你的余额：<b>{balance}</b>\n\n请发送要转多少积分。\n\n"
            "<i>发送 /start 可取消。</i>",
            BACK_HOME)

    async def _transfer_pick_amount(self, chat_id: Any,
                                    member: dict[str, Any],
                                    extra: dict[str, Any], raw: str) -> None:
        plugin = self._plugin("points_transfer")
        if plugin is None:
            self._pending.pop(str(chat_id), None)
            await self._show(chat_id, "转账功能未开启。",
                             self._with_admin_row(self.member_menu(), member))
            return
        try:
            amount = int(str(raw).strip())
        except ValueError:
            await self._show(chat_id, "请输入一个正整数，例如 <code>50</code>。",
                             BACK_HOME)
            return
        ok, reason = plugin.can_transfer(
            str(member.get("emby_user_id") or ""), amount)
        if not ok:
            await self._show(chat_id, f"❌ {reason}", BACK_HOME)
            return
        fee = plugin.fee_for(amount)
        self._pending[str(chat_id)] = (
            "transfer_confirm", time.time() + PENDING_TTL,
            {**extra, "amount": amount})
        fee_line = f"\n手续费：{fee}（对方到账 {amount - fee}）" if fee else ""
        await self._show(
            chat_id,
            f"请确认转账：\n\n收款人：<b>{extra.get('to_name')}</b>\n"
            f"数量：<b>{amount}</b>{fee_line}",
            [[{"text": "✅ 确认转账", "callback_data": "transfer_ok"},
              {"text": "取消", "callback_data": "home"}]])

    async def _transfer_execute(self, chat_id: Any, message_id: int,
                                member: dict[str, Any]) -> None:
        waiting = self._pending.pop(str(chat_id), None)
        plugin = self._plugin("points_transfer")
        if not waiting or waiting[0] != "transfer_confirm" or plugin is None:
            await self._edit(chat_id, message_id, "转账已取消或超时，请重新发起。",
                             self.member_menu())
            return
        extra = waiting[2]
        to_id = str(extra.get("to_id") or "")
        try:
            result = plugin.transfer(
                str(member.get("emby_user_id") or ""), to_id,
                int(extra.get("amount") or 0))
        except Exception as exc:  # noqa: BLE001 - the reason is for the member
            await self._edit(chat_id, message_id, f"❌ 转账失败：{exc}",
                             self.member_menu())
            return
        await self._edit(
            chat_id, message_id,
            f"✅ <b>转账成功</b>\n\n收款人：{extra.get('to_name')}\n"
            f"转出：<b>{result.get('amount')}</b>"
            + (f"（手续费 {result.get('fee')}）" if result.get("fee") else "")
            + f"\n对方到账：<b>{result.get('received')}</b>\n"
            f"你的余额：<b>{result.get('from_balance')}</b>",
            self.member_menu())
        # Telling the recipient is the difference between a transfer and a
        # number quietly changing. Best effort: a failed notification must not
        # undo a transfer that already committed.
        with contextlib.suppress(Exception):
            target = self._members.get(to_id)
            if target and target.get("tg_user_id"):
                await self.send(
                    str(target["tg_user_id"]),
                    f"💰 收到 <b>{member.get('username') or '一位成员'}</b> "
                    f"转来的 <b>{result.get('received')}</b> 积分\n"
                    f"当前余额：<b>{result.get('to_balance')}</b>")

    # -- claim / rebind requests ---------------------------------------------

    def _create_request(self, kind: str, tg_user_id: str, tg_username: str,
                        wanted: str) -> bool:
        if self._db is None or kind not in REQUEST_KINDS:
            return False
        existing = self._db.one(
            "SELECT 1 AS x FROM tg_requests WHERE tg_user_id=? AND status='pending'",
            (str(tg_user_id),))
        if existing:
            return False
        self._db.execute(
            "INSERT INTO tg_requests"
            "(kind,tg_user_id,tg_username,wanted_username,status,created_at) "
            "VALUES(?,?,?,?, 'pending', ?)",
            (kind, str(tg_user_id), tg_username, wanted, int(time.time())))
        return True

    def pending_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        return self._db.query(
            "SELECT * FROM tg_requests WHERE status='pending' "
            "ORDER BY created_at ASC LIMIT ?", (max(1, min(limit, 500)),))

    def review_request(self, request_id: int, approve: bool,
                       reviewer: str = "operator") -> dict[str, Any]:
        """Approve or reject. Approving is what actually moves the linkage."""
        if self._db is None:
            raise KeyError(request_id)
        row = self._db.one("SELECT * FROM tg_requests WHERE id=?", (request_id,))
        if not row or row.get("status") != "pending":
            raise KeyError(request_id)

        if approve:
            member = self._members.find_by_username(row["wanted_username"])
            if not member:
                self._db.execute(
                    "UPDATE tg_requests SET status='rejected',reviewed_at=?,"
                    "reviewed_by=?,note=? WHERE id=?",
                    (int(time.time()), reviewer, "找不到该账号", request_id))
                raise ValueError(f"找不到账号: {row['wanted_username']}")
            self._members.bind_telegram(
                member["emby_user_id"], row["tg_user_id"],
                row.get("tg_username") or "", actor=reviewer)

        self._db.execute(
            "UPDATE tg_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?",
            ("approved" if approve else "rejected", int(time.time()),
             reviewer, request_id))
        return {"id": request_id, "approved": approve,
                "tg_user_id": row["tg_user_id"]}

    # -- rankings -------------------------------------------------------------

    def _rankings_text(self, days: int = 1) -> str:
        window = "今日" if days <= 1 else f"近 {days} 天"
        lines = [f"🏆 <b>{window}排行</b>\n"]
        # Watch rankings and the points ranking come from different services,
        # so one being unavailable must not hide the other: a panel with no
        # playback stats still has a points economy worth showing.
        if self._stats is not None:
            with contextlib.suppress(Exception):
                users = self._stats.top_users(days=days, limit=5)
                if users:
                    lines.append("<b>观看时长</b>")
                    for i, u in enumerate(users, 1):
                        lines.append(
                            f"{i}. {u['username']} · {u['hours']} 小时 · "
                            f"{u['plays']} 次")
                    lines.append("")
            with contextlib.suppress(Exception):
                titles = self._stats.top_titles(days=days, limit=5)
                if titles:
                    lines.append("<b>热门影片</b>")
                    for i, t in enumerate(titles, 1):
                        lines.append(
                            f"{i}. {t['title']} · {t['plays']} 次 · "
                            f"{t['hours']} 小时")
                    lines.append("")
        # Points are a different kind of ranking -- earned rather than watched
        # -- so it is a separate section, and it is only shown once someone has
        # actually earned something.
        if self._points is not None:
            with contextlib.suppress(Exception):
                rich = self._points.top(limit=5)
                if rich:
                    lines.append("<b>积分排行</b>")
                    for i, r in enumerate(rich, 1):
                        lines.append(
                            f"{i}. {r.get('username') or '-'} · "
                            f"{int(r.get('balance') or 0)} 分")
        while lines and not lines[-1]:
            lines.pop()
        if len(lines) == 1:
            lines.append("暂时还没有排行数据。")
        return "\n".join(lines)

    # -- media requests -------------------------------------------------------

    def _requests_ready(self) -> bool:
        return self._requests is not None

    @staticmethod
    def _remaining_text(left: int | None) -> str:
        return "不限" if left is None else f"{left} 次"

    async def _request_start(self, chat_id: Any, message_id: int,
                             member: dict[str, Any]) -> None:
        """Show the allowance, then wait for a link.

        The remaining count is shown *before* asking rather than after they
        have gone and found a link: being told the quota is spent only once
        the work is done is the annoying version of this feature.
        """
        if not self._requests_ready():
            await self._edit(chat_id, message_id, "求片功能暂未开启。",
                             self.member_menu())
            return
        user_id = str(member.get("emby_user_id"))
        left = self._requests.remaining(user_id)
        if left is not None and left <= 0:
            await self._edit(
                chat_id, message_id,
                "🎬 <b>求片</b>\n\n本月的求片次数已经用完了，下个月 1 号恢复。",
                self.member_menu())
            return
        self._pending[str(chat_id)] = ("request_link", time.time() + PENDING_TTL, {})
        await self._edit(
            chat_id, message_id,
            "🎬 <b>求片</b>\n\n"
            f"本月还可以求 <b>{self._remaining_text(left)}</b>。\n\n"
            "请发送 <b>TMDB 链接或编号</b>，例如：\n"
            "<code>https://www.themoviedb.org/movie/550</code>\n"
            "<code>550</code>\n\n"
            "<i>在 themoviedb.org 搜到片子后，直接复制地址栏链接即可。</i>",
            BACK_HOME)

    async def _request_pick_title(self, chat_id: Any, member: dict[str, Any],
                                  text: str) -> None:
        """Resolve what they sent and ask them to confirm the actual title.

        The confirmation exists because a wrong id is invisible otherwise: the
        member would find out an uploader spent an evening on the wrong film.
        """
        parsed = parse_link(text) if parse_link else None
        if not parsed:
            await self._show(
                chat_id,
                "❌ 没能识别这个链接。\n\n"
                "请发送 TMDB 的链接或纯数字编号，例如：\n"
                "<code>https://www.themoviedb.org/movie/550</code>\n"
                "<code>550</code>\n\n"
                "<i>发送 /start 可取消。</i>",
                BACK_HOME)
            return

        media_type, tmdb_id = parsed
        meta = None
        if self._tmdb is not None:
            media_type, meta = await self._tmdb.resolve(media_type, tmdb_id)

        extra = {"media_type": media_type, "tmdb_id": tmdb_id}
        self._pending[str(chat_id)] = (
            "request_confirm", time.time() + PENDING_TTL, extra)
        keyboard = [[{"text": "✅ 确认求片", "callback_data": "req_ok"},
                     {"text": "✖ 取消", "callback_data": "home"}]]

        if meta:
            year = meta.get("year")
            poster = poster_url(str(meta.get("poster_path") or ""))
            caption = (
                "🎬 <b>确认求片</b>\n\n"
                f"<b>{meta.get('title') or tmdb_id}</b>"
                f"{f' ({year})' if year else ''}\n"
                f"类型：{'剧集' if media_type == 'tv' else '电影'}\n"
                f"TMDB：<code>{tmdb_id}</code>\n")
            if poster:
                caption += f"海报：{poster}\n"
            caption += "\n确认要求这部片子吗？"
            await self._show(chat_id, caption, keyboard)
            return

        # No key, or TMDB had no answer. The id is still valid, so the request
        # proceeds under a placeholder rather than being refused.
        await self._show(
            chat_id,
            "🎬 <b>确认求片</b>\n\n"
            f"TMDB 编号：<code>{tmdb_id}</code>\n"
            f"类型：{'剧集' if media_type == 'tv' else '电影'}\n\n"
            "<i>暂时查不到片名，上片员会按编号处理。</i>\n\n"
            "确认要求这部片子吗？",
            keyboard)

    async def _request_submit(self, chat_id: Any, message_id: int,
                              member: dict[str, Any]) -> None:
        waiting = self._pending.pop(str(chat_id), None)
        if not waiting or waiting[0] != "request_confirm":
            await self._edit(chat_id, message_id, "这条求片会话已经过期了，请重新开始。",
                             self._with_admin_row(self.member_menu(), member))
            return
        extra = waiting[2]
        user_id = str(member.get("emby_user_id"))
        try:
            request = await self._requests.create(
                user_id, extra.get("media_type", "movie"),
                int(extra.get("tmdb_id") or 0))
        except RequestError as exc:
            await self._show(chat_id, f"❌ {exc}",
                             self._with_admin_row(self.member_menu(), member))
            return

        left = self._requests.remaining(user_id)
        await self._show(
            chat_id,
            "✅ <b>已提交</b>\n\n"
            f"编号 <b>#{request['id']}</b> · {request['display_title']}\n"
            f"本月还可以求 {self._remaining_text(left)}。\n\n"
            "上片员处理后会通知你。",
            self._with_admin_row(self.member_menu(), member))
        await self.announce_request(request)

    async def _my_requests(self, chat_id: Any, message_id: int,
                           member: dict[str, Any]) -> None:
        if not self._requests_ready():
            await self._edit(chat_id, message_id, "求片功能暂未开启。",
                             self.info_menu())
            return
        user_id = str(member.get("emby_user_id"))
        rows = self._requests.for_user(user_id, limit=5)
        left = self._requests.remaining(user_id)
        if not rows:
            body = ("📋 <b>我的求片</b>\n\n还没有求过片。\n\n"
                    f"本月可求 {self._remaining_text(left)}。")
        else:
            lines = ["📋 <b>我的求片</b>\n"]
            for row in rows:
                mark = REQUEST_STATUS_ICONS.get(str(row.get("status")), "·")
                lines.append(
                    f"{mark} #{row['id']} {row['display_title']} · "
                    f"{row['status_label']}")
                note = str(row.get("result_note") or "")
                if note and row.get("status") == "rejected":
                    lines.append(f"    <i>{note}</i>")
            lines.append(f"\n本月还可以求 {self._remaining_text(left)}。")
            body = "\n".join(lines)
        await self._edit(chat_id, message_id, body, self.info_menu())

    # -- uploader fan-out ----------------------------------------------------

    async def announce_request(self, request: dict[str, Any]) -> int:
        """Tell every reachable uploader, and remember which message is whose.

        One message each rather than one group post: the claim button has to
        be taken back from the people who did not win, and that is only
        possible per chat.
        """
        if not self._requests_ready() or not self.enabled:
            return 0
        request_id = int(request.get("id") or 0)
        keyboard = [[{"text": "✋ 接单",
                      "callback_data": f"req_claim:{request_id}"}]]
        body = (
            f"🎬 <b>新求片 #{request_id}</b>\n\n"
            f"{request.get('display_title') or ''}\n"
            f"类型：{request.get('media_label') or '-'}\n"
            f"TMDB：<code>{request.get('tmdb_id')}</code>\n"
            f"求片人：{request.get('username') or '-'}")
        note = str(request.get("note") or "")
        if note:
            body += f"\n备注：{note}"

        sent = 0
        for uploader in self._requests.uploaders():
            chat_id = str(uploader.get("tg_user_id") or "")
            if not chat_id:
                continue
            message_id = await self.send_message(chat_id, body, keyboard)
            if message_id is None:
                continue
            self._requests.record_notice(request_id, chat_id, int(message_id))
            sent += 1
        return sent

    async def _request_claim(self, chat_id: Any, message_id: int,
                             callback_id: str, member: dict[str, Any],
                             request_id: int) -> None:
        """One uploader takes the job; everyone else loses the button."""
        if not self._requests_ready():
            return
        if not member:
            await self._answer_callback(callback_id, "这个 Telegram 还没有账号")
            return
        roles = member.get("roles") or []
        if "uploader" not in roles and "admin" not in roles:
            await self._answer_callback(callback_id, "你不是上片员")
            return

        user_id = str(member.get("emby_user_id"))
        try:
            result = self._requests.claim(request_id, user_id)
        except RequestError as exc:
            await self._answer_callback(callback_id, str(exc))
            return

        if not result.get("ok"):
            holder = result.get("claimed_by_name") or "其他上片员"
            await self._answer_callback(callback_id, f"已被 {holder} 接单")
            await self._retract_notices(request_id, holder,
                                        skip_chat=str(chat_id))
            return

        await self._answer_callback(callback_id, "接单成功")
        request = result["request"]
        await self._edit(
            chat_id, message_id,
            f"✋ <b>已接单 #{request_id}</b>\n\n"
            f"{request.get('display_title') or ''}\n"
            f"TMDB：<code>{request.get('tmdb_id')}</code>\n"
            f"求片人：{request.get('username') or '-'}\n\n"
            "处理完成后点下方按钮。",
            [[{"text": "✅ 已处理", "callback_data": f"req_done:{request_id}"},
              {"text": "❌ 无法处理", "callback_data": f"req_fail:{request_id}"}]])
        await self._retract_notices(
            request_id, str(member.get("username") or "上片员"),
            skip_chat=str(chat_id))

    async def _retract_notices(self, request_id: int, holder: str,
                               skip_chat: str = "") -> int:
        """Strip the claim button from every other uploader's message.

        Leaving a live button on a job that is gone is how two people end up
        downloading the same title.
        """
        if not self._requests_ready():
            return 0
        edited = 0
        for notice in self._requests.notices(request_id):
            chat_id = str(notice.get("tg_user_id") or "")
            if not chat_id or chat_id == str(skip_chat):
                continue
            await self._edit(
                chat_id, int(notice.get("message_id") or 0),
                f"🎬 <b>求片 #{request_id}</b>\n\n已由 {holder} 接单。")
            edited += 1
        return edited

    async def announce_request_claimed(self, request_id: int,
                                       holder_id: str) -> int:
        """Panel-side claim: take the button back from every uploader chat."""
        if not self._requests_ready():
            return 0
        holder = self._members.get(str(holder_id)) or {}
        name = str(holder.get("username") or "管理员")
        return await self._retract_notices(request_id, name)

    async def _request_resolve(self, chat_id: Any, message_id: int,
                               callback_id: str, member: dict[str, Any],
                               request_id: int, done: bool) -> None:
        if not self._requests_ready():
            return
        if not done:
            await self._answer_callback(callback_id)
            # A refusal without a reason is worse than no answer: the member
            # cannot tell whether to ask again differently.
            self._pending[str(chat_id)] = (
                "request_reason", time.time() + PENDING_TTL,
                {"request_id": request_id})
            await self._edit(
                chat_id, message_id,
                f"❌ <b>无法处理 #{request_id}</b>\n\n请发送一句原因，会转达给求片人。")
            return
        try:
            result = self._requests.resolve(
                request_id, str(member.get("emby_user_id")), done=True)
        except RequestError as exc:
            await self._answer_callback(callback_id, str(exc))
            return
        await self._answer_callback(callback_id, "已标记完成")
        request = result["request"]
        await self._edit(
            chat_id, message_id,
            f"✅ <b>已完成 #{request_id}</b>\n\n{request.get('display_title') or ''}")
        await self.notify_request_resolved(request)

    async def _request_reason(self, chat_id: Any, member: dict[str, Any],
                              extra: dict[str, Any], text: str) -> None:
        self._pending.pop(str(chat_id), None)
        request_id = int(extra.get("request_id") or 0)
        try:
            result = self._requests.resolve(
                request_id, str(member.get("emby_user_id")), done=False,
                note=text)
        except RequestError as exc:
            await self.send(chat_id, f"❌ {exc}", self.member_menu())
            return
        request = result["request"]
        await self.send(
            chat_id,
            f"已标记 #{request_id} 为无法处理，原因已转达求片人。",
            self.member_menu())
        await self.notify_request_resolved(request)

    async def notify_request_resolved(self, request: dict[str, Any]) -> bool:
        """Tell the member what happened to the title they asked for."""
        chat_id = str(request.get("tg_user_id") or "")
        if not chat_id or not self.enabled:
            return False
        title = request.get("display_title") or f"#{request.get('tmdb_id')}"
        if str(request.get("status")) == "done":
            body = (f"✅ <b>求片已处理</b>\n\n你求的《{title}》已经处理好了，"
                    "请耐心等待入库。")
            note = str(request.get("result_note") or "")
            if note:
                body += f"\n\n<i>{note}</i>"
        else:
            reason = str(request.get("result_note") or "暂时无法处理")
            body = (f"❌ <b>求片无法处理</b>\n\n你求的《{title}》暂时没能处理。\n\n"
                    f"原因：{reason}")
        return await self.send(chat_id, body)

    # -- admin commands -------------------------------------------------------

    def is_admin(self, member: dict[str, Any] | None) -> bool:
        """Admin is a role on a linked account, not a separate id list.

        The operator already grants 'admin' in the panel to let someone log
        in; a second list of privileged Telegram ids would be one more thing
        to keep in sync, and the two would eventually disagree about who is
        an admin.
        """
        return bool(member) and "admin" in (member.get("roles") or [])

    def admin_menu(self) -> list[list[dict[str, str]]]:
        """What embyboss put on the 管理 panel, minus the slash-command memory."""
        return [
            [{"text": "🔍 查找用户", "callback_data": "admin_find"}],
            [{"text": "📋 求片队列", "callback_data": "admin_reqs"},
             {"text": "🎟 生成卡密", "callback_data": "admin_code"}],
            [{"text": "✅ 预授权注册", "callback_data": "admin_auth"}],
            [{"text": "◀ 返回", "callback_data": "home"}],
        ]

    def _user_admin_keyboard(self, user_id: str) -> list[list[dict[str, str]]]:
        return [
            [{"text": "⏳ +30 天", "callback_data": "admin_renew:30"},
             {"text": "⏳ 自定义续期", "callback_data": "admin_renew"}],
            [{"text": "⭐ 移入白名单", "callback_data": "admin_pro"},
             {"text": "↩ 移回默认组", "callback_data": "admin_rev"}],
            [{"text": "💰 加减积分", "callback_data": "admin_score"},
             {"text": "🗑 删除", "callback_data": "admin_rm"}],
            [{"text": "◀ 返回管理", "callback_data": "admin"}],
        ]

    def _user_card(self, target: dict[str, Any]) -> str:
        user_id = str(target.get("emby_user_id"))
        devices = len(self._members.devices(user_id) or [])
        seen = target.get("last_seen_at")
        invitee_count = target.get("invitee_count")
        if invitee_count is None:
            invitee_count = len(self._members.invitees_of(user_id) or [])
        inviter = self._members.inviter_of(user_id) or {}
        left = (self._requests.remaining(user_id)
                if self._requests_ready() else None)
        return (
            f"👤 <b>{target.get('username') or '-'}</b>\n\n"
            f"状态：{self._status_label(target)}\n"
            f"用户组：{target.get('group_name') or '-'}\n"
            f"有效期：{_fmt_expiry(target.get('expires_at'))}\n"
            f"积分：{self._balance(user_id)}\n"
            f"注册渠道：{target.get('register_via') or 'legacy'}\n"
            f"邀请人：{inviter.get('username') or '—'}\n"
            f"下级：{invitee_count} 人\n"
            f"Telegram：{target.get('tg_user_id') or '未关联'}\n"
            f"设备数：{devices}\n"
            f"最近活跃："
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(seen)) if seen else '—'}\n"
            f"求片剩余：{self._remaining_text(left)}"
        )

    def _hold_admin_user(self, chat_id: Any, target: dict[str, Any],
                         actor: str) -> None:
        self._pending[str(chat_id)] = (
            "admin_user", time.time() + PENDING_TTL,
            {"user_id": str(target.get("emby_user_id")),
             "username": str(target.get("username") or ""),
             "actor": actor})

    async def _admin_home(self, chat_id: Any, message_id: int,
                          member: dict[str, Any] | None) -> None:
        if not self.is_admin(member):
            await self._edit(chat_id, message_id, "⛔ 无权限。")
            return
        self._pending.pop(str(chat_id), None)
        await self._edit(
            chat_id, message_id,
            "🛠 <b>管理</b>\n\n"
            "查找用户后，续期、换组、加减积分和删除都在同一条消息里完成。",
            self.admin_menu())

    async def _admin_prompt_find(self, chat_id: Any, message_id: int,
                                 member: dict[str, Any] | None) -> None:
        if not self.is_admin(member):
            await self._edit(chat_id, message_id, "⛔ 无权限。")
            return
        self._pending[str(chat_id)] = ("admin_find", time.time() + PENDING_TTL, {})
        await self._edit(
            chat_id, message_id,
            "🔍 <b>查找用户</b>\n\n请发送 Emby 用户名或 @Telegram 用户名。\n\n"
            "<i>发送 /start 可取消。</i>",
            [[{"text": "◀ 返回管理", "callback_data": "admin"}]])

    async def _admin_show_user(self, chat_id: Any, target: dict[str, Any],
                               actor: str) -> None:
        self._hold_admin_user(chat_id, target, actor)
        await self._show(chat_id, self._user_card(target),
                         self._user_admin_keyboard(str(target.get("emby_user_id"))))

    def _admin_held_target(self, chat_id: Any) -> tuple[dict[str, Any] | None, str]:
        waiting = self._pending.get(str(chat_id))
        if not waiting or waiting[0] != "admin_user":
            return None, ""
        extra = waiting[2] or {}
        target = self._members.get(str(extra.get("user_id") or ""))
        return target, str(extra.get("actor") or "tg:admin")

    async def _admin_handle_text(self, chat_id: Any, member: dict[str, Any] | None,
                                 kind: str, extra: dict[str, Any], text: str) -> None:
        if not self.is_admin(member):
            self._pending.pop(str(chat_id), None)
            await self._show(chat_id, "⛔ 无权限。")
            return
        actor = self._admin_actor(member or {}, "")
        if kind == "admin_find":
            target = self._find_target(text)
            if not target:
                await self._show(
                    chat_id, "找不到该用户。请重新发送用户名。",
                    [[{"text": "◀ 返回管理", "callback_data": "admin"}]])
                return
            await self._admin_show_user(chat_id, target, actor)
            return
        if kind == "admin_renew_days":
            if not text.lstrip("-").isdigit():
                await self._show(chat_id, "请发送天数，例如 <code>30</code>。",
                                 [[{"text": "◀ 返回", "callback_data": "admin"}]])
                return
            target = self._members.get(str(extra.get("user_id") or ""))
            if not target:
                await self._show(chat_id, "这个用户已经不在了。", self.admin_menu())
                return
            updated = self._members.renew(str(target.get("emby_user_id")),
                                          int(text), actor=actor)
            await self._admin_show_user(chat_id, updated or target, actor)
            return
        if kind == "admin_score_delta":
            raw = text.lstrip("+")
            if not raw.lstrip("-").isdigit():
                await self._show(chat_id, "请发送整数，例如 <code>+50</code> 或 <code>-10</code>。",
                                 [[{"text": "◀ 返回", "callback_data": "admin"}]])
                return
            target = self._members.get(str(extra.get("user_id") or ""))
            if not target or self._points is None:
                await self._show(chat_id, "无法调整积分。", self.admin_menu())
                return
            try:
                self._points.add(str(target.get("emby_user_id")), int(raw),
                                 "admin.adjust", ref="tg", actor=actor)
            except ValueError as exc:
                await self._show(chat_id, f"❌ {exc}",
                                 [[{"text": "◀ 返回", "callback_data": "admin"}]])
                return
            fresh = self._members.get(str(target.get("emby_user_id"))) or target
            await self._admin_show_user(chat_id, fresh, actor)
            return
        if kind == "admin_code":
            parts = text.split()
            await self._cmd_code(chat_id, actor, parts)
            return
        if kind == "admin_auth":
            await self._cmd_auth(chat_id, actor, [text.strip()])

    async def _admin_user_action(self, chat_id: Any, message_id: int,
                                 member: dict[str, Any] | None, data: str) -> None:
        if not self.is_admin(member):
            await self._edit(chat_id, message_id, "⛔ 无权限。")
            return
        target, actor = self._admin_held_target(chat_id)
        if not target:
            await self._edit(
                chat_id, message_id,
                "请先查找一个用户。", self.admin_menu())
            return
        actor = actor or self._admin_actor(member or {}, "")
        user_id = str(target.get("emby_user_id"))
        if data == "admin_renew" or data.startswith("admin_renew:"):
            _, _, days_raw = data.partition(":")
            if days_raw.lstrip("-").isdigit():
                try:
                    updated = self._members.renew(user_id, int(days_raw), actor=actor)
                except Exception as exc:  # noqa: BLE001
                    await self._edit(chat_id, message_id, f"❌ {exc}",
                                     self._user_admin_keyboard(user_id))
                    return
                await self._admin_show_user(chat_id, updated or target, actor)
                return
            self._pending[str(chat_id)] = (
                "admin_renew_days", time.time() + PENDING_TTL,
                {"user_id": user_id, "actor": actor})
            await self._edit(
                chat_id, message_id,
                f"⏳ 给 <b>{target.get('username')}</b> 续期多少天？\n请发送数字。",
                [[{"text": "◀ 返回", "callback_data": "admin"}]])
            return
        if data == "admin_pro":
            if self._groups is not None:
                with contextlib.suppress(Exception):
                    self._groups.ensure_whitelist()
            self._members.upsert(user_id, str(target.get("username") or ""),
                                 {"group_id": WHITELIST_GROUP_ID}, actor=actor)
            fresh = self._members.get(user_id) or target
            await self._admin_show_user(chat_id, fresh, actor)
            return
        if data == "admin_rev":
            default_id = self._groups.default_group_id() if self._groups else None
            if not default_id:
                await self._edit(chat_id, message_id, "没有设置默认用户组。",
                                 self._user_admin_keyboard(user_id))
                return
            self._members.upsert(user_id, str(target.get("username") or ""),
                                 {"group_id": default_id}, actor=actor)
            fresh = self._members.get(user_id) or target
            await self._admin_show_user(chat_id, fresh, actor)
            return
        if data == "admin_score":
            self._pending[str(chat_id)] = (
                "admin_score_delta", time.time() + PENDING_TTL,
                {"user_id": user_id, "actor": actor})
            await self._edit(
                chat_id, message_id,
                f"💰 给 <b>{target.get('username')}</b> 加减多少积分？\n"
                "例如 <code>+50</code> 或 <code>-10</code>。",
                [[{"text": "◀ 返回", "callback_data": "admin"}]])
            return
        if data == "admin_rm":
            await self._cmd_rm(chat_id, actor, [str(target.get("username") or "")])

    def _find_target(self, token: str) -> dict[str, Any] | None:
        """Resolve '@name' or a bare Emby username to a member."""
        name = str(token or "").strip().lstrip("@")
        if not name:
            return None
        found = self._members.find_by_username(name)
        if found:
            return found
        # A Telegram @handle is not the Emby login, so fall back to the link
        # table rather than telling an operator their own admin does not exist.
        for candidate in self._members.linked_telegram():
            if str(candidate.get("tg_username") or "").lower() == name.lower():
                return candidate
        return None

    def _admin_actor(self, member: dict[str, Any], tg_username: str) -> str:
        label = tg_username or member.get("username") or "admin"
        return f"tg:{label}"

    async def _open_start(self, chat_id: Any, tg_user_id: str, tg_name: str,
                           payload: str = "") -> None:
        """Home screen, or skip into registration when /start carries a code."""
        self._pending.pop(str(chat_id), None)
        token = str(payload or "").strip()
        if (token and looks_like_credential(token)
                and not self._member_for_chat(tg_user_id)):
            await self._start_registration(chat_id, tg_user_id, credential=token)
            return
        body, keyboard = self._home(tg_user_id, tg_name)
        await self._show(chat_id, body, keyboard)

    @staticmethod
    def _private_chat(message: dict[str, Any]) -> bool:
        kind = str((message.get("chat") or {}).get("type") or "private")
        return kind == "private"

    async def _handle_command(self, chat_id: Any, tg_user_id: str,
                              tg_username: str, text: str,
                              display_name: str = "") -> None:
        """Dispatch an admin '/' command.

        Every command re-checks the role: a member could have been demoted
        between two messages, and the bot must not act on the authority they
        used to have.
        """
        parts = text.split()
        command = parts[0].lower().lstrip("/")
        command = command.split("@", 1)[0]
        args = parts[1:]
        display = (display_name or tg_username or "朋友").strip() or "朋友"

        member = self._member_for_chat(tg_user_id)
        # /start is how anybody opens the bot -- it is the very first message
        # every ordinary member ever sends. Routing it through the admin gate
        # answered that message with "no permission", and admins fared no
        # better: with no _cmd_start they were told the command was unknown.
        # A payload on /start is an invite or a card; /start itself cancels
        # whatever conversation was in flight.
        if command == "start":
            payload = args[0] if args else ""
            await self._open_start(chat_id, tg_user_id, display, payload)
            return
        if command == "help" and not self.is_admin(member):
            await self.send(
                chat_id, self._help_text(member),
                self.member_menu() if member else self.guest_menu())
            return
        if not self.is_admin(member):
            await self.send(chat_id, "⛔ 无权限。")
            return

        actor = self._admin_actor(member, tg_username)
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            await self.send(chat_id, f"未知命令 /{command}，发送 /help 查看命令清单。")
            return
        try:
            await handler(chat_id, actor, args)
        except (ConfigError, ShopError, RequestError) as exc:
            await self.send(chat_id, f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            self._last_error = f"{type(exc).__name__}"
            await self.send(chat_id, f"❌ 命令执行失败：{type(exc).__name__}")

    async def _need_target(self, chat_id: Any, args: list[str]
                           ) -> dict[str, Any] | None:
        if not args:
            await self.send(chat_id, "请指定用户，例如 <code>/kk alice</code>")
            return None
        target = self._find_target(args[0])
        if not target:
            await self.send(chat_id, "找不到该用户。")
            return None
        return target

    async def _cmd_help(self, chat_id: Any, actor: str,
                        args: list[str]) -> None:
        await self.send(chat_id, ADMIN_HELP)

    async def _cmd_kk(self, chat_id: Any, actor: str,
                      args: list[str]) -> None:
        target = await self._need_target(chat_id, args)
        if not target:
            return
        self._hold_admin_user(chat_id, target, actor)
        await self.send(chat_id, self._user_card(target),
                        self._user_admin_keyboard(str(target.get("emby_user_id"))))

    async def _move_group(self, chat_id: Any, actor: str, args: list[str],
                          group_id: str, label: str) -> None:
        target = await self._need_target(chat_id, args)
        if not target:
            return
        if self._groups is not None:
            # /prouser names a specific group, so make sure it is there even
            # on a panel whose owner tidied their group list.
            with contextlib.suppress(Exception):
                self._groups.ensure_whitelist()
            if not self._groups.get(group_id):
                await self.send(chat_id, f"用户组 {group_id} 不存在。")
                return
        user_id = str(target.get("emby_user_id"))
        self._members.upsert(user_id, str(target.get("username") or ""),
                             {"group_id": group_id}, actor=actor)
        await self.send(
            chat_id,
            f"✅ <b>{target.get('username')}</b> 已移入{label}。")

    async def _cmd_prouser(self, chat_id: Any, actor: str,
                           args: list[str]) -> None:
        await self._move_group(chat_id, actor, args, WHITELIST_GROUP_ID,
                               "白名单（永不过期）")

    async def _cmd_revuser(self, chat_id: Any, actor: str,
                           args: list[str]) -> None:
        default_id = None
        if self._groups is not None:
            default_id = self._groups.default_group_id()
        if not default_id:
            await self.send(chat_id, "没有设置默认用户组。")
            return
        await self._move_group(chat_id, actor, args, default_id, "默认组")

    async def _cmd_renew(self, chat_id: Any, actor: str,
                         args: list[str]) -> None:
        target = await self._need_target(chat_id, args)
        if not target:
            return
        if len(args) < 2 or not args[1].lstrip("-").isdigit():
            await self.send(chat_id, "用法：<code>/renew 用户名 天数</code>")
            return
        days = int(args[1])
        updated = self._members.renew(str(target.get("emby_user_id")), days,
                                      actor=actor)
        await self.send(
            chat_id,
            f"✅ <b>{target.get('username')}</b> 已续期 {days} 天，"
            f"现在{_fmt_expiry(updated.get('expires_at'))}。")

    async def _cmd_renewall(self, chat_id: Any, actor: str,
                            args: list[str]) -> None:
        if not args or not args[0].lstrip("-").isdigit():
            await self.send(chat_id, "用法：<code>/renewall 天数</code>")
            return
        days = int(args[0])
        total = len(self._members.list(limit=5000))
        # Everyone at once is not undoable, so it is confirmed before it runs.
        self._pending[str(chat_id)] = (
            "admin_confirm", time.time() + PENDING_TTL,
            {"action": "renewall", "days": days, "actor": actor})
        await self.send(
            chat_id,
            f"⚠ <b>全员续期</b>\n\n将给 <b>{total}</b> 个账号各加 {days} 天。\n\n确认执行？",
            [[{"text": "✅ 确认", "callback_data": "admin_ok"},
              {"text": "✖ 取消", "callback_data": "admin_cancel"}]])

    async def _cmd_rm(self, chat_id: Any, actor: str,
                      args: list[str]) -> None:
        target = await self._need_target(chat_id, args)
        if not target:
            return
        user_id = str(target.get("emby_user_id"))
        preview = self._members.delete_preview(user_id, cascade=False)
        available = preview.get("available_cascade") or []
        lines = [
            "⚠ <b>确认删除</b>\n",
            f"账号：<b>{preview['target'].get('username') or user_id}</b>",
            f"注册渠道：{preview['target'].get('register_via') or 'legacy'}",
        ]
        if available:
            lines.append("\n连带邀请人是单独操作，不会随「只删本人」一起执行：")
            lines.extend(
                f"· {row.get('username') or row.get('emby_user_id')}"
                f"（{row.get('reason') or ''}）" for row in available)
        lines.append("\n同时会删除 Emby 账号，且<b>不可恢复</b>。")
        self._pending[str(chat_id)] = (
            "admin_confirm", time.time() + PENDING_TTL,
            {"action": "rm", "user_id": user_id, "actor": actor,
             "username": target.get("username") or user_id})
        buttons = [[{"text": "🗑 只删本人", "callback_data": "rm_self"}]]
        if available:
            buttons.append([{"text": "⚠ 连带邀请人", "callback_data": "rm_cascade"}])
        buttons.append([{"text": "✖ 取消", "callback_data": "admin_cancel"}])
        await self._show(chat_id, "\n".join(lines), buttons)

    async def _cmd_score(self, chat_id: Any, actor: str,
                         args: list[str]) -> None:
        target = await self._need_target(chat_id, args)
        if not target:
            return
        if len(args) < 2:
            await self.send(chat_id, "用法：<code>/score 用户名 ±数量</code>")
            return
        raw = args[1].lstrip("+")
        if not raw.lstrip("-").isdigit():
            await self.send(chat_id, "积分数量必须是整数。")
            return
        delta = int(raw)
        if self._points is None:
            await self.send(chat_id, "积分服务不可用。")
            return
        user_id = str(target.get("emby_user_id"))
        try:
            balance = self._points.add(user_id, delta, "admin.adjust",
                                       ref="tg", actor=actor)
        except ValueError as exc:
            await self.send(chat_id, f"❌ {exc}")
            return
        self._members.audit(actor, "points.adjust", user_id, f"delta={delta}")
        await self.send(
            chat_id,
            f"✅ <b>{target.get('username')}</b> 积分 {delta:+d}，当前 {balance}。")

    async def _cmd_scoreall(self, chat_id: Any, actor: str,
                            args: list[str]) -> None:
        if not args or not args[0].lstrip("+-").isdigit():
            await self.send(chat_id, "用法：<code>/scoreall 数量</code>")
            return
        amount = int(args[0].lstrip("+"))
        total = len(self._members.list(limit=5000))
        self._pending[str(chat_id)] = (
            "admin_confirm", time.time() + PENDING_TTL,
            {"action": "scoreall", "amount": amount, "actor": actor})
        await self.send(
            chat_id,
            f"⚠ <b>全员积分</b>\n\n将给 <b>{total}</b> 个账号各 {amount:+d} 分。\n\n确认执行？",
            [[{"text": "✅ 确认", "callback_data": "admin_ok"},
              {"text": "✖ 取消", "callback_data": "admin_cancel"}]])

    async def _cmd_gift(self, chat_id: Any, actor: str,
                        args: list[str]) -> None:
        target = await self._need_target(chat_id, args)
        if not target:
            return
        if len(args) < 3:
            await self.send(
                chat_id,
                "用法：<code>/gift 用户名 traffic|days|bandwidth|invite 数量</code>")
            return
        kind = args[1].lower()
        if not args[2].isdigit():
            await self.send(chat_id, "数量必须是正整数。")
            return
        if self._shop is None:
            await self.send(chat_id, "商城服务不可用。")
            return
        # Same write the shop performs, so a gift and a purchase cannot mean
        # two different things.
        note = self._shop.grant(str(target.get("emby_user_id")), kind,
                                int(args[2]), actor=actor)
        await self.send(chat_id, f"🎁 已发放给 <b>{target.get('username')}</b>：{note}")

    async def _cmd_code(self, chat_id: Any, actor: str,
                        args: list[str]) -> None:
        if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
            await self.send(chat_id, "用法：<code>/code 套餐id 天数 数量</code>")
            return
        if self._registration is None:
            await self.send(chat_id, "注册服务不可用。")
            return
        issued = self._registration.generate_redeem(
            args[0], int(args[1]), int(args[2]), note=f"tg:{actor}")
        self._members.audit(actor, "redeem.generate", "",
                            f"group={args[0]} days={args[1]} count={args[2]}")
        lines = [f"🎟 已生成 {len(issued)} 张卡密（{args[1]} 天）：\n"]
        lines.extend(f"<code>{row.get('code')}</code>" for row in issued)
        await self.send(chat_id, "\n".join(lines))

    async def _cmd_invite(self, chat_id: Any, actor: str,
                          args: list[str]) -> None:
        target = await self._need_target(chat_id, args)
        if not target:
            return
        if len(args) < 2 or not args[1].lstrip("-").isdigit():
            await self.send(chat_id, "用法：<code>/invite 用户名 次数</code>")
            return
        amount = int(args[1])
        user_id = str(target.get("emby_user_id"))
        self._db.execute(
            "UPDATE members SET invite_quota=MAX(0,COALESCE(invite_quota,0)+?),"
            "updated_at=? WHERE emby_user_id=?",
            (amount, int(time.time()), user_id))
        self._members.audit(actor, "member.invite_quota", user_id,
                            f"delta={amount}")
        after = self._members.get(user_id) or {}
        await self.send(
            chat_id,
            f"✅ <b>{target.get('username')}</b> 邀请名额 {amount:+d}，"
            f"当前 {after.get('invite_quota') or 0} 个。")

    async def _cmd_auth(self, chat_id: Any, actor: str,
                        args: list[str]) -> None:
        if not args:
            await self.send(chat_id, "用法：<code>/auth Telegram数字ID</code>")
            return
        if self._registration is None:
            await self.send(chat_id, "注册服务不可用。")
            return
        self._registration.grant_admin(args[0], granted_by=actor)
        self._members.audit(actor, "registration.grant", str(args[0]),
                            "admin grant via bot")
        await self.send(
            chat_id,
            f"✅ 已授权 <code>{args[0]}</code>，该 Telegram 现在可以直接注册。")

    async def _cmd_req(self, chat_id: Any, actor: str,
                       args: list[str]) -> None:
        if not self._requests_ready():
            await self._show(chat_id, "求片功能暂未开启。")
            return
        wanted = args[0].lower() if args else "active"
        if wanted not in ("open", "claimed", "active"):
            wanted = "active"
        rows = self._requests.list(status=wanted, limit=10)
        if not rows:
            await self._show(chat_id, "📋 当前没有符合条件的求片。",
                             self.admin_menu() if self._panel.get(str(chat_id)) else None)
            return
        stats = self._requests.stats()
        lines = [f"📋 <b>求片（{wanted}）</b>\n"]
        for row in rows:
            holder = row.get("claimed_by_name")
            lines.append(
                f"#{row['id']} {row['display_title']} · {row['status_label']}"
                + (f" · {holder}" if holder else "")
                + f"\n    求片人 {row.get('username') or '-'}")
        lines.append(
            f"\n待接单 {stats['open']} · 处理中 {stats['claimed']} · "
            f"本月 {stats['month_total']}")
        await self._show(chat_id, "\n".join(lines),
                         self.admin_menu() if self._panel.get(str(chat_id)) else None)

    # -- destructive confirmations -------------------------------------------

    async def _admin_confirm(self, chat_id: Any, message_id: int,
                             member: dict[str, Any]) -> None:
        """Run the action the operator just confirmed.

        Re-checks the role at execution time: the confirmation may have sat on
        screen while the person tapping it was demoted.
        """
        waiting = self._pending.pop(str(chat_id), None)
        if not waiting or waiting[0] != "admin_confirm":
            await self._edit(chat_id, message_id, "这个确认已经过期了。")
            return
        if not self.is_admin(member):
            await self._edit(chat_id, message_id, "⛔ 无权限。")
            return
        extra = waiting[2]
        action = str(extra.get("action") or "")
        actor = str(extra.get("actor") or "tg:admin")

        if action == "renewall":
            days = int(extra.get("days") or 0)
            done = 0
            for row in self._members.list(limit=5000):
                with contextlib.suppress(Exception):
                    self._members.renew(str(row.get("emby_user_id")), days,
                                        actor=actor)
                    done += 1
            await self._edit(chat_id, message_id,
                             f"✅ 已为 {done} 个账号各续期 {days} 天。")
            return

        if action == "scoreall":
            amount = int(extra.get("amount") or 0)
            done = 0
            for row in self._members.list(limit=5000):
                with contextlib.suppress(Exception):
                    self._points.add(str(row.get("emby_user_id")), amount,
                                     "admin.adjust", ref="scoreall",
                                     actor=actor)
                    done += 1
            await self._edit(chat_id, message_id,
                             f"✅ 已为 {done} 个账号各调整 {amount:+d} 分。")
            return

        if action == "rm":
            user_id = str(extra.get("user_id") or "")
            cascade = bool(extra.get("cascade"))
            from app.modules import member_ops
            preview = self._members.delete_preview(user_id, cascade=cascade)
            confirm_ids = [str(o.get("emby_user_id"))
                           for o in (preview.get("objects") or [])]
            result = await member_ops.execute_delete(
                self._members, self._emby, user_id, actor=actor,
                cascade=cascade, delete_emby=True, confirm_ids=confirm_ids)
            removed = result.get("removed") or []
            failed = result.get("emby_failed") or []
            note = ""
            if failed:
                note = "；Emby 失败 " + ", ".join(
                    f"{f.get('user_id')}: {f.get('error')}" for f in failed)
            if cascade:
                extra_n = max(0, len(removed) - 1)
                await self._edit(
                    chat_id, message_id,
                    f"🗑 已删除 <b>{extra.get('username')}</b>"
                    f"（连带 {extra_n} 个账号）{note}。")
            else:
                await self._edit(
                    chat_id, message_id,
                    f"🗑 已删除 <b>{extra.get('username')}</b>"
                    f"（仅本人）{note}。")
            return

        await self._edit(chat_id, message_id, "未知操作。")

    # -- update handling ------------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        from_user = message.get("from") or {}
        tg_user_id = str(from_user.get("id") or "")
        tg_username = str(from_user.get("username") or "")
        tg_name = from_user.get("first_name") or tg_username or "朋友"
        text = str(message.get("text") or "").strip()
        if not chat_id or not tg_user_id:
            return
        # Group chats are for broadcasts, not self-service. Answering a random
        # message there would leak someone's home screen to everyone else.
        if not self._private_chat(message):
            return

        self._sweep_pending()
        waiting = self._pending.get(str(chat_id))
        if waiting and text and not text.startswith("/"):
            kind, _, extra = waiting
            if kind == "credential":
                await self._submit_credential(chat_id, tg_user_id, text)
                return
            if kind == "username":
                await self._finish_registration(
                    chat_id, tg_user_id, tg_username, text,
                    admission=extra.get("admission"))
                return
            if kind in ("transfer_to", "transfer_amount"):
                member = self._member_for_chat(tg_user_id)
                if not member:
                    self._pending.pop(str(chat_id), None)
                    await self.send(chat_id, "这个 Telegram 还没有账号。",
                                    self.guest_menu())
                    return
                if kind == "transfer_to":
                    await self._transfer_pick_target(chat_id, member, text)
                else:
                    await self._transfer_pick_amount(chat_id, member, extra, text)
                return
            if kind in ("request_link", "request_reason"):
                member = self._member_for_chat(tg_user_id)
                if not member:
                    self._pending.pop(str(chat_id), None)
                    await self.send(chat_id, "这个 Telegram 还没有账号。",
                                    self.guest_menu())
                    return
                if kind == "request_link":
                    await self._request_pick_title(chat_id, member, text)
                else:
                    await self._request_reason(chat_id, member, extra, text)
                return
            if kind in ("admin_find", "admin_renew_days", "admin_score_delta",
                        "admin_code", "admin_auth"):
                member = self._member_for_chat(tg_user_id)
                await self._admin_handle_text(chat_id, member, kind, extra, text)
                return
            if kind == "claim":
                self._pending.pop(str(chat_id), None)
                created = self._create_request(
                    extra.get("request_kind", "bind"), tg_user_id,
                    tg_username, text.strip())
                await self._show(
                    chat_id,
                    "📨 已提交申请，等待管理员确认。" if created
                    else "你已经有一条待处理的申请了，请耐心等待。",
                    self.guest_menu())
                return

        # Commands are dispatched after the pending-conversation check above,
        # which deliberately ignores anything starting with '/': a member who
        # typed a command instead of the answer they were asked for meant the
        # command, not a title called "/help".
        if text.startswith("/"):
            await self._handle_command(
                chat_id, tg_user_id, tg_username, text, display_name=tg_name)
            return

        if looks_like_credential(text) and not self._member_for_chat(tg_user_id):
            await self._start_registration(chat_id, tg_user_id, credential=text)
            return

        body, keyboard = self._home(tg_user_id, tg_name)
        await self._show(chat_id, body, keyboard)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        from_user = callback.get("from") or {}
        tg_user_id = str(from_user.get("id") or "")
        tg_name = from_user.get("first_name") or "朋友"
        callback_id = str(callback.get("id") or "")
        # A callback query may only be answered once, and the claim handlers
        # answer with text ("已被 X 接单") -- acking here first would swallow
        # that alert and the loser would be told nothing at all.
        # Fire the generic ack without waiting: Telegram keeps a spinner until
        # it lands, and the edit that follows should not sit behind that RTT.
        ack_task: asyncio.Task | None = None
        if not data.startswith(SELF_ANSWERING_CALLBACKS):
            ack_task = asyncio.create_task(self._answer_callback(callback_id))
            # Let the ack start its HTTP call before we do any local work.
            await asyncio.sleep(0)
        try:
            await self._run_callback(
                data, chat_id, message_id, callback_id, tg_user_id, tg_name,
                message)
        finally:
            if ack_task is not None:
                with contextlib.suppress(Exception):
                    await ack_task

    async def _run_callback(self, data: str, chat_id: Any, message_id: Any,
                            callback_id: str, tg_user_id: str, tg_name: str,
                            message: dict[str, Any]) -> None:
        if not chat_id or not message_id:
            if callback_id and data.startswith(SELF_ANSWERING_CALLBACKS):
                await self._answer_callback(callback_id)
            return
        if not self._private_chat(message):
            if data.startswith(SELF_ANSWERING_CALLBACKS):
                await self._answer_callback(callback_id)
            return
        self._touch_panel(chat_id, message_id)

        # Re-read binding state on every tap: the member could have been
        # unlinked from the panel while this keyboard sat on their screen.
        member = self._member_for_chat(tg_user_id)

        if data == "register":
            await self._start_registration(chat_id, tg_user_id)
            return
        if data == "claim":
            self._pending[str(chat_id)] = (
                "claim", time.time() + PENDING_TTL, {"request_kind": "bind"})
            await self._edit(
                chat_id, message_id,
                "🔗 <b>认领已有账号</b>\n\n请发送你在影视库里的<b>用户名</b>。\n\n"
                "<i>管理员确认后会关联到这个 Telegram。发送 /start 可取消。</i>",
                BACK_HOME)
            return
        if data == "help":
            await self._edit(
                chat_id, message_id, self._help_text(member),
                self._with_admin_row(self.member_menu(), member) if member else self.guest_menu())
            return
        if data == "home":
            self._pending.pop(str(chat_id), None)
            body, keyboard = self._home(tg_user_id, tg_name)
            await self._edit(chat_id, message_id, body, keyboard)
            return
        if data == "admin_ok":
            await self._admin_confirm(chat_id, message_id, member)
            return
        if data in ("rm_self", "rm_cascade"):
            waiting = self._pending.get(str(chat_id))
            if waiting and waiting[0] == "admin_confirm":
                extra = dict(waiting[2] or {})
                extra["cascade"] = data == "rm_cascade"
                self._pending[str(chat_id)] = (waiting[0], waiting[1], extra)
            await self._admin_confirm(chat_id, message_id, member)
            return
        if data == "admin_cancel":
            self._pending.pop(str(chat_id), None)
            await self._edit(chat_id, message_id, "已取消，什么都没做。")
            return
        if data == "admin":
            await self._admin_home(chat_id, message_id, member)
            return
        if data == "admin_find":
            await self._admin_prompt_find(chat_id, message_id, member)
            return
        if data == "admin_reqs":
            if not self.is_admin(member):
                await self._edit(chat_id, message_id, "⛔ 无权限。")
                return
            actor = self._admin_actor(member, "")
            await self._cmd_req(chat_id, actor, ["active"])
            return
        if data == "admin_code":
            if not self.is_admin(member):
                await self._edit(chat_id, message_id, "⛔ 无权限。")
                return
            self._pending[str(chat_id)] = (
                "admin_code", time.time() + PENDING_TTL, {})
            await self._edit(
                chat_id, message_id,
                "🎟 <b>生成卡密</b>\n\n请发送：<code>套餐id 天数 数量</code>\n"
                "例如 <code>standard 30 5</code>",
                [[{"text": "◀ 返回管理", "callback_data": "admin"}]])
            return
        if data == "admin_auth":
            if not self.is_admin(member):
                await self._edit(chat_id, message_id, "⛔ 无权限。")
                return
            self._pending[str(chat_id)] = (
                "admin_auth", time.time() + PENDING_TTL, {})
            await self._edit(
                chat_id, message_id,
                "✅ <b>预授权注册</b>\n\n请发送对方的 Telegram 数字 ID。",
                [[{"text": "◀ 返回管理", "callback_data": "admin"}]])
            return
        if data.startswith("admin_renew") or data in (
                "admin_pro", "admin_rev", "admin_score", "admin_rm"):
            await self._admin_user_action(chat_id, message_id, member, data)
            return
        if data == "top":
            # Rankings are about the library, not one account, so they stay
            # available to anyone who found the bot.
            await self._edit(chat_id, message_id, self._rankings_text(1),
                             self._with_admin_row(self.member_menu(), member) if member else self.guest_menu())
            return

        if not member:
            await self._edit(chat_id, message_id,
                             "这个 Telegram 还没有账号。", self.guest_menu())
            return

        if data == "me":
            await self._edit(
                chat_id, message_id,
                f"👤 <b>{member.get('username') or '-'}</b>\n\n"
                f"状态：{self._status_label(member)}\n"
                f"积分：<b>{self._balance(str(member.get('emby_user_id')))}</b>\n\n"
                "选择要查看的内容：",
                self.info_menu())
            return
        if data == "me_status":
            await self._edit(
                chat_id, message_id,
                f"📋 <b>{member.get('username') or '-'}</b>\n\n"
                f"状态：{self._status_label(member)}\n"
                f"用户组：{member.get('group_name') or '默认'}\n"
                f"有效期：{_fmt_expiry(member.get('expires_at'))}\n"
                f"备注：{member.get('note') or '—'}",
                self.info_menu())
            return
        if data == "me_points":
            await self._edit(chat_id, message_id, self._points_text(member),
                             self.info_menu())
            return
        if data == "me_nodes":
            await self._edit(chat_id, message_id, await self._nodes_text(),
                             self.info_menu())
            return
        if data == "bag":
            await self._edit(
                chat_id, message_id,
                "🎒 <b>背包</b>\n\n"
                f"当前积分：<b>{self._balance(str(member.get('emby_user_id')))}</b>\n\n"
                "这里是你的邀请码、可兑换的商品和兑换记录。",
                self.bag_menu())
            return
        if data == "checkin":
            await self._checkin(chat_id, message_id, member)
            return
        if data == "transfer":
            await self._transfer_start(chat_id, message_id)
            return
        if data == "transfer_ok":
            await self._transfer_execute(chat_id, message_id, member)
            return
        if data == "shop":
            await self._shop_view(chat_id, message_id, member)
            return
        if data.startswith("buy:"):
            await self._shop_confirm(chat_id, message_id, data.split(":", 1)[1])
            return
        if data.startswith("buyok:"):
            await self._shop_redeem(chat_id, message_id, member,
                                    data.split(":", 1)[1])
            return
        if data == "orders":
            await self._orders_view(chat_id, message_id, member)
            return
        if data == "req_new":
            await self._request_start(chat_id, message_id, member)
            return
        if data == "req_ok":
            await self._request_submit(chat_id, message_id, member)
            return
        if data == "my_requests":
            await self._my_requests(chat_id, message_id, member)
            return
        if data.startswith("req_claim:"):
            await self._request_claim(
                chat_id, message_id, callback_id, member,
                _as_request_id(data))
            return
        if data.startswith(("req_done:", "req_fail:")):
            await self._request_resolve(
                chat_id, message_id, callback_id, member,
                _as_request_id(data), done=data.startswith("req_done:"))
            return
        if data == "expiry":
            expires = member.get("expires_at")
            when = time.strftime("%Y-%m-%d", time.localtime(expires)) if expires else "永久有效"
            await self._edit(
                chat_id, message_id,
                f"⏳ <b>有效期</b>\n\n到期时间：{when}\n状态：{_fmt_expiry(expires)}\n\n"
                "<i>需要续期请联系管理员。</i>",
                self.member_menu())
            return
        if data == "devices":
            devices = self._members.devices(str(member.get("emby_user_id")))
            if not devices:
                text = "📺 <b>我的设备</b>\n\n还没有记录到设备。"
            else:
                rows = []
                for d in devices[:8]:
                    seen = d.get("last_seen_at")
                    when = time.strftime("%m-%d %H:%M", time.localtime(seen)) if seen else "—"
                    flag = "🚫 " if d.get("blocked") else ""
                    rows.append(f"{flag}{d.get('device_name') or d.get('device_id')} · {when}")
                text = "📺 <b>我的设备</b>\n\n" + "\n".join(rows)
            await self._edit(chat_id, message_id, text, self.info_menu())
            return
        if data == "usage":
            used = member.get("traffic_used_bytes") or 0
            seen = member.get("last_seen_at")
            await self._edit(
                chat_id, message_id,
                f"📊 <b>观看统计</b>\n\n本周期用量：{_fmt_bytes(used)}\n"
                f"最近活跃：{time.strftime('%Y-%m-%d %H:%M', time.localtime(seen)) if seen else '—'}",
                self.info_menu())
            return
        if data in ("invites", "invite_new"):
            await self._invites_view(chat_id, message_id, member, mint=data == "invite_new")
            return
        if data == "resetpw":
            if self._emby is None:
                await self._edit(chat_id, message_id, "后台未连接 Emby，暂时无法重置。",
                                 self.info_menu())
                return
            password = generate_password()
            ok = False
            with contextlib.suppress(Exception):
                ok = await self._emby.set_user_password(
                    str(member.get("emby_user_id")), password)
            await self._edit(
                chat_id, message_id,
                (f"🔑 <b>新密码</b>\n\n<code>{password}</code>\n\n"
                 "<i>请立刻保存，这条消息不会再发第二次。</i>") if ok
                else "❌ 重置失败，请稍后再试或联系管理员。",
                self.info_menu())
            return

    # -- polling loop ---------------------------------------------------------

    def _chat_key(self, update: dict[str, Any]) -> str:
        if "message" in update:
            chat = ((update.get("message") or {}).get("chat") or {})
            return str(chat.get("id") or "")
        callback = update.get("callback_query") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        return str(chat.get("id") or callback.get("id") or "")

    def _lock_for(self, chat_key: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_key)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_key] = lock
        return lock

    async def _dispatch_update(self, update: dict[str, Any]) -> None:
        try:
            async with self._lock_for(self._chat_key(update)):
                if "message" in update:
                    await self._handle_message(update["message"])
                elif "callback_query" in update:
                    await self._handle_callback(update["callback_query"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
            self._last_error = f"处理更新失败: {type(exc).__name__}"

    def _spawn_update(self, update: dict[str, Any]) -> None:
        task = asyncio.create_task(self._dispatch_update(update))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _poll_once(self) -> None:
        result = await self._call(
            "getUpdates",
            {"offset": self._offset, "timeout": POLL_TIMEOUT,
             "allowed_updates": ["message", "callback_query"]},
            timeout=HTTP_TIMEOUT)
        self._last_poll_at = time.time()
        for update in result or []:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            self._spawn_update(update)

    async def run(self) -> None:
        """Poll while enabled; idle cheaply while not.

        Disabling the bot in the panel must not require a restart, so the loop
        stays alive and simply stops reaching out.
        """
        self._started_at = time.time()
        backoff = 1.0
        while True:
            if not self.enabled:
                await self._close_http()
                await asyncio.sleep(5)
                continue
            try:
                await self._ensure_identity()
                await self._poll_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"轮询失败: {type(exc).__name__}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        pending = list(self._in_flight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._in_flight.clear()
        self._chat_locks.clear()
        await self._close_http()

    # -- outbound notifications ----------------------------------------------

    async def notify_member(self, member: dict[str, Any], text: str) -> bool:
        chat_id = member.get("tg_user_id")
        if not chat_id or not self.enabled:
            return False
        return await self.send(chat_id, text)

    async def notify_expiring(self, members: list[dict[str, Any]]) -> int:
        sent = 0
        for member in members:
            if not member.get("tg_user_id"):
                continue
            ok = await self.notify_member(
                member,
                "⏳ <b>有效期提醒</b>\n\n"
                f"账号 <b>{member.get('username') or '-'}</b> "
                f"{_fmt_expiry(member.get('expires_at'))}。\n"
                "需要续期请联系管理员。")
            sent += 1 if ok else 0
        return sent

    async def broadcast_rankings(self, chat_id: str, days: int = 1) -> bool:
        """Daily ranking post, for a group or channel."""
        if not chat_id or not self.enabled:
            return False
        return await self.send(chat_id, self._rankings_text(days))

    async def audit_group_membership(self) -> dict[str, Any]:
        """Which linked members have left the required group.

        Reported, never enforced: someone who left a chat has not necessarily
        stopped paying, and silently suspending them would be the panel making
        a call that belongs to a person.
        """
        chat = str(self._cfg().get("require_group") or "").strip()
        if not chat or not self.enabled:
            return {"checked": 0, "left": [], "unavailable": True}
        left: list[dict[str, Any]] = []
        checked = 0
        # linked_telegram(), not list(): the latter caps at 500 rows, so a
        # larger install would silently skip everyone past the cap and still
        # report "all present". An audit that under-reports is worse than none,
        # because it is believed.
        for member in self._members.linked_telegram():
            tg_id = member.get("tg_user_id")
            if not tg_id:
                continue
            checked += 1
            allowed, status = await self.in_required_group(str(tg_id))
            if not allowed:
                left.append({
                    "emby_user_id": member.get("emby_user_id"),
                    "username": member.get("username"),
                    "tg_user_id": tg_id,
                    "status": status,
                })
        return {"checked": checked, "left": left, "unavailable": False}
