"""Telegram bot: registration, account self-service and rankings.

The bot is the front door for new members. Someone who has never been here
registers in the chat and walks away with a working Emby account; someone who
already has one lands on their own status. The keyboard is chosen from that
state on every render, so neither audience is offered buttons that lead
nowhere.

Registration creates the Emby account directly. There is no code to copy from
a panel, because the chat itself already proves who is asking: the Telegram id
is the identity, and it is recorded as the owner at creation time. That leaves
one reviewed operation: moving an existing account to a different Telegram.
Old account claiming is retired. Reassignment requires password verification
in private plus an administrator's approval in the configured group.

Registration passwords are generated. Passwords supplied for reassignment
verification are never echoed, logged or persisted; input messages are removed.

Polling, not webhooks. A webhook needs a public HTTPS route into the panel;
long polling reaches out instead, so the panel stays reachable only from where
it already was.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import re
import secrets
import string
import time
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

import httpx

from app.core.errors import ConfigError, ConflictError
from app.modules.bot_rebinding import RebindBotMixin
from app.modules.bot_views import (
    bandwidth_lines,
    duration,
    member_mention,
    poetry_line,
    quota_lines,
    watch_rank_mention,
)
from app.modules.gift_receipts import GiftReceipts
from app.modules.group_membership import GroupMembership, GroupMembershipPlugin
from app.modules.groups import WHITELIST_GROUP_ID
from app.modules.rebinding import RebindingService
from app.modules.requests import RequestError
from app.modules.settings import parse_group_interaction_chats
from app.modules.shop import ShopError
from app.modules.stats import ranking_stamp
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
    [{"text": "◀ 返回主菜单 / 取消", "callback_data": "home"}],
]


def looks_like_credential(raw: str) -> bool:
    """True when the text is the shape of an invite code or a card.

    This is a shape check, not a validity check: a mistyped code still looks
    like a code, and the registration service is what says so.
    """
    compact = "".join(ch for ch in str(raw or "").strip().upper() if ch.isalnum())
    return (len(compact) in CREDENTIAL_LENGTHS
            or (len(compact) == 16 and compact.upper().startswith("GIFT")))

# Registration conversation state is intentionally short-lived: an abandoned
# half-finished signup should not hold a slot or confuse the next /start.
PENDING_TTL = 600.0

REQUEST_KINDS = ("rebind",)

# Status marks for the member's own request list.
REQUEST_STATUS_ICONS = {
    "open": "🕓", "claimed": "🔧", "done": "✅", "rejected": "❌",
}

# Callbacks whose handler sends its own answerCallbackQuery, because it has
# something to say. Everything else is acked immediately.
SELF_ANSWERING_CALLBACKS = (
    "req_claim:", "req_done:", "req_fail:", "tg_rebind_review:", "urank:", "urank_close")

ADMIN_HELP = """🛠 <b>管理员命令</b>

<b>查询</b>
<code>/kk TG数字ID或用户名</code> 查看账号；未注册可赠送开号
<code>/manage</code> 打开管理菜单
<code>/req [open|claimed]</code> 最近 10 条求片

<b>用户组</b>
<code>/prouser TG数字ID或账号名</code> 直接授予白名单（亦可回复消息；保留账号状态）
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


def _public_error(exc: Exception) -> str:
    from app.modules.member_ops import redact
    # Only domain refusals are user-facing. Unknown failures may embed raw
    # credentials, request bodies or local paths; keep their type, not text.
    detail = str(exc) if isinstance(exc, (ValueError, ShopError, RequestError)) else type(exc).__name__
    return escape(redact(detail))


def generate_password(length: int = 12) -> str:
    """Passwords are issued, not chosen: registration never asks a member to choose one."""
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


def _bar(percent: float | None, width: int = 10) -> str:
    if percent is None:
        return "░" * width
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(round(pct / 100.0 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


RULES_TEXT = """📜 <b>行为准则</b>

· 账号仅供本人使用，禁止分享、转卖或公开线路
· 禁止用下载工具、多开设备把带宽打满
· 求片请给准确的 TMDB 链接，不要重复提交
· 流量或设备超出套餐后会被限速或暂停播放
· 遵守当地法律法规，片源仅限个人观影

发送 /start 回到菜单。"""

MEMBER_COMMANDS = {"start", "help", "me", "myinfo", "rules", "rank"}
GROUP_MEMBER_COMMANDS = ("start", "me", "myinfo", "rank", "rules", "help")
GROUP_ADMIN_COMMANDS = (
    "kk", "renew", "score", "prouser", "revuser", "rmemby", "rm",
)
ADMIN_TARGET_COMMANDS = {
    "kk", "renew", "score", "prouser", "revuser", "rm", "rmemby",
}
PRIVATE_ONLY_COMMANDS = {"register", "claim", "rebind", "resetpw"}
GROUP_BRIEF_TTL = 60.0

_SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tg_session", default=None)
_THREAD: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "tg_thread", default=None)
_GROUP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "tg_group", default=False)
_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tg_actor", default="")
_CALL_ERROR: contextvars.ContextVar[str | None] = contextvars.ContextVar('tg_call_error', default=None)
_QUIET_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar('tg_quiet_call', default=False)


class TelegramBot(RebindBotMixin):
    """Long-polling bot bound to the panel's member records."""

    def __init__(self, config_provider: Any, members: Any, emby: Any = None,
                 stats: Any = None, db: Any = None,
                 registration: Any = None, points: Any = None,
                 shop: Any = None, plugins: Any = None,
                 scheduler: Any = None, requests: Any = None,
                 tmdb: Any = None, groups: Any = None, *,
                 on_password_changed: Callable[[], None] | None = None,
                 on_member_changed: Callable[[str, int | None], Awaitable[dict[str, Any]]] | None = None) -> None:
        self._config = config_provider
        self._members = members
        self._emby = emby
        self._stats = stats
        self._db = db
        self._rebinding = RebindingService(db) if db is not None else None
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
        self._on_password_changed = on_password_changed
        self._on_member_changed = on_member_changed
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._last_error = ""
        self._last_poll_at = 0.0
        self._started_at = 0.0
        # chat id -> what the bot is waiting for, with a deadline
        self._pending: dict[str, tuple[str, float, dict[str, Any]]] = {}
        # Only a recipient's gift-registration intent may survive membership
        # guidance. Never save/replay an administrative action through the gate.
        self._gift_claims: dict[tuple[str, int], tuple[float, str, str]] = {}
        # chat id -> the one panel message this conversation is editing.
        # A request that keeps adding replies is what made 求片 feel messy.
        self._panel: dict[str, int] = {}
        self._photo_panels: set[tuple[str, int]] = set()
        # Only interactive panels may be cleaned up, never unrelated notices.
        self._menu_panel: dict[str, int] = {}
        self._chat_commands: dict[str, tuple] = {}
        self._card_actor: dict[str, str] = {}
        # Target selection outlives a prompt, but belongs to one message and
        # one verified operator; it is never inferred from the last /kk.
        self._admin_panels: dict[tuple[str, int], dict[str, Any]] = {}
        self._retired_panels: dict[tuple[str, int], float] = {}
        self._installed_group_chats: set[str] = set()
        self._active_bot_id = self._token().split(":", 1)[0]
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
        self._registration_lock = asyncio.Lock()
        self._gift_receipts = GiftReceipts(self) if db is not None and registration is not None else None
        self.membership = GroupMembership(self)

    def bind_plugins(self, registry: Any) -> None:
        """Late-bind the plugin registry.

        The bot is constructed before the plugins are registered -- they take
        it as a context member so they can message people -- so the dependency
        runs both ways and one of them has to be attached afterwards.
        """
        self._plugins = registry
        if registry.get('group_membership') is None:
            registry.register(GroupMembershipPlugin(self.membership, registry))

    async def _after_member_change(self, before: dict[str, Any]) -> str:
        """Report remote uncertainty without undoing a committed local grant."""
        if self._on_member_changed is None:
            return ''
        try:
            result = await self._on_member_changed(
                str(before['emby_user_id']), before.get('bandwidth_limit_kbps'))
            if result.get('ok') is not False and result.get('remote_ok') is not False:
                return ''
        except Exception:  # noqa: BLE001 - never expose callback/adapter exception bodies
            self._last_error = '成员权益已更新，远端同步未确认'
        return '\n⚠ 本地已更新，远端未确认，请管理员重试同步。'

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
        self._check_bot_identity()
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

    def _record_call_error(self, error: str) -> None:
        _CALL_ERROR.set(error)
        if not _QUIET_CALL.get():
            self._last_error = error

    async def _delete_trigger_command(self, chat_id: Any, message_id: Any) -> None:
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            return
        # Best-effort housekeeping must neither publish noise nor overwrite
        # another task's operational error (even when Telegram denies deletion).
        token = _QUIET_CALL.set(True)
        error_token = _CALL_ERROR.set(None)
        try:
            with contextlib.suppress(Exception):
                await self._call('deleteMessage', {'chat_id': chat_id, 'message_id': message_id}, timeout=10)
        finally:
            _CALL_ERROR.reset(error_token)
            _QUIET_CALL.reset(token)

    async def _call(self, method: str, payload: dict[str, Any] | None = None,
                    timeout: float = 20) -> Any:
        _CALL_ERROR.set('')
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
            self._record_call_error(f"{type(exc).__name__}: 请求失败")
            return None
        if not isinstance(body, dict):
            self._record_call_error('Telegram 返回格式无效')
            return None
        if not body.get("ok"):
            from app.modules.member_ops import redact
            self._record_call_error(redact(str(body.get("description") or "Telegram 拒绝了请求").replace(auth_part, '***')))
            return None
        self._record_call_error('')
        return body.get("result")

    async def _call_multipart(self, method: str, fields: dict[str, Any],
                               files: dict[str, tuple[str, bytes, str]],
                               timeout: float = 40) -> Any:
        _CALL_ERROR.set('')
        auth_part = self._token()
        if not auth_part:
            return None
        client = await self._client()
        if client is None:
            return None
        url = f"{API_ROOT}/bot{auth_part}/{method}"
        payload = dict(fields)
        if isinstance(payload.get("reply_markup"), (dict, list)):
            payload["reply_markup"] = json.dumps(payload["reply_markup"], ensure_ascii=False)
        chat_id = payload.get("chat_id")
        thread = _THREAD.get() if chat_id is not None and self._in_bound_chat(chat_id) else None
        if thread:
            payload["message_thread_id"] = thread
        try:
            r = await client.post(url, data=payload, files=files, timeout=timeout)
            body = r.json()
        except Exception as exc:  # noqa: BLE001 - token lives in the URL
            self._record_call_error(f"{type(exc).__name__}: 请求失败")
            return None
        if not isinstance(body, dict) or not body.get("ok"):
            from app.modules.member_ops import redact
            self._record_call_error(redact(str((body or {}).get("description") or "Telegram 拒绝了请求").replace(auth_part, '***')))
            return None
        self._record_call_error('')
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

    @staticmethod
    def _command_list(admin: bool = False, *, group: bool = False
                      ) -> list[dict[str, str]]:
        if group:
            commands = [
                {"command": "start", "description": "打开私聊"},
                {"command": "me", "description": "我的简卡"},
                {"command": "rank", "description": "观看排行"},
                {"command": "rules", "description": "行为准则"},
                {"command": "help", "description": "使用说明"},
            ]
            if admin:
                commands += [
                    {"command": "kk", "description": "查用户／赠送开号"},
                    {"command": "renew", "description": "续期"},
                    {"command": "score", "description": "调整积分"},
                    {"command": "prouser", "description": "直接授予白名单"},
                    {"command": "revuser", "description": "移回默认组"},
                    {"command": "rmemby", "description": "删除账号"},
                ]
            return commands
        commands = [
            {"command": "start", "description": "打开菜单"},
            {"command": "me", "description": "我的账号"},
            {"command": "usage", "description": "用量与观看时长"},
            {"command": "rebind", "description": "TG 换绑申请"},
            {"command": "rank", "description": "观看排行"},
            {"command": "help", "description": "使用说明"},
            {"command": "rules", "description": "行为准则"},
        ]
        if admin:
            commands += [
                {"command": "manage", "description": "用户管理"},
                {"command": "kk", "description": "查用户／赠送开号"},
                {"command": "prouser", "description": "直接授予白名单"},
            ]
        return commands

    def invalidate_commands(self) -> None:
        """Force command scopes to be rewritten on the next poll or verify."""
        self._commands_installed = False
        self._chat_commands.clear()

    async def _sync_chat_commands(self, chat_id: Any, tg_user_id: str,
                                  language: str = "", *, group: bool = False
                                  ) -> None:
        """Replace legacy chat overrides, including the user's language scope."""
        admin = self.is_admin(self._member_for_chat(tg_user_id))
        lang = str(language).lower().split("-", 1)[0].split("_", 1)[0]
        lang = lang if re.fullmatch(r"[a-z]{2}", lang) else ""
        if group:
            if not self._group_chat_allowed({"id": chat_id}):
                return
            chat_state = ("group-chat", lang)
            if self._chat_commands.get(f"gchat:{chat_id}") != chat_state:
                for code in dict.fromkeys(["", lang]):
                    result = await self._call("setMyCommands", {
                        "scope": {"type": "chat", "chat_id": chat_id},
                        "language_code": code,
                        "commands": self._command_list(False, group=True),
                    }, timeout=15)
                    if result is not True:
                        return
                self._chat_commands[f"gchat:{chat_id}"] = chat_state
            member_state = ("group-member", admin, lang)
            member_key = f"gmember:{chat_id}:{tg_user_id}"
            if self._chat_commands.get(member_key) == member_state:
                return
            scope = {"type": "chat_member", "chat_id": chat_id,
                     "user_id": int(tg_user_id) if str(tg_user_id).lstrip("-").isdigit()
                     else tg_user_id}
            commands = self._command_list(admin, group=True)
            for code in dict.fromkeys(["", lang]):
                result = await self._call("setMyCommands", {
                    "scope": scope, "language_code": code, "commands": commands,
                }, timeout=15)
                if result is not True:
                    return
            self._chat_commands[member_key] = member_state
            return
        state = (admin, lang)
        if self._chat_commands.get(str(chat_id)) == state:
            return
        for code in dict.fromkeys(["", lang]):
            result = await self._call("setMyCommands", {
                "scope": {"type": "chat", "chat_id": chat_id},
                "language_code": code, "commands": self._command_list(admin),
            }, timeout=15)
            if result is not True:
                return  # retry on the next update; don't cache a failed write
        self._chat_commands[str(chat_id)] = state

    async def _delete_command_scope(self, scope: dict[str, Any],
                                    language: str = "") -> None:
        await self._call("deleteMyCommands", {
            "scope": scope, "language_code": language,
        }, timeout=15)

    async def _install_commands(self) -> None:
        """Install private defaults, allowed-group menus, and drop stale scopes."""
        if self._commands_installed or not self._token():
            return
        for scope in (
            {"type": "default"},
            {"type": "all_private_chats"},
            {"type": "all_group_chats"},
            {"type": "all_chat_administrators"},
        ):
            for lang in ("zh", "en"):
                await self._delete_command_scope(scope, lang)
        await self._delete_command_scope({"type": "all_chat_administrators"})
        await self._delete_command_scope({"type": "all_group_chats"})
        result = await self._call("setMyCommands", {
            "commands": self._command_list(),
        }, timeout=15)
        if result is not True:
            return
        private = await self._call("setMyCommands", {
            "scope": {"type": "all_private_chats"},
            "commands": self._command_list(),
        }, timeout=15)
        if private is not True:
            return
        wanted = {c for c in self._group_allowlist() if re.fullmatch(r"-?\d+", c)}
        for stale in self._installed_group_chats - wanted:
            await self._delete_command_scope({"type": "chat", "chat_id": stale})
            await self._delete_command_scope(
                {"type": "chat", "chat_id": int(stale)})
        for chat_id in wanted:
            scoped = await self._call("setMyCommands", {
                "scope": {"type": "chat", "chat_id": int(chat_id)},
                "commands": self._command_list(False, group=True),
            }, timeout=15)
            if scoped is not True:
                return
        self._installed_group_chats = wanted
        self._commands_installed = True

    def _check_bot_identity(self) -> None:
        bot_id = self._token().split(":", 1)[0]
        if bot_id != self._active_bot_id:
            self._active_bot_id = bot_id
            self._offset = 0
            self._panel.clear()
            self._photo_panels.clear()
            self._menu_panel.clear()
            self._pending.clear()
            self._gift_claims.clear()
            self._chat_commands.clear()
            self._card_actor.clear()
            self._admin_panels.clear()
            self._retired_panels.clear()
            self._installed_group_chats.clear()
            self._commands_installed = False
            self._bot_username = ""

    async def _ensure_identity(self) -> None:
        self._check_bot_identity()
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

    def _private_link(self, payload: str = "") -> str:
        user = self._bot_username
        if not user:
            return ""
        if payload:
            return f"https://t.me/{user}?start={payload}"
        return f"https://t.me/{user}"

    def _pkey(self, chat_id: Any) -> str:
        session = _SESSION.get()
        if session and (session == str(chat_id) or session.startswith(f'g:{chat_id}:')):
            return session
        return str(chat_id)

    def _in_bound_chat(self, chat_id: Any) -> bool:
        return _SESSION.get() is None or self._pkey(chat_id) == _SESSION.get()

    @staticmethod
    def _session_key(chat_id: Any, actor_id: Any, *, group: bool,
                     thread_id: int | None = None) -> str:
        if not group:
            return str(chat_id)
        return f"g:{chat_id}:{thread_id or 0}:{actor_id}"

    @contextlib.contextmanager
    def _bind_session(self, chat_id: Any, actor_id: Any, *, group: bool = False,
                      thread_id: int | None = None):
        session = self._session_key(chat_id, actor_id, group=group,
                                    thread_id=thread_id)
        t_session = _SESSION.set(session)
        t_thread = _THREAD.set(thread_id if group else None)
        t_group = _GROUP.set(group)
        t_actor = _ACTOR.set(str(actor_id or ""))
        try:
            yield session
        finally:
            _SESSION.reset(t_session)
            _THREAD.reset(t_thread)
            _GROUP.reset(t_group)
            _ACTOR.reset(t_actor)

    def _group_allowlist(self) -> list[str]:
        try:
            return parse_group_interaction_chats(
                self._cfg().get("group_interaction_chats"))
        except ConfigError:
            return []

    def _group_chat_allowed(self, chat: dict[str, Any] | None) -> bool:
        allow = self._group_allowlist()
        if not allow:
            return False
        chat = chat or {}
        cid = str(chat.get("id") or "").strip()
        username = str(chat.get("username") or "").strip().lstrip("@")
        tokens = {cid}
        if re.fullmatch(r"-?\d+", cid):
            tokens.add(str(int(cid)))
        if username:
            tokens.add("@" + username)
        return any(token in allow for token in tokens if token)

    @staticmethod
    def _is_group_chat(message: dict[str, Any] | None) -> bool:
        kind = str(((message or {}).get("chat") or {}).get("type") or "private")
        return kind in ("group", "supergroup")

    @staticmethod
    def _thread_id(message: dict[str, Any] | None) -> int | None:
        message = message or {}
        # Reply chains can carry message_thread_id without being a forum
        # topic; it may differ or disappear on the Bot's callback message.
        # Only actual topics define a separate management session/routing scope.
        if not message.get("is_topic_message"):
            return None
        raw = message.get("message_thread_id")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value or None

    def _addressed_to_other_bot(self, text: str) -> bool:
        first = (text or "").split(None, 1)[0] if text else ""
        if not first.startswith("/") or "@" not in first:
            return False
        mentioned = first.split("@", 1)[1].strip().lower()
        if not mentioned:
            return False
        mine = (self._bot_username or "").lower()
        return bool(mine) and mentioned != mine

    @staticmethod
    def _anonymous_sender(message: dict[str, Any] | None) -> bool:
        message = message or {}
        if message.get("sender_chat"):
            return True
        sender = message.get("from") or {}
        if sender.get("is_bot") and str(sender.get("username") or "").lower() == "groupanonymousbot":
            return True
        return False

    def _remember_card_actor(self, chat_id: Any, message_id: Any) -> None:
        actor = _ACTOR.get()
        if not actor or not message_id or not _GROUP.get() or not self._in_bound_chat(chat_id):
            return
        try:
            self._card_actor[f"{chat_id}:{int(message_id)}"] = str(actor)
        except (TypeError, ValueError):
            return

    def _card_owner(self, chat_id: Any, message_id: Any) -> str | None:
        try:
            return self._card_actor.get(f"{chat_id}:{int(message_id)}")
        except (TypeError, ValueError):
            return None

    def _touch_panel(self, chat_id: Any, message_id: Any) -> None:
        if chat_id is None or not message_id or not self._in_bound_chat(chat_id):
            return
        try:
            self._panel[self._pkey(chat_id)] = int(message_id)
        except (TypeError, ValueError):
            return
        self._remember_card_actor(chat_id, message_id)

    def _menu_key(self, chat_id: Any) -> str:
        # The public numeric bot id namespaces message ids; never store a token.
        bot_id = self._token().split(":", 1)[0]
        return f"telegram.panel:{bot_id}:{self._pkey(chat_id)}"

    def _saved_menu(self, chat_id: Any) -> int | None:
        key = self._pkey(chat_id)
        mid = self._menu_panel.get(key)
        if mid is None and self._db is not None:
            row = self._db.one("SELECT value FROM meta WHERE key=?", (self._menu_key(chat_id),))
            if row and str(row.get("value") or "").isdigit():
                mid = int(row["value"])
                self._menu_panel[key] = mid
        return mid

    def _remember_menu(self, chat_id: Any, message_id: int,
                       keyboard: list[list[dict[str, str]]] | None) -> None:
        # Uploader fan-out is a notification with its own buttons, not a menu.
        if not self._in_bound_chat(chat_id):
            return
        actions = [b.get("callback_data", "") for row in keyboard or [] for b in row]
        if actions and not any(a.startswith(SELF_ANSWERING_CALLBACKS) for a in actions):
            key = self._pkey(chat_id)
            changed = self._menu_panel.get(key) != int(message_id)
            self._menu_panel[key] = int(message_id)
            self._touch_panel(chat_id, message_id)
            if changed and self._db is not None:
                self._db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (self._menu_key(chat_id), str(message_id)))

    async def _retire_menu(self, chat_id: Any, message_id: int) -> None:
        self._photo_panels.discard((str(chat_id), int(message_id)))
        if await self._call("deleteMessage", {
                "chat_id": chat_id, "message_id": message_id}) is not True:
            # Telegram may refuse deletion of old messages. At least disarm it.
            await self._call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": {"inline_keyboard": []}})

    async def send(self, chat_id: str | int, text: str,
                   keyboard: list[list[dict[str, str]]] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        thread = _THREAD.get() if self._in_bound_chat(chat_id) else None
        if thread:
            payload["message_thread_id"] = thread
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = await self._call("sendMessage", payload)
        if isinstance(result, dict) and result.get("message_id"):
            if _GROUP.get() and self._in_bound_chat(chat_id):
                key = self._pkey(chat_id)
                self._menu_panel[key] = int(result["message_id"])
                self._touch_panel(chat_id, result["message_id"])
            else:
                self._remember_menu(chat_id, result["message_id"], keyboard)
            return True
        return result is not None

    async def _answer_callback(self, callback_id: str, text: str = "") -> None:
        # Telegram shows a spinner until this lands. Keep it short so a hung
        # ack cannot sit in front of the real reply.
        await self._call("answerCallbackQuery",
                         {"callback_query_id": callback_id, "text": text},
                         timeout=10)

    async def send_message(self, chat_id: str | int, text: str,
                           keyboard: list[list[dict[str, str]]] | None = None, *,
                           thread_id: int | None = None) -> int | None:
        """Like send(), but returns the message id.

        The request fan-out needs it: when one uploader claims, every other
        uploader's message has to be edited, and without its id there is no
        way to reach it.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        thread = _THREAD.get() if self._in_bound_chat(chat_id) else None
        if thread:
            payload["message_thread_id"] = thread
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        if thread_id is not None:
            payload['message_thread_id'] = thread_id
        result = await self._call("sendMessage", payload)
        if isinstance(result, dict) and result.get("message_id"):
            self._remember_menu(chat_id, result["message_id"], keyboard)
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

    async def _replace_panel(self, chat_id: Any, message_id: int, text: str,
                             keyboard: list[list[dict[str, str]]] | None) -> bool:
        if not self._in_bound_chat(chat_id):
            return False  # do not turn an outbound notice into a new personal menu
        old_key = (str(chat_id), int(message_id))
        panel = self._admin_panels.get(old_key)
        new = await self.send_message(chat_id, text, keyboard)
        if not new:
            return False
        self._touch_panel(chat_id, new)
        claim = self._gift_claims.pop(old_key, None)
        if claim:
            self._gift_claims[(str(chat_id), int(new))] = claim
        waiting = self._pending.get(self._pkey(chat_id))
        if waiting and waiting[2].get('message_id') == message_id:
            waiting[2]['message_id'] = new
        if panel:
            self._admin_panels[(str(chat_id), int(new))] = panel
            self._admin_panels.pop(old_key, None)
        self._retired_panels[old_key] = time.time() + PENDING_TTL
        await self._retire_menu(chat_id, message_id)
        return True

    async def _edit(self, chat_id: str | int, message_id: int, text: str,
                    keyboard: list[list[dict[str, str]]] | None = None) -> bool:
        if (str(chat_id), int(message_id)) in self._photo_panels:
            # Entering a feature from a photo starts one text panel. Existing
            # text panels remain text, including when returning to home.
            return await self._replace_panel(chat_id, message_id, text, keyboard)
        payload: dict[str, Any] = {
            'chat_id': chat_id, 'message_id': message_id, 'text': text,
            'parse_mode': 'HTML', 'disable_web_page_preview': True,
            'reply_markup': {'inline_keyboard': keyboard or []},
        }
        _CALL_ERROR.set(None)
        result = await self._call('editMessageText', payload)
        # The native transport records errors task-locally: another chat's
        # callback acknowledgement must not turn this error into success.
        error = _CALL_ERROR.get()
        error = (self._last_error if error is None else error).lower()
        if result is not None or 'not modified' in error:
            self._touch_panel(chat_id, message_id)
            self._remember_menu(chat_id, message_id, keyboard)
            return True
        if any(reason in error for reason in (
                'message to edit not found', "message can't be edited",
                'message cannot be edited', 'message is not editable', 'message_id_invalid')):
            return await self._replace_panel(chat_id, message_id, text, keyboard)
        # A timeout may have applied the edit already. Do not publish a second
        # result or replay business logic; a later refresh is safe.
        return False

    async def _show(self, chat_id: Any, text: str,
                    keyboard: list[list[dict[str, str]]] | None = None) -> bool:
        mid = self._panel.get(self._pkey(chat_id))
        if mid:
            return await self._edit(chat_id, mid, text, keyboard)
        return await self.send(chat_id, text, keyboard)

    async def _show_home(self, chat_id: Any, tg_id: str, tg_name: str) -> None:
        body, keyboard = self._home(tg_id, tg_name)
        if self._gift_receipts and not _GROUP.get():
            keyboard += await self._gift_receipts.retry_menu(tg_id)
        logo = str(self._cfg().get('menu_logo_url') or '')
        if self._panel.get(self._pkey(chat_id)):
            await self._show(chat_id, body, keyboard)
            return
        if logo and not _GROUP.get() and len(body) <= 900:
            old = self._panel.get(self._pkey(chat_id))
            result = await self._call('sendPhoto', {'chat_id': chat_id, 'photo': logo,
                'caption': body, 'parse_mode': 'HTML',
                'reply_markup': {'inline_keyboard': keyboard}})
            if isinstance(result, dict) and result.get('message_id'):
                mid = int(result['message_id'])
                self._photo_panels.add((str(chat_id), mid))
                self._remember_menu(chat_id, mid, keyboard)
                if old and old != mid:
                    await self._retire_menu(chat_id, old)
                return
        await self._show(chat_id, body, keyboard)

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
        if status == "restricted":
            # A confirmed nonmember is denied. Missing membership detail
            # follows the existing unavailable-check admission policy.
            return bool(result.get("is_member", True)), status
        if status in ("creator", "administrator", "member"):
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
        """No account here: register with a credential or request verified reassignment."""
        rows: list[list[dict[str, str]]] = []
        if self._registration_open():
            rows.append([{"text": "🆕 注册账号", "callback_data": "register"}])
        rows.append([
            {"text": "🔗 TG 换绑申请", "callback_data": "rebind"},
            {"text": "📜 准则", "callback_data": "rules"},
        ])
        rows.append([{"text": "❓ 使用说明", "callback_data": "help"}])
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
        """A stable two-column home; optional point actions belong in the bag."""
        return [
            [{"text": "👤 我的账号", "callback_data": "me"},
             {"text": "📊 用量与观看", "callback_data": "usage"}],
            [{"text": "🌐 播放线路", "callback_data": "me_nodes"},
             {"text": "🎬 求片中心", "callback_data": "request_center"}],
            [{"text": "🎒 积分背包", "callback_data": "bag"},
             {"text": "🏆 排行榜", "callback_data": "rank"}],
            [{"text": "🔗 TG 换绑", "callback_data": "rebind"},
             {"text": "📜 使用准则", "callback_data": "rules"}],
            [{"text": "❓ 使用帮助", "callback_data": "help"}],
        ]

    def _with_admin_row(self, rows: list[list[dict[str, str]]],
                        member: dict[str, Any] | None) -> list[list[dict[str, str]]]:
        if self.is_admin(member):
            rows.append([{"text": "🛠 管理", "callback_data": "admin"}])
        return rows

    @staticmethod
    def info_menu() -> list[list[dict[str, str]]]:
        """Account details. Line/server lives on the home row, not here."""
        return [
            [{"text": "📋 账号状态", "callback_data": "me_status"},
             {"text": "📊 用量", "callback_data": "usage"}],
            [{"text": "📺 设备", "callback_data": "devices"},
             {"text": "💰 积分", "callback_data": "me_points"}],
            [{"text": "📋 我的求片", "callback_data": "my_requests"},
             {"text": "🔑 重置密码", "callback_data": "resetpw"}],
            [{"text": "🔗 TG 换绑", "callback_data": "rebind"}],
            [{'text': '🔄 刷新我的账号', 'callback_data': 'me'},
             {'text': '◀ 功能首页', 'callback_data': 'home'}],
        ]

    @staticmethod
    def nodes_menu() -> list[list[dict[str, str]]]:
        """Line page is not the account card; keep only refresh and home."""
        return [
            [{"text": "🔄 刷新线路", "callback_data": "me_nodes"}],
            [{"text": "◀ 功能首页", "callback_data": "home"}],
        ]

    def bag_menu(self) -> list[list[dict[str, str]]]:
        """What the member owns or can spend; don't advertise disabled plugins."""
        extra = []
        if self._plugin_on("checkin"):
            extra.append({"text": "✅ 每日签到", "callback_data": "checkin"})
        if self._plugin_on("points_transfer"):
            extra.append({"text": "💸 积分转账", "callback_data": "transfer"})
        return ([extra] if extra else []) + [
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
        }.get(str(member.get("state") or member.get("status") or ""), str(member.get("status") or "未知"))

    def _help_text(self, member: dict[str, Any] | None) -> str:
        if member:
            return (
                "❓ <b>使用说明</b>\n\n"
                "· <b>我的账号</b>：状态、用量、设备、密码\n"
                "· <b>TG 换绑</b>：更换 TG、原号失效或无法发言时使用，账号权益保留\n"
                "· <b>线路</b>：服务器地址和当前节点水位\n"
                "· <b>背包</b>：邀请码、积分兑换\n"
                "· <b>求片</b>：发送 TMDB 链接即可提交\n"
                "· <b>准则</b>：账号使用约定\n"
                "· 发送 /start 随时回到菜单\n\n"
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
            "· <b>TG 换绑</b>：原 TG 更换或遗失时，验证 Emby 密码后申请管理员审核\n"
            "· 已绑定的用户直接使用，无需认领\n\n"
            "遇到问题请联系管理员。"
        )

    @staticmethod
    def _whitelist_decoration(member: dict[str, Any]) -> str:
        # Identity is the reserved group ID, never its editable display name or role.
        return '💠 ' if member.get('group_id') == WHITELIST_GROUP_ID else ''

    @staticmethod
    def _group_label(member: dict[str, Any]) -> str:
        name = escape(str(member.get('group_name') or '默认'))
        return name

    def _home(self, tg_user_id: str, tg_name: str) -> tuple[str, list[list[dict[str, str]]]]:
        member = self._member_for_chat(tg_user_id)
        if not member:
            return self._guest_home(tg_name), self.guest_menu()
        bits = [self._status_label(member)]
        group = str(member.get("group_name") or "").strip()
        if group:
            bits.append(self._group_label(member))
        bits.append(_fmt_expiry(member.get("expires_at_effective", member.get("expires_at"))))
        lines = [
            self._whitelist_decoration(member) + "<b>MediaDeck · 我的影库</b>\n",
            f"{escape(str(member.get('username') or '成员'))}，欢迎回来",
            " · ".join(bits),
        ]
        user_id = str(member.get("emby_user_id") or "")
        balance = self._balance(user_id)
        if balance or self._plugin_on("checkin") or self._plugin_on("points_transfer"):
            lines.append(f"积分 <b>{balance}</b>")
        lines.append("")
        lines.append("请选择功能")
        return "\n".join(lines), self._with_admin_row(self.member_menu(), member)

    def _guest_home(self, tg_name: str) -> str:
        cfg = self._cfg()
        open_ = self._registration_open()
        channels = []
        if cfg.get("allow_invite", True):
            channels.append("邀请码")
        if cfg.get("allow_redeem", True):
            channels.append("卡密")
        lines = [f"👋 你好，{escape(str(tg_name))}\n"]
        if open_:
            lines.append("这里是影视库账号服务，<b>开放注册中</b>。\n")
            if channels:
                how = "或".join(channels)
                lines.append(f"没有账号：用{how}即可开通")
            else:
                lines.append("管理员已授权的用户可以直接注册。")
            lines.append("已有绑定：请使用原 Telegram；换了 TG 可申请「TG 换绑」。")
        else:
            lines.append("这里是影视库账号服务，<b>当前暂停注册</b>。\n")
            lines.append("已有用户请使用已绑定的 Telegram；更换 TG 可申请换绑。")
        if str(cfg.get("require_group") or "").strip():
            lines.append("\n注册前需要先加入官方群组。")
        return "\n".join(lines)

    # -- registration ---------------------------------------------------------

    def _sweep_pending(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for chat, (_, deadline, _) in list(self._pending.items()):
            if deadline <= now:
                self._pending.pop(chat, None)
        for key, claim in list(self._gift_claims.items()):
            if claim[0] + PENDING_TTL <= now:
                self._gift_claims.pop(key, None)
        for key, panel in list(self._admin_panels.items()):
            if panel['expires'] <= now:
                self._admin_panels.pop(key, None)
                self._retired_panels[key] = now + PENDING_TTL
        for key, deadline in list(self._retired_panels.items()):
            if deadline <= now:
                self._retired_panels.pop(key, None)

    def _gift_claim_problem(self, tg_id: str, code: str) -> str:
        if self._member_for_chat(tg_id):
            return '你已经有账号了，不能重复领取注册资格。'
        admission = self._resolve(tg_id, code)
        if admission is None:
            return '注册服务暂不可用，请稍后重新打开领取链接。'
        return '' if admission.allowed else admission.reason

    async def _resume_gift_claim(self, chat_id: Any, message_id: int, tg_id: str) -> bool:
        claim = self._gift_claims.pop((str(chat_id), int(message_id)), None)
        if claim is None:
            return False
        deadline, owner, code = claim
        problem = ('领取会话已过期，请重新打开群里的领取链接。' if deadline <= time.time()
                   else '这份赠送资格不属于你的 Telegram 账号。' if owner != tg_id
                   else self._gift_claim_problem(tg_id, code))
        if problem:
            self._pending.pop(self._pkey(chat_id), None)
            await self._show(chat_id, '❌ ' + escape(problem), BACK_HOME)
        else:
            await self._start_registration(chat_id, tg_id, credential=code)
        return True

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
            await self._show(chat_id, "你已经有账号了。", self.member_menu())
            return
        blocked = await self._registration_blocked(tg_user_id)
        if blocked:
            await self._show(chat_id, f"🚫 {blocked}", self.guest_menu())
            return
        self._sweep_pending()

        admission = self._resolve(tg_user_id, credential)
        if admission is not None and admission.allowed:
            self._pending[self._pkey(chat_id)] = (
                "username", time.time() + PENDING_TTL,
                {"admission": admission})
            await self._show(chat_id, self._USERNAME_PROMPT, BACK_HOME)
            return

        if self._registration is None:
            # No registration service wired (older deployments / tests): fall
            # back to the plain username step rather than blocking everyone.
            self._pending[self._pkey(chat_id)] = ("username", time.time() + PENDING_TTL, {})
            await self._show(chat_id, self._USERNAME_PROMPT, BACK_HOME)
            return

        if credential:
            await self._submit_credential(chat_id, tg_user_id, credential)
            return

        self._pending[self._pkey(chat_id)] = (
            "credential", time.time() + PENDING_TTL, {})
        await self._show(chat_id, self._credential_prompt(), BACK_HOME)

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
            self._pending.pop(self._pkey(chat_id), None)
            await self._show(chat_id, "🚫 注册暂时不可用，请稍后再试。",
                            self.guest_menu())
            return
        if not admission.allowed:
            # The conversation stays open: a mistyped code should cost one
            # message, not the whole flow.
            await self._show(
                chat_id,
                f"❌ {admission.reason}\n\n请重新发送邀请码或卡密，或点下面的按钮返回。",
                BACK_HOME)
            return
        self._pending[self._pkey(chat_id)] = (
            "username", time.time() + PENDING_TTL, {"admission": admission})
        await self._show(chat_id, f"✅ {admission.reason}\n\n" + self._USERNAME_PROMPT,
                        BACK_HOME)

    async def _finish_registration(self, chat_id: Any, tg_user_id: str,
                                   tg_username: str, username: str,
                                   admission: Any = None) -> None:
        # Chats run concurrently, but the last slot and a single-use invitation
        # must not be promised twice. No SQLite transaction crosses an await.
        async with self._registration_lock:
            await self._finish_registration_locked(
                chat_id, tg_user_id, tg_username, username, admission)

    def _fresh_admission(self, tg_id: str, admission: Any) -> Any:
        if self._member_for_chat(tg_id):
            raise ConfigError('当前 Telegram 已有账号，不能覆盖绑定。')
        if self._registration is None:
            return admission  # compatibility for installations without channels
        if admission is None:
            raise ConfigError('注册资格已失效，请重新开始。')
        fresh = self._resolve(tg_id, admission.credential)
        if (fresh is None or not fresh.allowed or fresh.as_dict() != admission.as_dict()
                or fresh.credential != admission.credential):
            raise ConfigError('注册资格已变化或失效，请重新开始。')
        return fresh

    async def _rollback_registration(self, user_id: str) -> None:
        # Only a newly created remote ID reaches this path. Never delete an
        # existing membership or let an adapter exception disclose credentials.
        ok = False
        with contextlib.suppress(Exception):
            ok = await self._emby.delete_user(user_id)
        if not ok:
            self._last_error = '注册未完成，Emby 清理未确认，请管理员核查'
            if self._db is not None:
                self._members.audit('telegram', 'registration.cleanup_failed', user_id,
                                    'new remote account requires manual cleanup', ok=False)

    async def _finish_registration_locked(self, chat_id: Any, tg_user_id: str,
                                          tg_username: str, username: str,
                                          admission: Any) -> None:
        username = username.strip()
        if not USERNAME_RE.match(username):
            await self._show(chat_id, '❌ 用户名不符合要求：3–20 字符、字母开头、只含字母数字下划线。\n请重新发送一个。', BACK_HOME)
            return
        blocked = await self._registration_blocked(tg_user_id)
        try:
            if blocked:
                raise ConfigError(blocked)
            admission = self._fresh_admission(tg_user_id, admission)
        except ConfigError as exc:
            self._pending.pop(self._pkey(chat_id), None)
            await self._show(chat_id, escape(str(exc)), self.guest_menu())
            return
        await self._show(chat_id, '⏳ 正在创建账号…', BACK_HOME)
        password = generate_password()
        created = None
        # If shutdown cancels creation, settle the in-flight operation so the
        # returned ID can be cleaned up rather than becoming a passwordless orphan.
        creation = asyncio.create_task(self._emby.create_user(username))
        try:
            created = await asyncio.shield(creation)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                created = await creation
                if created and created.get('Id') and not self._members.get(str(created['Id'])):
                    await self._rollback_registration(str(created['Id']))
            raise
        except Exception:  # noqa: BLE001 - adapter failures must not expose secrets
            self._last_error = '注册创建请求失败'
        if not created or not created.get('Id'):
            self._pending.pop(self._pkey(chat_id), None)
            await self._show(chat_id, '❌ 创建失败，可能是用户名已被占用。请重新注册。', self.guest_menu())
            return
        emby_id = str(created['Id'])
        if self._members.get(emby_id):
            self._last_error = '注册返回了现有账号，未修改密码或绑定'
            await self._show(chat_id, '❌ 创建未确认，请联系管理员。', self.guest_menu())
            return
        committed = False
        receipt_id = None
        try:
            setting = asyncio.create_task(self._emby.set_user_password(emby_id, password))
            try:
                password_ok = await asyncio.shield(setting)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await setting
                raise
            if not password_ok:
                raise ConfigError('密码设置未成功，注册未完成。')
            blocked = await self._registration_blocked(tg_user_id)
            if blocked:
                raise ConfigError(blocked)
            cfg = self._cfg()
            now = int(time.time())
            days = int(getattr(admission, 'days', cfg.get('register_days') or 0))
            group_id = str(getattr(admission, 'group_id', cfg.get('default_group_id') or ''))
            payload = {'status': 'active', 'register_via': getattr(admission, 'via', 'admin'),
                       'inviter_id': getattr(admission, 'inviter_id', ''), 'register_at': now}
            if group_id:
                payload['group_id'] = group_id
            if days > 0:
                payload['expires_at'] = now + days * 86400
            if self._db is not None:
                with self._db.write() as conn:
                    admission = self._fresh_admission(tg_user_id, admission)
                    used, cap = self.registration_slots()
                    if cap and used >= cap:
                        raise ConfigError('注册名额已满。')
                    member = self._members.upsert(emby_id, username, payload, actor='telegram', conn=conn)
                    conn.execute('UPDATE members SET tg_user_id=?,tg_username=?,tg_bound_at=? WHERE emby_user_id=?',
                                 (tg_user_id, tg_username, now, emby_id))
                    self._members.audit('telegram', 'member.telegram.bind', emby_id, 'linked', conn=conn)
                    if (admission is not None and self._registration is not None
                            and not self._registration.consume(admission, emby_id, conn=conn)):
                        raise ConfigError('注册资格已被使用或撤回，注册未完成。')
                    if self._gift_receipts:
                        group = self._groups.get(group_id) if self._groups and group_id else None
                        receipt_id = self._gift_receipts.stage(conn, admission, member,
                            str((group or {}).get('name') or group_id or '用户组'))
            else:
                member = self._members.upsert(emby_id, username, payload, actor='telegram')
                self._members.bind_telegram(emby_id, tg_user_id, tg_username, actor='telegram')
            committed = True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never echo credential-bearing adapter errors
            await self._show(chat_id, '❌ 注册未完成，未消费注册资格；请重试或联系管理员。', self.guest_menu())
            return
        finally:
            self._pending.pop(self._pkey(chat_id), None)
            if not committed:
                await self._rollback_registration(emby_id)
        server = str(cfg.get('emby_public_url') or '').strip()
        expires = (member or {}).get('expires_at_effective', (member or {}).get('expires_at'))
        lines = ['✅ <b>注册成功</b>\n', f'用户名：<code>{username}</code>',
                 f'密码：<code>{password}</code>']
        if server:
            lines.append('服务器：' + escape(server))
        lines.append('有效期：' + _fmt_expiry(expires))
        lines.append('\n<i>请立刻保存密码，这条消息不会再发第二次。</i>')
        await self._show(chat_id, '\n'.join(lines), self.member_menu())
        if receipt_id:
            try:
                receipt = await self._gift_receipts.deliver(receipt_id)
                if receipt and receipt['status'] != 'sent':
                    await self.send(chat_id, '账号注册成功。群回执暂未送达，恢复群权限后可重试一次。',
                        [[{'text': '重试群回执一次', 'callback_data': f'gift_receipt_retry:{receipt_id}'}]])
            except Exception:  # noqa: BLE001 - registration already committed; never claim rollback
                self._last_error = '注册成功，群回执未确认；请在私聊 /start 查看回执'
                await self.send(chat_id, '账号注册成功。群回执尚未确认，可发送 /start 查看回执。')

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
                notice = f"❌ {_public_error(exc)}\n\n"

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
                    f"{when} · {escape(str(row.get('reason_label') or row.get('reason')))} "
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
            await self._edit(chat_id, message_id, f"❌ 签到失败：{_public_error(exc)}",
                             self.member_menu())
            return
        if not result.get("ok"):
            await self._edit(
                chat_id, message_id,
                f"📅 {escape(str(result.get('reason') or '今天已签到'))}\n\n"
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

    def _node_snapshot(self) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            if self._scheduler is not None:
                nodes = list(self._scheduler.snapshot() or [])
        return nodes

    def _online_plays(self, nodes: list[dict[str, Any]] | None = None) -> int | None:
        total = 0
        known = False
        for node in nodes if nodes is not None else self._node_snapshot():
            if node.get("active_streams") is None:
                continue
            known = True
            if node.get("enabled", True) and not node.get("manually_disabled"):
                total += max(0, int(node.get("active_streams") or 0))
        return total if known else None

    def _load_lines(self) -> list[str]:
        """Node utilisation plus live play counts.

        Internal node names stay here: member-facing addresses belong in
        the configured playback-line list.
        """
        nodes = self._node_snapshot()
        if not nodes:
            return ["暂无节点水位。"]
        rows = []
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
            line = f"{escape(str(node.get('name') or '-'))} · {mark}"
            if node.get("active_streams") is not None:
                line += f" · {max(0, int(node['active_streams']))} 路"
            rows.append(line)
        rows.append("\n<i>水位越低越空闲，系统会自动为你选择线路。</i>")
        return rows

    async def _nodes_text(self) -> str:
        """Member-facing playback addresses, then optional node load.

        Custom lines are operator copy: labels and URLs are escaped, never
        treated as Telegram HTML. An empty list keeps the previous fallback
        (public Emby URL plus load) so existing deployments do not go blank.
        """
        cfg = self._cfg()
        custom = list(cfg.get("playback_lines") or [])
        note = str(cfg.get("playback_lines_note") or "").strip()
        show_load = bool(cfg.get("playback_lines_show_load", True))
        parts = ["🌐 <b>播放线路</b>\n"]
        if custom:
            for item in custom:
                parts.append(f"<b>{escape(str(item.get('label') or ''))}</b>")
                parts.append(f"<code>{escape(str(item.get('url') or ''))}</code>")
                hint = str(item.get("hint") or "").strip()
                if hint:
                    parts.append(f"<i>{escape(hint)}</i>")
                parts.append("")
        else:
            server = str(cfg.get("emby_public_url") or "").strip()
            if server:
                parts.append(f"服务器：{escape(server)}\n")
            elif not show_load:
                return "🌐 <b>播放线路</b>\n\n暂无线路信息。"
        if note:
            parts.append(escape(note).replace("\n", "\n"))
            parts.append("")
        online = self._online_plays()
        if online is not None:
            parts.append(f"当前在线：<b>{online}</b> 路播放")
            parts.append("")
        if show_load:
            if custom or str(cfg.get("emby_public_url") or "").strip() or online is not None:
                parts.append("节点水位")
            parts.extend(self._load_lines())
        text = "\n".join(parts).strip()
        return text if text != "🌐 <b>播放线路</b>" else "🌐 <b>播放线路</b>\n\n暂无线路信息。"

    def _watch_text(self, member: dict[str, Any]) -> str:
        if self._stats is None or not hasattr(self._stats, 'watch_summary'):
            return "观看统计：暂不可用"
        try:
            s = self._stats.watch_summary(str(member['emby_user_id']))
        except Exception:
            return "观看统计：暂不可用"
        start = time.strftime('%Y-%m-%d', time.localtime(s['first_at'])) if s.get('first_at') else '尚无记录'
        def window(key: str, incomplete: str) -> str:
            value = duration(s[key])
            if s.get(incomplete):
                return f'已确认 {value}，另有跨界历史无法拆分' if s[key] else '有跨界历史，时长无法完整还原'
            return value
        return (f"近24小时观看：{window('seconds_24h', 'incomplete_24h')}\n"
                f"近30天观看：{window('seconds_30d', 'incomplete_30d')}\n"
                f"累计已记录：{duration(s['recorded_seconds'])}\n"
                f"统计起点：{start}")

    def _usage_text(self, member: dict[str, Any]) -> str:
        lines = [self._whitelist_decoration(member) + "📊 <b>用量与观看</b>\n", *quota_lines(member, public=_GROUP.get()),
                 '', *bandwidth_lines(member), '']
        streams, devices = member.get('max_streams'), member.get('max_devices')
        if streams not in (None, ''):
            lines.append(f"同时播放：{streams} 路")
        if devices not in (None, ''):
            lines.append(f"已登记设备：{member.get('device_count', 0)} / {devices}")
        lines.extend([f"有效期：{_fmt_expiry(member.get('expires_at_effective', member.get('expires_at')))}", "", self._watch_text(member)])
        return '\n'.join(lines)

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
                f"· <b>{escape(str(item['name']))}</b> · {item['cost']} 分"
                + (f"\n  {escape(str(item['description']))}" if item.get("description") else ""))
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
        member = self._member_for_chat(_ACTOR.get() or str(chat_id))
        self._pending[self._pkey(chat_id)] = ('shop_confirm', time.time() + 120,
            {'item': item, 'user_id': (member or {}).get('emby_user_id'), 'message_id': message_id})
        await self._edit(
            chat_id, message_id,
            f"确定用 <b>{item['cost']}</b> 积分兑换 <b>{escape(str(item['name']))}</b>？\n\n"
            f"内容：{item['amount']}{escape(str(item.get('unit') or ''))} · "
            f"{escape(str(item.get('kind_label') or ''))}",
            [[{"text": "✅ 确认兑换", "callback_data": f"buyok:{item['id']}"},
              {"text": "取消", "callback_data": "shop"}]])

    async def _shop_redeem(self, chat_id: Any, message_id: int,
                           member: dict[str, Any], item_id: str) -> None:
        if self._shop is None:
            await self._edit(chat_id, message_id, "商城暂未开放。", self.bag_menu())
            return
        waiting = self._pending.pop(self._pkey(chat_id), None)
        extra = waiting[2] if waiting else {}
        if (not waiting or waiting[0] != 'shop_confirm' or waiting[1] <= time.time()
                or extra.get('user_id') != member.get('emby_user_id')
                or extra.get('message_id') != message_id
                or str((extra.get('item') or {}).get('id')) != str(item_id)):
            return
        if self._shop.get(int(item_id)) != extra.get('item'):
            await self._edit(chat_id, message_id, '商品已变化，请重新确认。', self.bag_menu())
            return
        try:
            result = self._shop.redeem(
                str(member.get("emby_user_id") or ""), int(item_id),
                actor="telegram")
        except Exception as exc:  # noqa: BLE001 - the reason is for the member
            await self._edit(chat_id, message_id, f"❌ 兑换失败：{_public_error(exc)}",
                             self.bag_menu())
            return
        notice = await self._after_member_change(member)
        item = result.get("item") or {}
        await self._edit(
            chat_id, message_id,
            f"✅ <b>兑换成功</b>\n\n商品：{escape(str(item.get('name')))}\n"
            f"发放：{escape(str(result.get('granted')))}\n"
            f"消耗：{result.get('cost')} 分\n"
            f"余额：<b>{result.get('balance')}</b>{notice}",
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
                f"{when} · {escape(str(row.get('item_name') or '-'))} · -{row.get('cost')} 分")
        await self._edit(chat_id, message_id, "\n".join(lines), self.bag_menu())

    # -- transfer -------------------------------------------------------------

    async def _transfer_start(self, chat_id: Any, message_id: int) -> None:
        if not self._plugin_on("points_transfer"):
            await self._edit(chat_id, message_id, "转账功能未开启。",
                             self.member_menu())
            return
        self._pending[self._pkey(chat_id)] = (
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
            await self._show(chat_id, f"❌ 找不到用户「{escape(username)}」，请确认后重试。",
                             BACK_HOME)
            return
        if str(target.get("emby_user_id")) == str(member.get("emby_user_id")):
            await self._show(chat_id, "❌ 不能转给自己。", BACK_HOME)
            return
        self._pending[self._pkey(chat_id)] = (
            "transfer_amount", time.time() + PENDING_TTL,
            {"to_id": str(target.get("emby_user_id")),
             "to_name": str(target.get("username") or username)})
        balance = self._balance(str(member.get("emby_user_id") or ""))
        await self._show(
            chat_id,
            f"收款人：<b>{escape(str(target.get('username') or username))}</b>\n"
            f"你的余额：<b>{balance}</b>\n\n请发送要转多少积分。\n\n"
            "<i>发送 /start 可取消。</i>",
            BACK_HOME)

    async def _transfer_pick_amount(self, chat_id: Any,
                                    member: dict[str, Any],
                                    extra: dict[str, Any], raw: str) -> None:
        plugin = self._plugin("points_transfer")
        if plugin is None:
            self._pending.pop(self._pkey(chat_id), None)
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
        self._pending[self._pkey(chat_id)] = (
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
        waiting = self._pending.pop(self._pkey(chat_id), None)
        plugin = self._plugin("points_transfer")
        if (not waiting or waiting[0] != "transfer_confirm" or waiting[1] <= time.time()
                or plugin is None or not self._plugin_on('points_transfer')):
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
            await self._edit(chat_id, message_id, f"❌ 转账失败：{_public_error(exc)}",
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
                    f"💰 收到 <b>{escape(str(member.get('username') or '一位成员'))}</b> "
                    f"转来的 <b>{result.get('received')}</b> 积分\n"
                    f"当前余额：<b>{result.get('to_balance')}</b>")

    # -- claim / rebind requests ---------------------------------------------

    def _create_request(self, kind: str, tg_user_id: str, tg_username: str,
                        wanted: str) -> bool:
        # Retired unverified claim/rebind entry point. Only authenticated
        # Emby IDs may enter RebindingService.create().
        return False

    def pending_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        return self._db.query(
            "SELECT * FROM tg_requests WHERE status='pending' AND kind='rebind' "
            "AND verified_at IS NOT NULL AND expires_at>? ORDER BY created_at ASC LIMIT ?",
            (int(time.time()), max(1, min(limit, 500))))

    def review_request(self, request_id: int, approve: bool,
                       reviewer: str = "operator") -> dict[str, Any]:
        """Retired: callers must use review_rebind with live presence checks."""
        raise ValueError("认领已停用，请使用已验证的 TG 换绑审核。")

    # -- rankings -------------------------------------------------------------

    def _rankings_keyboard(self, days: int, member: dict[str, Any] | None
                            ) -> list[list[dict[str, str]]]:
        today = "● 今日" if days <= 1 else "今日"
        month = "● 近 30 天" if days > 1 else "近 30 天"
        return [
            [{"text": today, "callback_data": "top:1"},
             {"text": month, "callback_data": "top:30"}],
            [{"text": "◀ 返回", "callback_data": "home"}],
        ]

    def _rankings_text(self, days: int = 1) -> str:
        """Scheduled heat bulletin: movie/episode plays, not watch-time.

        Watch-time is a separate post covering every member with sampled
        seconds. days=1 is yesterday's complete local calendar day.
        """
        days = max(1, int(days or 1))
        stamp = ranking_stamp(days)
        if days <= 1:
            title = "播放日榜"
        elif days <= 7:
            title = "播放周榜"
        else:
            title = f"近 {days} 天播放榜"
        lines = [f"🏆 <b>{title}</b>  {stamp}\n"]
        movies: list[dict[str, Any]] = []
        shows: list[dict[str, Any]] = []
        if self._stats is not None:
            with contextlib.suppress(Exception):
                movies, shows = self._stats.top_titles_split(
                    days=days, limit=10, calendar=True)
        if movies:
            lines.append("<b>▎电影</b>")
            for i, row in enumerate(movies, 1):
                lines.append(
                    f"{i}. {escape(str(row.get('title') or '—'))}\n"
                    f"播放次数: {int(row.get('plays') or 0)}  时长: "
                    f"{duration(int((row.get('hours') or 0) * 3600))}")
            lines.append("")
        if shows:
            lines.append("<b>▎电视剧</b>")
            for i, row in enumerate(shows, 1):
                lines.append(
                    f"{i}. {escape(str(row.get('title') or '—'))}\n"
                    f"播放次数: {int(row.get('plays') or 0)}  时长: "
                    f"{duration(int((row.get('hours') or 0) * 3600))}")
            lines.append("")
        while lines and not lines[-1]:
            lines.pop()
        if len(lines) == 1:
            lines.append("暂时还没有排行数据。")
        return "\n".join(lines)

    def _watch_rank_pages(self, days: int = 1, page_size: int = 10) -> list[str]:
        """Every member with watch time, ten names per page like EmbyBoss."""
        days = max(1, int(days or 1))
        stamp = ranking_stamp(days)
        heading = f"▎🏆 <b>{days} 天观影榜</b>"
        rows: list[dict[str, Any]] = []
        if self._stats is not None:
            with contextlib.suppress(Exception):
                rows = self._stats.top_users(days=days, limit=5000, calendar=True)
        medals = ("🥇", "🥈", "🥉")
        pages: list[str] = []
        size = max(1, int(page_size or 10))
        if not rows:
            return [f"{heading}\n\n暂时还没有排行数据。\n\n#UPlaysRank  {stamp}"]
        for start in range(0, len(rows), size):
            chunk = rows[start:start + size]
            lines = [heading, ""]
            for offset, row in enumerate(chunk):
                rank = start + offset + 1
                medal = medals[rank - 1] if rank <= 3 else "🏅"
                name = watch_rank_mention(row)
                if str(row.get("group_id") or "") == WHITELIST_GROUP_ID:
                    name += " · 💠白名单"
                lines.append(
                    f"{medal}<b>第{rank}名</b> | {name}\n"
                    f"  观影时长 | {duration(int(row.get('seconds') or (row.get('hours') or 0) * 3600))}")
            lines.append("")
            lines.append(f"#UPlaysRank  {stamp}")
            pages.append("\n".join(lines))
        return pages

    @staticmethod
    def _watch_rank_keyboard(page: int, total: int, days: int) -> list[list[dict[str, str]]]:
        """Numbered pager like EmbyBoss plays_list_button."""
        page = max(1, int(page or 1))
        total = max(1, int(total or 1))
        days = max(1, int(days or 1))
        if total <= 8:
            numbers = list(range(1, total + 1))
        else:
            window = {1, total, page, page - 1, page + 1, page - 2, page + 2}
            numbers = [n for n in range(1, total + 1) if n in window]
            if page > 4:
                numbers = [1, 0] + [n for n in numbers if n != 1]
            if page < total - 3:
                numbers = [n for n in numbers if n != total] + [0, total]
        row: list[dict[str, str]] = []
        for n in numbers:
            if n == 0:
                row.append({"text": "…", "callback_data": f"urank:{page}_{days}"})
                continue
            label = f"·{n}·" if n == page else str(n)
            row.append({"text": label, "callback_data": f"urank:{n}_{days}"})
        extra = [{"text": "❌ 关闭", "callback_data": "urank_close"}]
        if total > 5:
            if page - 5 >= 1:
                extra.append({"text": "⏮️ -5", "callback_data": f"urank:{page - 5}_{days}"})
            if page + 5 <= total:
                extra.append({"text": "⏭️ +5", "callback_data": f"urank:{page + 5}_{days}"})
        return [row, extra]

    @staticmethod
    def _split_bulletin(text: str, limit: int = 1000) -> list[str]:
        rest = text or ""
        parts: list[str] = []
        while rest:
            if len(rest) <= limit:
                parts.append(rest)
                break
            cut = rest.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            parts.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip("\n")
        return parts or [""]

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
        self._pending[self._pkey(chat_id)] = ("request_link", time.time() + PENDING_TTL, {})
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
            resolved_type, meta = await self._tmdb.resolve(media_type, tmdb_id)
            # Only a bare number is ambiguous. An explicit /movie/ URL must
            # not turn into an unrelated TV work sharing its numeric ID.
            if re.fullmatch(r'#?\d+', text.strip()) or resolved_type == media_type:
                media_type = resolved_type
            else:
                meta = None

        extra = {"media_type": media_type, "tmdb_id": tmdb_id}
        self._pending[self._pkey(chat_id)] = (
            "request_confirm", time.time() + PENDING_TTL, extra)
        keyboard = [[{"text": "✅ 确认求片", "callback_data": "req_ok"},
                     {"text": "✖ 取消", "callback_data": "home"}]]

        if meta:
            year = meta.get("year")
            poster = poster_url(str(meta.get("poster_path") or ""))
            caption = (
                "🎬 <b>确认求片</b>\n\n"
                f"<b>{escape(str(meta.get('title') or tmdb_id))}</b>"
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
        waiting = self._pending.pop(self._pkey(chat_id), None)
        if not waiting or waiting[0] != "request_confirm":
            await self._edit(chat_id, message_id, "这条求片会话已经过期了，请重新开始。",
                             self._with_admin_row(self.member_menu(), member))
            return
        extra = waiting[2]
        user_id = str(member.get("emby_user_id"))
        try:
            request = await self._requests.create(
                user_id, extra.get("media_type", "movie"),
                int(extra.get("tmdb_id") or 0), confirmed_type=True)
        except RequestError as exc:
            await self._show(chat_id, f"❌ {_public_error(exc)}",
                             self._with_admin_row(self.member_menu(), member))
            return

        left = self._requests.remaining(user_id)
        await self._show(
            chat_id,
            "✅ <b>已提交</b>\n\n"
            f"编号 <b>#{request['id']}</b> · {escape(str(request['display_title']))}\n"
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
                    f"{mark} #{row['id']} {escape(str(row['display_title']))} · "
                    f"{row['status_label']}")
                note = str(row.get("result_note") or "")
                if note and row.get("status") == "rejected":
                    lines.append(f"    <i>{escape(str(note))}</i>")
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
            f"{escape(str(request.get('display_title') or ''))}\n"
            f"类型：{request.get('media_label') or '-'}\n"
            f"TMDB：<code>{request.get('tmdb_id')}</code>\n"
            f"求片人：{escape(str(request.get('username') or '-'))}")
        note = str(request.get("note") or "")
        if note:
            body += f"\n备注：{escape(str(note))}"

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
            await self._answer_callback(callback_id, f"已被 {escape(str(holder))} 接单")
            await self._retract_notices(request_id, holder,
                                        skip_chat=str(chat_id))
            return

        await self._answer_callback(callback_id, "接单成功")
        request = result["request"]
        await self._edit(
            chat_id, message_id,
            f"✋ <b>已接单 #{request_id}</b>\n\n"
            f"{escape(str(request.get('display_title') or ''))}\n"
            f"TMDB：<code>{request.get('tmdb_id')}</code>\n"
            f"求片人：{escape(str(request.get('username') or '-'))}\n\n"
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
                f"🎬 <b>求片 #{request_id}</b>\n\n已由 {escape(str(holder))} 接单。")
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
            self._pending[self._pkey(chat_id)] = (
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
            f"✅ <b>已完成 #{request_id}</b>\n\n{escape(str(request.get('display_title') or ''))}")
        await self.notify_request_resolved(request)

    async def _request_reason(self, chat_id: Any, member: dict[str, Any],
                              extra: dict[str, Any], text: str) -> None:
        self._pending.pop(self._pkey(chat_id), None)
        request_id = int(extra.get("request_id") or 0)
        try:
            result = self._requests.resolve(
                request_id, str(member.get("emby_user_id")), done=False,
                note=text)
        except RequestError as exc:
            await self.send(chat_id, f"❌ {_public_error(exc)}", self.member_menu())
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
            body = (f"✅ <b>求片已处理</b>\n\n你求的《{escape(str(title))}》已经处理好了，"
                    "请耐心等待入库。")
            note = str(request.get("result_note") or "")
            if note:
                body += f"\n\n<i>{escape(str(note))}</i>"
        else:
            reason = str(request.get("result_note") or "暂时无法处理")
            body = (f"❌ <b>求片无法处理</b>\n\n你求的《{escape(str(title))}》暂时没能处理。\n\n"
                    f"原因：{escape(str(reason))}")
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

    def _admin_panel(self, chat_id: Any, message_id: Any = None) -> dict[str, Any] | None:
        mid = message_id or self._panel.get(self._pkey(chat_id))
        return self._admin_panels.get((str(chat_id), int(mid))) if mid else None

    def _remember_admin_panel(self, chat_id: Any, target: dict[str, Any], actor: str) -> None:
        mid = self._panel.get(self._pkey(chat_id))
        if not mid:
            return  # a failed first send must not create an actionable card
        owner = _ACTOR.get() or str(chat_id)
        reviewer = self._member_for_chat(owner) or {}
        key = (str(chat_id), int(mid))
        self._admin_panels[key] = {
            'owner': owner, 'reviewer_id': reviewer.get('emby_user_id'),
            'session': self._pkey(chat_id), 'user_id': str(target.get('emby_user_id') or ''),
            'tg_id': str(target.get('tg_user_id') or ''), 'actor': actor,
            'expires': time.time() + PENDING_TTL,
            'pending': self._pending.get(self._pkey(chat_id)),
        }

    def _admin_panel_authorized(self, panel: dict[str, Any], tg_id: str) -> bool:
        reviewer = self._member_for_chat(tg_id)
        return (panel['owner'] == str(tg_id) and panel['session'] == _SESSION.get()
                and self.is_admin(reviewer)
                and reviewer['emby_user_id'] == panel['reviewer_id'])

    def _save_admin_pending(self, chat_id: Any) -> None:
        panel = self._admin_panel(chat_id)
        if panel:
            panel['pending'] = self._pending.get(self._pkey(chat_id))

    async def _return_admin_target(self, chat_id: Any, message_id: int,
                                    notice: str = '') -> None:
        panel = self._admin_panel(chat_id, message_id)
        if not panel:
            return
        self._pending.pop(self._pkey(chat_id), None)  # returning cancels a draft/confirmation
        if panel['user_id']:
            target = self._members.get(panel['user_id'])
            if target:
                await self._admin_show_user(chat_id, target, panel['actor'], notice)
                return
            await self._edit(chat_id, message_id, '管理目标已不存在；未执行其他账号的操作。',
                             [[{'text': '管理入口', 'callback_data': 'admin_root'},
                               {'text': '关闭', 'callback_data': 'panel_close'}]])
        else:
            await self._admin_lookup(chat_id, panel['tg_id'], panel['actor'])

    def _target_back(self) -> list[list[dict[str, str]]]:
        return [[{'text': '◀ 返回目标账号 / 取消', 'callback_data': 'admin_card'}]]

    def _private_button(self, payload: str = '', label: str = '在私聊打开') -> list[list[dict[str, str]]]:
        payload = payload if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', payload) else ''
        link = self._private_link(payload)
        return [[{'text': label, 'url': link}]] if link else []

    def admin_menu(self) -> list[list[dict[str, str]]]:
        """What embyboss put on the 管理 panel, minus the slash-command memory."""
        if _GROUP.get():
            return [[{'text': '🔍 查找其他账号', 'callback_data': 'admin_find'},
                     {'text': '关闭', 'callback_data': 'panel_close'}],
                    *self._private_button('manage', '完整管理 · 私聊')]
        return [
            [{"text": "🔍 查找用户", "callback_data": "admin_find"},
             {"text": "🎁 赠送开号", "callback_data": "admin_find"}],
            [{"text": "📋 求片队列", "callback_data": "admin_reqs"},
             {"text": "🎟 生成卡密", "callback_data": "admin_code"}],
            [{"text": "✅ 预授权注册", "callback_data": "admin_auth"}],
            [{"text": "◀ 我的首页", "callback_data": "personal_home"}],
        ]

    def _user_admin_keyboard(self, user_id: str) -> list[list[dict[str, str]]]:
        rows = [
            [{'text': '⏳ 续期', 'callback_data': 'admin_renew'},
             {'text': '👥 用户组', 'callback_data': 'admin_groups'}],
            [{'text': '💰 调整积分', 'callback_data': 'admin_score'},
             {'text': '📊 使用详情', 'callback_data': 'admin_usage'}],
            [{'text': '🗑 删除账号', 'callback_data': 'admin_rm'},
             {'text': '🔄 刷新目标账号', 'callback_data': 'admin_card'}],
            [{'text': '◀ 管理入口', 'callback_data': 'admin_root'},
             {'text': '关闭卡片', 'callback_data': 'panel_close'}],
        ]
        rows.insert(1, [{'text': '💠 直接授予白名单', 'callback_data': 'admin_prouser'}])
        if _GROUP.get():
            rows += self._private_button('manage_' + user_id, '完整管理 · 私聊')
        else:
            rows.insert(2, [{'text': '📋 详细资料 / 绑定', 'callback_data': 'admin_binding'}])
        return rows

    def _user_card(self, target: dict[str, Any]) -> str:
        return self._account_card(target, public=_GROUP.get(), managing=True)

    def _admin_details(self, target: dict[str, Any]) -> str:
        if _GROUP.get():
            return self._account_card(target, public=True, managing=True)
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
            self._whitelist_decoration(target)
            + f"🛠 <b>管理目标 · {escape(str(target.get('username') or '-'))}</b>\n\n"
            f"状态：{self._status_label(target)}\n"
            f"用户组：{self._group_label(target)}\n"
            f"有效期：{_fmt_expiry(target.get('expires_at_effective', target.get('expires_at')))}\n"
            f"积分：{self._balance(user_id)}\n"
            f"注册渠道：{escape(str(target.get('register_via') or 'legacy'))}\n"
            f"邀请人：{escape(str(inviter.get('username') or '—'))}\n"
            f"下级：{invitee_count} 人\n"
            f"Telegram：{escape(str(target.get('tg_user_id') or '未关联'))}\n"
            f"设备数：{devices}\n"
            f"最近活跃："
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(seen)) if seen else '—'}\n"
            f"求片剩余：{self._remaining_text(left)}\n\n"
            + self._usage_brief(target) + "\n\n" + self._watch_text(target)
        )

    def _hold_admin_user(self, chat_id: Any, target: dict[str, Any],
                         actor: str) -> None:
        self._pending[self._pkey(chat_id)] = (
            "admin_user", time.time() + PENDING_TTL,
            {"user_id": str(target.get("emby_user_id")),
             "username": str(target.get("username") or ""),
             "actor": actor,
             "message_id": self._panel.get(self._pkey(chat_id))})

    async def _admin_home(self, chat_id: Any, message_id: int,
                          member: dict[str, Any] | None) -> None:
        if not self.is_admin(member):
            await self._edit(chat_id, message_id, "⛔ 无权限。")
            return
        self._pending.pop(self._pkey(chat_id), None)
        await self._edit(
            chat_id, message_id,
            "🛠 <b>管理</b>\n\n"
            "查找用户后，续期、换组、加减积分和删除都在该目标的卡片里完成。",
            self.admin_menu() + (self._target_back() if self._admin_panel(chat_id, message_id) else []))

    async def _admin_prompt_find(self, chat_id: Any, message_id: int,
                                 member: dict[str, Any] | None) -> None:
        if not self.is_admin(member):
            await self._edit(chat_id, message_id, "⛔ 无权限。")
            return
        self._pending[self._pkey(chat_id)] = ("admin_find", time.time() + PENDING_TTL, {'message_id': message_id})
        await self._edit(
            chat_id, message_id,
            "🔍 <b>查找用户 / 赠送开号</b>\n\n"
            "请发送 Telegram 数字 ID、Emby 用户名或已绑定的 @Telegram 用户名。\n"
            "也可以转发对方的消息；若隐藏了转发身份，请使用数字 ID。\n\n"
            + ("群聊请回复本条消息提交查询，避免与其他管理卡混淆。\n" if _GROUP.get() else "")
            + "<i>发送 /start 可取消。</i>",
            [[{"text": "◀ 返回管理", "callback_data": "admin"}]])

    async def _admin_lookup(self, chat_id: Any, query: str, actor: str) -> bool | None:
        target = self._find_target(query)
        if target:
            return await self._admin_show_user(chat_id, target, actor)
        tg_id = str(query).strip()
        if not tg_id.isdigit() or not (0 < int(tg_id) < 2**63):
            await self._show(chat_id, "找不到该用户。未注册的人请使用 Telegram 数字 ID。",
                             [[{"text": "◀ 返回管理", "callback_data": "admin"}]])
            return
        tg_id = str(int(tg_id))
        previous = self._admin_panel(chat_id)
        if previous and (previous['user_id'] or previous['tg_id'] != tg_id):
            self._panel.pop(self._pkey(chat_id), None)
        grant = self._registration.get_grant(tg_id) if self._registration else None
        status = "已有待领取资格" if grant and not grant.get("used_at") else "暂无注册资格"
        await self._show(
            chat_id, f"👤 <b>Telegram 用户</b>\n\nID：<code>{tg_id}</code>\n"
            f"账号：尚未注册\n资格：{status}\n\n"
            + ('可在本群赠送专属注册资格，对方领取后前往私聊注册。' if _GROUP.get()
             else "可赠送专属注册资格，由对方自己设置用户名。"),
            [[{"text": "🎁 赠送开号", "callback_data": "admin_gift"}]]
            + [[{'text': '刷新目标', 'callback_data': 'admin_card'},
                {'text': '管理入口', 'callback_data': 'admin_root'}]])
        self._pending[self._pkey(chat_id)] = (
            "admin_person", time.time() + PENDING_TTL,
            {"tg_id": tg_id, "message_id": self._panel.get(self._pkey(chat_id)), "actor": actor})
        self._remember_admin_panel(chat_id, {'tg_user_id': tg_id}, actor)

    async def _admin_gift(self, chat_id: Any, message_id: int,
                          member: dict[str, Any] | None, data: str) -> None:
        if not self.is_admin(member):
            await self._edit(chat_id, message_id, "⛔ 无权限。")
            return
        waiting = self._pending.get(self._pkey(chat_id))
        if (not waiting or waiting[0] not in ("admin_person", "admin_gift_confirm")
                or waiting[1] < time.time()):
            return
        extra = waiting[2]
        if extra.get("message_id") != message_id:
            return
        target = str(extra.get("tg_id") or "")
        if self._member_for_chat(target):
            self._pending.pop(self._pkey(chat_id), None)
            await self._edit(chat_id, message_id, "对方已有账号，不能重复赠送开号。请重新 /kk 查询。")
            return
        if self._registration is None:
            await self._edit(chat_id, message_id, "注册服务不可用。", self.admin_menu())
            return
        if _GROUP.get() and not self._start_link('gift'):
            await self._edit(chat_id, message_id, "暂时无法生成 Bot 私聊入口，未发放资格，请稍后重新查询。")
            return
        group, days = self._registration.gift_terms(target)
        duration = f"{days} 天" if days else "永久"
        group_row = self._groups.get(group) if self._groups and group else None
        group_name = escape(str((group_row or {}).get("name") or group or "未设置"))
        if data == "admin_gift":
            nonce = secrets.token_hex(6)
            self._pending[self._pkey(chat_id)] = (
                "admin_gift_confirm", time.time() + 120,
                {**extra, "nonce": nonce, "group": group, "days": days})
            await self._edit(
                chat_id, message_id,
                f"🎁 <b>赠送开号资格</b>\n\n接收人：<code>{target}</code>\n"
                f"用户组：{group_name}\n权益：{duration}（注册成功后起算）\n\n"
                "只允许该 Telegram 领取，不会立即创建账号。\n"
                + ("确认后在本群发布领取按钮；仍须满足当前注册准入条件。" if _GROUP.get()
                   else "确认后生成链接，由你转发；仍须满足当前注册准入条件。"),
                [[{"text": "确认赠送", "callback_data": f"admin_gift_ok:{nonce}"},
                  {"text": "取消", "callback_data": "admin"}]])
            return
        if waiting[0] != "admin_gift_confirm" or data != f"admin_gift_ok:{extra.get('nonce')}":
            return
        self._pending.pop(self._pkey(chat_id), None)
        if (group, days) != (extra.get("group"), extra.get("days")):
            await self._edit(chat_id, message_id, "注册权益已变化，请重新查找并确认。", self.admin_menu())
            return
        previous = self._registration.get_grant(target)
        try:
            origin = ({'chat_id': str(chat_id), 'message_id': message_id,
                       'thread_id': _THREAD.get(), 'bot_id': self._token().split(':', 1)[0]}
                      if _GROUP.get() else None)
            issued = self._registration.issue_gift(target, self._admin_actor(member, ""), origin=origin)
        except ConfigError as exc:
            await self._edit(chat_id, message_id, f"❌ {escape(str(exc))}", self.admin_menu())
            return
        code = str(issued.get("gift_code") or "")
        if not previous or previous.get('gift_code') != code:
            self._members.audit(self._admin_actor(member, ""), "registration.gift", target,
                                f"group={group} days={days}")
        link = self._start_link(code)
        if _GROUP.get():
            issuer = str(member.get('tg_user_id') or _ACTOR.get())
            issuer_name = str(member.get('tg_username') or '')
            issuer_label = '@' + issuer_name if issuer_name else 'TG ' + issuer
            await self._edit(chat_id, message_id,
                f'🎁 <a href="tg://user?id={escape(issuer, quote=True)}">{escape(issuer_label)}</a> 为 '
                f'<a href="tg://user?id={target}">TG {target}</a> 赠送了一份注册资格\n'
                '点击下方领取，前往机器人完成注册。',
                [[{'text': '领取并注册', 'url': link}]])
            return
        buttons = [[{"text": "🎁 点击领取（仅指定用户）", "url": link}]] if link else []
        buttons.append([{"text": "◀ 返回管理", "callback_data": "admin"}])
        await self._edit(
            chat_id, message_id,
            f"✅ <b>赠送资格已准备好</b>\n\n接收人：<code>{target}</code>\n"
            f"用户组：{group_name} · {duration}\n\n"
            + (f"{escape(link)}\n\n" if link else f"领取码：<code>{code}</code>\n\n")
            + "请将链接或领取码交给对方，别人无法代领。\n"
            "<i>Bot 没有主动私信对方。返回前请先复制链接。</i>", buttons)

    async def _admin_show_user(self, chat_id: Any, target: dict[str, Any],
                               actor: str, notice: str = '') -> bool:
        previous = self._admin_panel(chat_id)
        if previous and previous['user_id'] != str(target.get('emby_user_id') or ''):
            # A card is never silently retargeted; late buttons still name the
            # account originally shown on that message.
            self._panel.pop(self._pkey(chat_id), None)
        self._hold_admin_user(chat_id, target, actor)
        shown = await self._show(chat_id, self._user_card(target) + notice,
                                 self._user_admin_keyboard(str(target.get("emby_user_id"))))
        waiting = self._pending.get(self._pkey(chat_id))
        if waiting and waiting[0] == "admin_user":
            waiting[2]["message_id"] = self._panel.get(self._pkey(chat_id))
        self._remember_admin_panel(chat_id, target, actor)
        return shown

    def _admin_held_target(self, chat_id: Any) -> tuple[dict[str, Any] | None, str]:
        panel = self._admin_panel(chat_id)
        if panel and panel['expires'] > time.time():
            return self._members.get(panel['user_id']), panel['actor']
        waiting = self._pending.get(self._pkey(chat_id))
        if not waiting or waiting[0] != "admin_user" or waiting[1] <= time.time():
            return None, ""
        extra = waiting[2] or {}
        target = self._members.get(str(extra.get("user_id") or ""))
        return target, str(extra.get("actor") or "tg:admin")

    async def _admin_handle_text(self, chat_id: Any, member: dict[str, Any] | None,
                                 kind: str, extra: dict[str, Any], text: str) -> None:
        if not self.is_admin(member):
            self._pending.pop(self._pkey(chat_id), None)
            await self._show(chat_id, "⛔ 无权限。")
            return
        actor = self._admin_actor(member or {}, "")
        if kind == "admin_find":
            await self._admin_lookup(chat_id, text, actor)
            return
        if kind == "admin_renew_days":
            if not text.lstrip("-").isdigit():
                await self._show(chat_id, "请发送天数，例如 <code>30</code>。",
                                 self._target_back())
                return
            target = self._members.get(str(extra.get("user_id") or ""))
            if not target:
                await self._show(chat_id, "这个用户已经不在了。", self.admin_menu())
                return
            try:
                updated = self._members.renew(str(target.get("emby_user_id")),
                                              int(text), actor=actor)
            except (ValueError, KeyError) as exc:
                await self._show(chat_id, '❌ ' + _public_error(exc), self._target_back())
                return
            notice = await self._after_member_change(target)
            await self._admin_show_user(chat_id, updated or target, actor, notice)
            return
        if kind == "admin_score_delta":
            raw = text.lstrip("+")
            if not raw.lstrip("-").isdigit():
                await self._show(chat_id, "请发送整数，例如 <code>+50</code> 或 <code>-10</code>。",
                                 self._target_back())
                return
            target = self._members.get(str(extra.get("user_id") or ""))
            if not target or self._points is None:
                await self._show(chat_id, "无法调整积分。", self.admin_menu())
                return
            try:
                self._points.add(str(target.get("emby_user_id")), int(raw),
                                 "admin.adjust", ref="tg", actor=actor)
            except ValueError as exc:
                await self._show(chat_id, f"❌ {_public_error(exc)}",
                                 self._target_back())
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
        if data == "admin_prouser":
            await self._grant_whitelist(chat_id, actor, target)
            return
        if data == "admin_usage":
            await self._edit(chat_id, message_id,
                             '🛠 <b>管理目标 · ' + escape(str(target.get('username') or '—')) + '</b>\n\n' + self._usage_text(target),
                             self._target_back())
            return
        if data == "admin_binding":
            await self._edit(chat_id, message_id,
                self._admin_details(target) + '\n\n'
                + "更换 TG 由新 Telegram 私聊验证 Emby 密码后提交申请，管理员在绑定群审核。",
                self._user_admin_keyboard(user_id))
            return
        if data == "admin_groups":
            choices = [{'text': str(g['name']), 'callback_data': 'admin_group_pick:'+g['id']}
                       for g in self._groups.list()] if self._groups else []
            rows = [choices[i:i+2] for i in range(0, len(choices), 2)]
            await self._edit(chat_id, message_id, "👥 <b>选择用户组</b>\n\n新组的计费方式生效；仍计时时默认保留期限。确认前会显示日期变化。", rows + [[{'text':'◀ 返回用户','callback_data':'admin_card'}]])
            return
        if data.startswith('admin_group_pick:'):
            await self._preview_group_change(chat_id, target, actor, data.split(':',1)[1])
            return
        if data.startswith('admin_group_apply:'):
            await self._apply_group_change(chat_id, message_id, target, actor, data)
            return
        if data == "admin_card":
            await self._admin_show_user(chat_id, target, actor)
            return
        if data == "admin_renew":
            self._pending[self._pkey(chat_id)] = (
                "admin_renew_days", time.time() + PENDING_TTL,
                {"user_id": user_id, "actor": actor, "message_id": message_id})
            await self._edit(
                chat_id, message_id,
                f"⏳ 给 <b>{escape(str(target.get('username')))}</b> 续期多少天？\n"
                + ("请回复本条消息发送数字；/cancel 可取消。" if _GROUP.get() else "请发送数字。"),
                self._target_back())
            return
        if data == "admin_pro":
            if self._groups is not None:
                with contextlib.suppress(Exception):
                    self._groups.ensure_whitelist()
            await self._preview_group_change(chat_id, target, actor, WHITELIST_GROUP_ID)
            return
        if data == "admin_rev":
            default_id = self._groups.default_group_id() if self._groups else None
            if not default_id:
                await self._edit(chat_id, message_id, "没有设置默认用户组。",
                                 self._user_admin_keyboard(user_id))
                return
            await self._preview_group_change(chat_id, target, actor, default_id)
            return
        if data == "admin_score":
            self._pending[self._pkey(chat_id)] = (
                "admin_score_delta", time.time() + PENDING_TTL,
                {"user_id": user_id, "actor": actor, "message_id": message_id})
            await self._edit(
                chat_id, message_id,
                f"💰 给 <b>{escape(str(target.get('username')))}</b> 加减多少积分？\n"
                "例如 <code>+50</code> 或 <code>-10</code>。"
                + ("\n请回复本条消息输入；/cancel 可取消。" if _GROUP.get() else ""),
                self._target_back())
            return
        if data == "admin_rm":
            await self._cmd_rm(chat_id, actor, [str(target.get("username") or "")])

    def _find_target(self, token: str) -> dict[str, Any] | None:
        """Resolve '@name' or a bare Emby username to a member."""
        name = str(token or "").strip().lstrip("@")
        if not name:
            return None
        if name.isdigit():
            found = self._members.find_by_telegram(name)
            if found:
                return found
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
        self._pending.pop(self._pkey(chat_id), None)
        if _GROUP.get():
            await self._group_start(chat_id, payload)
            return
        token = str(payload or "").strip()
        if (token and looks_like_credential(token)
                and not self._member_for_chat(tg_user_id)):
            await self._start_registration(chat_id, tg_user_id, credential=token)
            return
        await self._show_home(chat_id, tg_user_id, tg_name)

    async def _group_start(self, chat_id: Any, payload: str = "") -> None:
        supported = payload in ('account', 'manage') or bool(re.fullmatch(r'(manage_[A-Za-z0-9_-]+|person_[0-9]+)', payload))
        link = self._private_link(payload if looks_like_credential(payload) or supported else "")
        keyboard = [[{"text": "打开私聊", "url": link}]] if link else None
        await self._show(
            chat_id,
            "请私聊我打开菜单。" + (f"\n{escape(link)}" if link else ""),
            keyboard)

    def _group_help_text(self, member: dict[str, Any] | None) -> str:
        lines = [
            "❓ <b>群内命令</b>",
            "",
            "· /me 我的账号卡：状态、分组、额度与剩余、观看摘要（完整资料请私聊）",
            "· /rank 近 24 小时 / 近 30 天观看时长",
            "· /rules 行为准则",
            "· /start 打开私聊菜单",
        ]
        if self.is_admin(member):
            lines += [
                "",
                "管理员可回复对方消息或带参数使用 /kk /renew /score /prouser /revuser /rmemby。",
            ]
        return "\n".join(lines)

    def _account_card(self, member: dict[str, Any], *, public: bool = False,
                       managing: bool = False) -> str:
        name = escape(str(member.get('username') or '—'))
        badge = self._whitelist_decoration(member) or '👤 '
        heading = f'🛠 <b>管理目标 · {name}</b>' if managing else badge + f'<b>{name}</b>'
        expiry = member.get('expires_at_effective', member.get('expires_at'))
        term = time.strftime('%Y-%m-%d 到期', time.localtime(expiry)) if expiry else '♾ 永久'
        lines = [heading,
                 self._group_label(member) + ' · ' + self._status_label(member) + ' · ' + term, '',
                 *quota_lines(member, public=public), '', *bandwidth_lines(member), '',
                 '🎬 <b>观看记录</b>']
        balance = None
        if self._points is not None:
            with contextlib.suppress(Exception):
                balance = int(self._points.balance(str(member['emby_user_id'])))
        summary = None
        if self._stats is not None and hasattr(self._stats, 'watch_summary'):
            with contextlib.suppress(Exception):
                summary = self._stats.watch_summary(str(member['emby_user_id']))
        for key, label, uncertain in (('seconds_24h', '近24小时', 'incomplete_24h'),
                                      ('seconds_30d', '近30天', 'incomplete_30d')):
            value = (summary or {}).get(key)
            text = duration(value)
            hint = '（部分记录）' if (summary or {}).get(uncertain) else ''
            lines.append(label + '：<b>' + text + '</b>' + hint)
        lines += ['', '💰 <b>积分：' + (str(balance) if balance is not None else '暂不可用') + '</b>']
        return '\n'.join(lines)

    def _brief_card(self, member: dict[str, Any]) -> str:
        return self._account_card(member, public=True)

    def _group_account_menu(self) -> list[list[dict[str, str]]]:
        return [[{'text': '🔄 刷新我的账号', 'callback_data': 'me'},
                 {'text': '关闭卡片', 'callback_data': 'panel_close'}],
                *self._private_button('account', '完整账号 · 私聊')]

    def _usage_brief(self, member: dict[str, Any]) -> str:
        return '\n'.join([*quota_lines(member), '', *bandwidth_lines(member)])

    async def _expire_own_card(self, chat_id: Any, message_id: int,
                               delay: float = GROUP_BRIEF_TTL) -> None:
        await asyncio.sleep(delay)
        await self._call("deleteMessage", {
            "chat_id": chat_id, "message_id": message_id})

    def _schedule_brief_cleanup(self, chat_id: Any) -> None:
        mid = self._panel.get(self._pkey(chat_id))
        if not mid:
            return
        task = asyncio.create_task(self._expire_own_card(chat_id, mid))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    @staticmethod
    def _rank_hours(hours: int) -> int:
        return 168 if int(hours) >= 48 else 24

    def _watch_rankings_text(self, hours: int = 24) -> str:
        hours = self._rank_hours(hours)
        window = "今日" if hours <= 24 else "本周"
        lines = [f"🏆 <b>{window}观影时长</b>\n"]
        rows: list[dict[str, Any]] = []
        if self._stats is not None:
            with contextlib.suppress(Exception):
                rows = self._stats.top_watchers(hours=hours, limit=10)
        if not rows:
            lines.append("暂时还没有排行数据。")
            return "\n".join(lines)
        for i, row in enumerate(rows, 1):
            lines.append(
                f"{i}. {watch_rank_mention(row)} · {duration(row.get('seconds', int((row.get('hours') or 0)*3600)))}"
                + ('（已确认，部分跨界历史无法拆分）' if row.get('incomplete') else ''))
        return "\n".join(lines)

    def _heat_rankings_text(self, days: int = 1) -> str:
        days = 7 if int(days) >= 3 else 1
        window = "今日" if days <= 1 else "本周"
        lines = [f"🎞 <b>{window}热度排行</b>\n"]
        movies: list[dict[str, Any]] = []
        shows: list[dict[str, Any]] = []
        if self._stats is not None:
            with contextlib.suppress(Exception):
                movies, shows = self._stats.top_titles_split(days=days, limit=10)
        if not movies and not shows:
            lines.append("暂时还没有排行数据。")
            return "\n".join(lines)
        if movies:
            lines.append("<b>▎电影</b>")
            for i, row in enumerate(movies, 1):
                lines.append(
                    f"{i}. {escape(str(row.get('title') or '—'))} · {int(row.get('plays') or 0)} 次 · "
                    f"{duration(int((row.get('hours') or 0) * 3600))}")
            lines.append("")
        if shows:
            lines.append("<b>▎电视剧</b>")
            for i, row in enumerate(shows, 1):
                lines.append(
                    f"{i}. {escape(str(row.get('title') or '—'))} · {int(row.get('plays') or 0)} 次 · "
                    f"{duration(int((row.get('hours') or 0) * 3600))}")
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def _watch_rankings_keyboard(self, hours: int, *, heat: bool = False
                                  ) -> list[list[dict[str, str]]]:
        hours = self._rank_hours(hours)
        day = "● 今日观影" if hours <= 24 and not heat else "今日观影"
        week = "● 本周观影" if hours > 24 and not heat else "本周观影"
        heat_day = "● 今日热度" if heat and hours <= 24 else "今日热度"
        heat_week = "● 本周热度" if heat and hours > 24 else "本周热度"
        rows = [
            [{"text": day, "callback_data": "rank:24"},
             {"text": week, "callback_data": "rank:168"}],
            [{"text": heat_day, "callback_data": "heat:1"},
             {"text": heat_week, "callback_data": "heat:7"}],
        ]
        if not _GROUP.get():
            rows.append([{"text": "💰 积分榜", "callback_data": "points_rank"}])
            rows += BACK_HOME
        return rows

    @staticmethod
    def _private_chat(message: dict[str, Any]) -> bool:
        kind = str((message.get("chat") or {}).get("type") or "private")
        return kind == "private"

    async def _handle_command(self, chat_id: Any, tg_user_id: str,
                              tg_username: str, text: str,
                              display_name: str = "") -> bool | None:
        # A command starts a NEW operation. Text inputs/buttons within that
        # operation still edit its panel. Never delete before a new menu exists.
        self._check_bot_identity()
        key = self._pkey(chat_id)
        old = self._saved_menu(chat_id)
        if text.split('@', 1)[0] == '/cancel' and self._admin_panel(chat_id):
            await self._return_admin_target(chat_id, int(self._panel[key]), '\n已取消，未执行本次修改。')
            return
        previous = self._admin_panel(chat_id)
        if previous:
            # A new command cancels this draft/confirmation, not its target.
            # The old card can still be reopened, but cannot replay a grant.
            previous['pending'] = None
        old_panel = self._panel.pop(key, None)
        self._pending.pop(key, None)
        for claim_key, claim in list(self._gift_claims.items()):
            if claim_key[0] == str(chat_id) and claim[1] == tg_user_id:
                self._gift_claims.pop(claim_key, None)
        try:
            return await self._dispatch_command(chat_id, tg_user_id, tg_username,
                                                text, display_name)
        finally:
            new = self._menu_panel.get(key)
            if old and new and new != old and not self._admin_panel(chat_id, old):
                await self._retire_menu(chat_id, old)
            elif key not in self._panel and old_panel:
                self._panel[key] = old_panel

    async def _dispatch_command(self, chat_id: Any, tg_user_id: str,
                                tg_username: str, text: str,
                                display_name: str = "") -> bool | None:
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
        if command == "myinfo":
            command = "me"
        if command == "rmemby":
            command = "rm"

        member = self._member_for_chat(tg_user_id)
        in_group = _GROUP.get()
        # /start is how anybody opens the bot -- it is the very first message
        # every ordinary member ever sends. Routing it through the admin gate
        # answered that message with "no permission", and admins fared no
        # better: with no _cmd_start they were told the command was unknown.
        # A payload on /start is an invite or a card; /start itself cancels
        # whatever conversation was in flight.
        if command == "start":
            payload = args[0] if args else ""
            if not in_group and payload == 'account':
                await self._show(chat_id, self._account_card(member) if member else self._guest_home(display),
                                 self.info_menu() if member else self.guest_menu())
                return
            if not in_group and (payload == 'manage' or payload.startswith(('manage_', 'person_'))):
                if not self.is_admin(member):
                    await self._show(chat_id, '⛔ 仅管理员可以打开目标管理。', BACK_HOME)
                    return
                actor = self._admin_actor(member, tg_username)
                if payload.startswith('manage_'):
                    target = self._members.get(payload[7:])
                    if target:
                        await self._admin_show_user(chat_id, target, actor)
                    else:
                        await self._show(chat_id, '目标账号不存在，请重新查找。', self.admin_menu())
                elif payload.startswith('person_'):
                    await self._admin_lookup(chat_id, payload[7:], actor)
                else:
                    await self._show(chat_id, '🛠 <b>用户管理</b>\n\n请先查找目标账号。', self.admin_menu())
                return
            if not in_group and payload.startswith("rebind_"):
                await self._open_rebind_handoff(chat_id, tg_user_id, payload[7:])
            elif not in_group and payload == "rebind":
                await self._start_rebind(chat_id, tg_user_id)
            else:
                await self._open_start(chat_id, tg_user_id, display, payload)
            return
        if command in PRIVATE_ONLY_COMMANDS and in_group:
            link = self._private_link()
            keyboard = [[{"text": "打开私聊", "url": link}]] if link else None
            await self._show(chat_id, "请私聊我完成这项操作。", keyboard)
            return
        if command == "claim":
            await self._show(chat_id, "认领功能已停用，现有绑定用户直接使用即可。", self.guest_menu())
            return
        if command == "rebind":
            await self._start_rebind(chat_id, tg_user_id)
            return
        if command == "cancel":
            await self._show(chat_id, "已取消，未执行任何变更。", BACK_HOME)
            return
        if command == "rank":
            hours = 24
            if args and args[0] in ("7", "168", "30", "720"):
                hours = 168
            await self._show(chat_id, self._watch_rankings_text(hours),
                             self._watch_rankings_keyboard(hours))
            return
        if command == "manage" and self.is_admin(member):
            if in_group:
                await self._show(chat_id, "请私聊打开管理菜单，或直接使用 /kk /renew 等命令。")
                return
            await self._show(chat_id, "🛠 <b>用户管理</b>\n\n请选择操作。", self.admin_menu())
            return
        if command == "help" and self.is_admin(member) and not in_group:
            await self._show(chat_id, ADMIN_HELP, self.admin_menu())
            return
        if command in ("help", "rules"):
            if in_group:
                body = RULES_TEXT if command == "rules" else self._group_help_text(member)
                await self._show(chat_id, body)
                return
            body = RULES_TEXT if command == "rules" else self._help_text(member)
            keyboard = (self._with_admin_row(self.member_menu(), member)
                        if member else self.guest_menu())
            await self._show(chat_id, body, keyboard)
            return
        if command == "usage":
            if member:
                await self._show(chat_id, self._usage_text(member), BACK_HOME if not in_group else None)
            elif not in_group:
                await self._show(chat_id, "当前 Telegram 没有绑定账号。", self.guest_menu())
            return
        if command == "me":
            if args:
                await self._show(chat_id, '用法：/me（不带参数）')
                return
            if not member:
                if in_group:
                    return
                await self._show(chat_id, "这个 Telegram 还没有账号。",
                                 self.guest_menu())
                return
            if in_group:
                shown = await self._show(chat_id, self._brief_card(member), self._group_account_menu())
                if shown:
                    self._schedule_brief_cleanup(chat_id)
                return shown
            return await self._show(chat_id, self._account_card(member), self.info_menu())
        if not self.is_admin(member):
            if in_group:
                return
            await self._show(
                chat_id,
                "请使用下方按钮，或发送 /start。",
                self._with_admin_row(self.member_menu(), member)
                if member else self.guest_menu())
            return

        if in_group and command not in GROUP_ADMIN_COMMANDS:
            await self._group_start(chat_id, 'manage')
            return
        actor = self._admin_actor(member, tg_username)
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            if in_group:
                return
            await self._show(chat_id, f"未知命令 /{escape(command)}，请使用下方菜单或 /help。",
                             self.admin_menu())
            return
        try:
            return await handler(chat_id, actor, args)
        except (ConfigError, ShopError, RequestError) as exc:
            await self.send(chat_id, f"❌ {_public_error(exc)}")
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
                      args: list[str]) -> bool | None:
        if len(args) > 1:
            await self._show(chat_id, '用法：/kk TelegramID或账号名，也可回复对方消息使用。')
            return
        if not args:
            if _GROUP.get():
                await self._show(chat_id, "请回复对方消息，或带上 Telegram 数字 ID。")
                return
            self._pending[self._pkey(chat_id)] = ("admin_find", time.time() + PENDING_TTL, {})
            await self._show(chat_id, "🔍 请指定用户：发送 Telegram 数字 ID、用户名，或转发对方消息。",
                             [[{"text": "◀ 返回管理", "callback_data": "admin"}]])
            return
        return await self._admin_lookup(chat_id, args[0], actor)

    async def _preview_group_change(self, chat_id: Any, target: dict, actor: str, group_id: str) -> None:
        from app.modules.member_ops import group_preview
        try:
            preview = group_preview(self._members, str(target['emby_user_id']), group_id)
        except (ValueError, KeyError, ConfigError) as exc:
            await self._show(chat_id, escape(str(exc)), self.admin_menu())
            return
        nonce = secrets.token_hex(5)
        group = self._groups.get(group_id)
        signature = hashlib.sha256(json.dumps(group, sort_keys=True).encode()).hexdigest()
        lines = [f"👥 <b>确认换组 · {escape(str(target.get('username') or '—'))}</b>\n",
                 f"目标组：{escape(str(group['name']))}",
                 f"原有效期：{_fmt_expiry(preview['current_expires_at_effective'])}",
                 f"保留方案：{_fmt_expiry(preview['policies']['keep']['expires_at'])}",
                 f"按组重算：{_fmt_expiry(preview['policies']['apply_group']['expires_at'])}",
                 *[escape(w) for w in preview['warnings']],
                 "历史用量、积分及其他个人权限保留。"]
        buttons = [{'text':'✅ 确认（保留期限/按组取消计时）','callback_data':f'admin_group_apply:keep:{nonce}'}]
        if group['billing_mode'] in ('time','both'):
            buttons.append({'text':'按新组重算期限','callback_data':f'admin_group_apply:apply_group:{nonce}'})
        await self._show(chat_id, '\n'.join(lines), [[b] for b in buttons]+[[{'text':'取消','callback_data':'admin_card'}]])
        self._hold_admin_user(chat_id, target, actor)
        self._pending[self._pkey(chat_id)][2]['group_confirm'] = {
            'group_id':group_id, 'nonce':nonce, 'signature':signature,
            'from_group':target.get('group_id'), 'from_expiry':target.get('expires_at_effective')}
        self._remember_admin_panel(chat_id, target, actor)

    async def _apply_group_change(self, chat_id: Any, message_id: int, target: dict, actor: str, data: str) -> None:
        waiting = self._pending.get(self._pkey(chat_id))
        if not waiting or waiting[0] != 'admin_user' or waiting[1] <= time.time():
            return
        extra = waiting[2]
        saved = extra.get('group_confirm') or {}
        parts = data.split(':')
        if len(parts)!=3 or parts[1] not in ('keep','apply_group') or parts[2]!=saved.get('nonce'):
            return
        if extra.get('message_id') is not None and extra['message_id']!=message_id:
            return
        group = self._groups.get(saved.get('group_id') or '') if self._groups else None
        signature = hashlib.sha256(json.dumps(group, sort_keys=True).encode()).hexdigest()
        if (not group or signature!=saved.get('signature') or target.get('group_id')!=saved.get('from_group')
                or target.get('expires_at_effective')!=saved.get('from_expiry')):
            extra.pop('group_confirm',None)
            await self._show(chat_id, '用户或用户组已变化，请重新预览。', self._user_admin_keyboard(str(target['emby_user_id'])))
            return
        extra.pop('group_confirm',None)
        updated = self._members.upsert(str(target['emby_user_id']), str(target['username']),
                                      {'group_id':group['id'],'expiry_policy':parts[1]},actor=actor)
        notice = await self._after_member_change(target)
        await self._admin_show_user(chat_id, updated, actor, notice)

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
        await self._preview_group_change(chat_id, target, actor, group_id)

    def _prouser_target(self, query: str) -> dict[str, Any] | None:
        """Same supported identifiers as /kk, but never pick an ambiguous grant."""
        value = query.strip().lstrip('@')
        if not value:
            return None
        if self._db is None:
            return self._find_target(query)
        if query.startswith('@'):
            rows = self._db.query('SELECT emby_user_id FROM members WHERE tg_username=? COLLATE NOCASE', (value,))
        else:
            numeric = str(int(value)) if value.isascii() and value.isdigit() and len(value) <= 19 else value
            rows = self._db.query(
                'SELECT emby_user_id FROM members WHERE username=? COLLATE NOCASE OR tg_username=? COLLATE NOCASE OR tg_user_id=?',
                (value, value, numeric))
        ids = {r['emby_user_id'] for r in rows}
        if len(ids) > 1:
            raise ConfigError('目标存在歧义，请使用能唯一定位的 Telegram ID 或 @用户名。')
        return self._members.get(ids.pop()) if ids else None

    async def _grant_whitelist(self, chat_id: Any, actor: str,
                                target: dict[str, Any]) -> None:
        reviewer = self._member_for_chat(_ACTOR.get() or str(chat_id))
        if not self.is_admin(reviewer):
            await self._show(chat_id, '⛔ 仅有效管理员可以授予白名单。')
            return
        uid = str(target.get('emby_user_id') or '')
        target = self._members.get(uid) if uid else None
        if not target or target.get('emby_missing_since'):
            await self._show(chat_id, '账号不存在或已标记缺失，未授予白名单；不会自动注册。')
            return
        if target.get('group_id') == WHITELIST_GROUP_ID:
            body = 'ℹ ' + member_mention(target) + ' 已在白名单，未重复授予。'
            updated = target
        else:
            try:
                if self._groups is None:
                    raise ConfigError('用户组服务不可用')
                self._groups.ensure_whitelist()
                updated = self._members.upsert(uid, str(target.get('username') or ''),
                                               {'group_id': WHITELIST_GROUP_ID}, actor=actor)
            except Exception as exc:  # noqa: BLE001 - local failure cannot be announced as a grant
                await self._show(chat_id, '❌ 白名单授予未确认：' + _public_error(exc) + '\n请查询账号状态后再试。')
                return
            self._members.audit(actor, 'telegram.prouser', uid,
                                'group=' + WHITELIST_GROUP_ID + '; status and roles preserved')
            notice = await self._after_member_change(target)
            if notice:
                body = '⚠ ' + member_mention(updated) + '：白名单已写入本地，远端未确认，请管理员重试同步。'
            else:
                body = (poetry_line() + '\n\n🎉 恭喜 ' + member_mention(updated)
                        + '\n获得 ' + member_mention(reviewer) + ' 签发的 <b>💠 白名单</b>！')
        self._hold_admin_user(chat_id, updated, actor)
        await self._show(chat_id, body)
        self._remember_admin_panel(chat_id, updated, actor)

    async def _cmd_prouser(self, chat_id: Any, actor: str,
                           args: list[str]) -> None:
        if not args:
            await self._show(chat_id, '请指定用户。用法：<code>/prouser TelegramID或账号名</code>，也可回复对方消息使用。\n仅授予现存账号白名单，不自动注册。')
            return
        try:
            target = self._prouser_target(' '.join(args))
        except ConfigError as exc:
            await self._show(chat_id, _public_error(exc))
            return
        if not target:
            await self._show(chat_id, '找不到该用户，未授予白名单；请核对标识或先注册。')
            return
        await self._grant_whitelist(chat_id, actor, target)

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
        notice = await self._after_member_change(target)
        await self.send(
            chat_id,
            f"✅ <b>{escape(str(target.get('username')))}</b> 已续期 {days} 天，"
            f"现在{_fmt_expiry(updated.get('expires_at_effective', updated.get('expires_at')))}。{notice}")

    async def _cmd_renewall(self, chat_id: Any, actor: str,
                            args: list[str]) -> None:
        if not args or not args[0].lstrip("-").isdigit():
            await self.send(chat_id, "用法：<code>/renewall 天数</code>")
            return
        days = int(args[0])
        user_ids = [str(m["emby_user_id"]) for m in self._members.list(limit=5000)]
        total = len(user_ids)
        # Everyone at once is not undoable, so it is confirmed before it runs.
        self._pending[self._pkey(chat_id)] = (
            "admin_confirm", time.time() + PENDING_TTL,
            {"action": "renewall", "days": days, "actor": actor, "user_ids": user_ids})
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
            f"账号：<b>{escape(str(preview['target'].get('username') or user_id))}</b>",
            f"注册渠道：{escape(str(preview['target'].get('register_via') or 'legacy'))}",
        ]
        if available:
            lines.append("\n连带邀请人是单独操作，不会随「只删本人」一起执行：")
            lines.extend(
                f"· {escape(str(row.get('username') or row.get('emby_user_id')))}"
                f"（{escape(str(row.get('reason') or ''))}）" for row in available)
        lines.append("\n同时会删除 Emby 账号，且<b>不可恢复</b>。")
        nonce = secrets.token_hex(6)
        self._pending[self._pkey(chat_id)] = (
            "admin_confirm", time.time() + PENDING_TTL,
            {"action": "rm", "user_id": user_id, "actor": actor, "nonce": nonce,
             "username": target.get("username") or user_id,
             "self_ids": [user_id],
             "cascade_ids": [user_id] + [str(r['emby_user_id']) for r in available]})
        buttons = [[{"text": "🗑 只删本人", "callback_data": f"rm_self:{nonce}"}]]
        if available:
            buttons.append([{"text": "⚠ 连带邀请人", "callback_data": f"rm_cascade:{nonce}"}])
        buttons.append([{"text": "✖ 取消", "callback_data": "admin_cancel"}])
        await self._show(chat_id, "\n".join(lines), buttons)
        self._pending[self._pkey(chat_id)][2]["message_id"] = self._panel.get(self._pkey(chat_id))
        self._remember_admin_panel(chat_id, target, actor)

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
            await self.send(chat_id, f"❌ {_public_error(exc)}")
            return
        self._members.audit(actor, "points.adjust", user_id, f"delta={delta}")
        await self.send(
            chat_id,
            f"✅ <b>{escape(str(target.get('username')))}</b> 积分 {delta:+d}，当前 {balance}。")

    async def _cmd_scoreall(self, chat_id: Any, actor: str,
                            args: list[str]) -> None:
        if not args or not args[0].lstrip("+-").isdigit():
            await self.send(chat_id, "用法：<code>/scoreall 数量</code>")
            return
        amount = int(args[0].lstrip("+"))
        user_ids = [str(m["emby_user_id"]) for m in self._members.list(limit=5000)]
        total = len(user_ids)
        self._pending[self._pkey(chat_id)] = (
            "admin_confirm", time.time() + PENDING_TTL,
            {"action": "scoreall", "amount": amount, "actor": actor, "user_ids": user_ids})
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
        notice = await self._after_member_change(target)
        await self.send(chat_id, f"🎁 已发放给 <b>{escape(str(target.get('username')))}</b>：{note}{notice}")

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
            f"✅ <b>{escape(str(target.get('username')))}</b> 邀请名额 {amount:+d}，"
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
                             self.admin_menu() if self._panel.get(self._pkey(chat_id)) else None)
            return
        stats = self._requests.stats()
        lines = [f"📋 <b>求片（{wanted}）</b>\n"]
        for row in rows:
            holder = row.get("claimed_by_name")
            lines.append(
                f"#{row['id']} {escape(str(row['display_title']))} · {row['status_label']}"
                + (f" · {escape(str(holder))}" if holder else "")
                + f"\n    求片人 {escape(str(row.get('username') or '-'))}")
        lines.append(
            f"\n待接单 {stats['open']} · 处理中 {stats['claimed']} · "
            f"本月 {stats['month_total']}")
        await self._show(chat_id, "\n".join(lines),
                         self.admin_menu() if self._panel.get(self._pkey(chat_id)) else None)

    # -- destructive confirmations -------------------------------------------

    async def _admin_confirm(self, chat_id: Any, message_id: int,
                             member: dict[str, Any]) -> None:
        """Run the action the operator just confirmed.

        Re-checks the role at execution time: the confirmation may have sat on
        screen while the person tapping it was demoted.
        """
        waiting = self._pending.pop(self._pkey(chat_id), None)
        if (not waiting or waiting[0] != "admin_confirm" or waiting[1] < time.time()
                or (waiting[2].get("message_id") is not None
                    and waiting[2]["message_id"] != message_id)):
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
            done, unconfirmed = 0, 0
            authority_notice = ''
            reviewer_tg = _ACTOR.get() or str(member.get('tg_user_id') or chat_id)
            for uid in extra.get("user_ids", []):
                reviewer = self._member_for_chat(reviewer_tg)
                if not self.is_admin(reviewer) or reviewer['emby_user_id'] != member['emby_user_id']:
                    authority_notice = '\n⚠ 管理员身份或权限已变化，剩余未执行。'
                    break
                row = self._members.get(uid)
                if not row:
                    continue
                with contextlib.suppress(Exception):
                    self._members.renew(str(row.get("emby_user_id")), days,
                                        actor=actor)
                    done += 1
                    unconfirmed += bool(await self._after_member_change(row))
            notice = f'\n⚠ 本地已更新，其中 {unconfirmed} 个账号远端未确认，请重试同步。' if unconfirmed else ''
            await self._edit(chat_id, message_id,
                             f"✅ 已为 {done} 个账号各续期 {days} 天。{notice}{authority_notice}")
            return

        if action == "scoreall":
            amount = int(extra.get("amount") or 0)
            done = 0
            for uid in extra.get("user_ids", []):
                row = self._members.get(uid)
                if not row:
                    continue
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
            confirm_ids = extra.get('cascade_ids' if cascade else 'self_ids')
            reviewer_tg = _ACTOR.get() or str(member.get('tg_user_id') or chat_id)
            def authorized() -> bool:
                current = self._member_for_chat(reviewer_tg)
                return self.is_admin(current) and current['emby_user_id'] == member['emby_user_id']
            try:
                result = await member_ops.execute_delete(
                    self._members, self._emby, user_id, actor=actor,
                    cascade=cascade, delete_emby=True, confirm_ids=confirm_ids,
                    authorize=authorized)
            except (KeyError, ConfigError, ConflictError) as exc:
                await self._edit(chat_id, message_id, '❌ 删除未执行：' + escape(str(exc)))
                return
            removed = result.get("removed") or []
            failed = result.get("emby_failed") or []
            note = ""
            if failed:
                await self._edit(chat_id, message_id,
                    f'❌ 删除未全部完成：已移除 {len(removed)} 个，保留 {len(result.get("retained") or [])} 个，请核查后重试。')
                return
            if cascade:
                extra_n = max(0, len(removed) - 1)
                await self._edit(
                    chat_id, message_id,
                    f"🗑 已删除 <b>{escape(str(extra.get('username')))}</b>"
                    f"（连带 {extra_n} 个账号）{note}。")
            else:
                await self._edit(
                    chat_id, message_id,
                    f"🗑 已删除 <b>{escape(str(extra.get('username')))}</b>"
                    f"（仅本人）{note}。")
            return

        await self._edit(chat_id, message_id, "未知操作。")

    # -- update handling ------------------------------------------------------

    def _reply_target_id(self, message: dict[str, Any]) -> str | None:
        reply = message.get("reply_to_message") or {}
        origin = reply.get("forward_origin") or {}
        sender = origin.get("sender_user") if origin.get("type") == "user" else reply.get("from")
        if sender and not sender.get("is_bot") and sender.get("id"):
            return str(sender["id"])
        return None

    @staticmethod
    def _is_amount_token(token: str) -> bool:
        raw = str(token or "").lstrip("+")
        return bool(raw.lstrip("-").isdigit() and len(raw.lstrip("-")) <= 5)

    def _with_reply_target(self, text: str, message: dict[str, Any]) -> str:
        if not text.startswith("/"):
            return text
        parts = text.split()
        command = parts[0].lower().lstrip("/").split("@", 1)[0]
        if command not in ADMIN_TARGET_COMMANDS:
            return text
        target = self._reply_target_id(message)
        if not target:
            return text
        args = parts[1:]
        if command in ("renew", "score"):
            if args and not self._is_amount_token(args[0]):
                return text
            return f"/{command} {target}" + ((" " + " ".join(args)) if args else "")
        if not args:
            return f"/{command} {target}"
        return text

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        from_user = message.get("from") or {}
        tg_user_id = str(from_user.get("id") or "")
        tg_username = str(from_user.get("username") or "")
        tg_name = from_user.get("first_name") or tg_username or "朋友"
        text = str(message.get("text") or "").strip()
        if not chat_id or not tg_user_id:
            return
        if self._anonymous_sender(message):
            if (text and text.split()[0].lower().split('@', 1)[0] == '/kk'
                    and not self._addressed_to_other_bot(text)
                    and self._group_chat_allowed(message.get('chat') or {})):
                await self.send(chat_id, '⛔ 无法核实匿名管理身份，请使用本人账号发送 /kk。')
            return
        if self._addressed_to_other_bot(text):
            return
        in_group = not self._private_chat(message)
        if in_group and not self._group_chat_allowed(message.get("chat") or {}):
            return

        with self._bind_session(chat_id, tg_user_id, group=in_group,
                                thread_id=self._thread_id(message)):
            await self._handle_bound_message(
                message, chat_id, tg_user_id, tg_username, tg_name, text, in_group)

    async def _handle_bound_message(self, message: dict[str, Any], chat_id: Any,
                                    tg_user_id: str, tg_username: str, tg_name: str,
                                    text: str, in_group: bool) -> None:
        command = text.split()[0].lower().split('@', 1)[0] if text else ''
        reply = (message.get('reply_to_message') or {}).get('message_id')
        waiting = self._pending.get(self._pkey(chat_id))
        business_input = not in_group or text.startswith('/') or bool(
            waiting and reply and waiting[2].get('message_id') == reply)
        claim = None
        if not in_group:
            parts = text.split()
            token = parts[1] if command == '/start' and len(parts) == 2 else text
            if looks_like_credential(token) and token.upper().startswith('GIFT'):
                # Validate recipient BEFORE the membership guide. A foreign
                # link must not fall back to the clicker's own authorisation.
                self._pending.pop(self._pkey(chat_id), None)
                for key, saved in list(self._gift_claims.items()):
                    if key[0] == str(chat_id) and saved[1] == tg_user_id:
                        self._gift_claims.pop(key, None)
                problem = self._gift_claim_problem(tg_user_id, token)
                if problem:
                    await self._show(chat_id, '❌ ' + escape(problem), BACK_HOME)
                    return
                claim = (time.time() + PENDING_TTL, tg_user_id, token)
            elif waiting and waiting[0] == 'username' and not text.startswith('/'):
                admission = waiting[2].get('admission')
                if str(getattr(admission, 'credential', '')).startswith('GIFT'):
                    claim = (waiting[1], tg_user_id, admission.credential)
        if (business_input and command not in ('/help', '/rules', '/cancel')
                and not await self.membership.gate(chat_id, tg_user_id)):
            mid = self._panel.get(self._pkey(chat_id))
            if claim and mid:
                self._gift_claims[(str(chat_id), int(mid))] = claim
            return
        mid = reply or self._panel.get(self._pkey(chat_id))
        panel = self._admin_panel(chat_id, mid)
        is_input = not text.startswith('/') or text.split('@', 1)[0] == '/cancel'
        if panel and is_input:
            if not self._admin_panel_authorized(panel, tg_user_id):
                return
            if panel['expires'] <= time.time():
                if not in_group:
                    await self._edit(chat_id, int(mid), '管理会话已过期，未执行修改；请重新 /kk 查询。')
                return
            pending = panel.get('pending')
            if in_group and reply != mid and text.split('@', 1)[0] != '/cancel':
                return  # group input must answer the card; /cancel may cancel the current one
            self._touch_panel(chat_id, mid)
            if pending and pending[1] > time.time():
                self._pending[self._pkey(chat_id)] = pending
            else:
                self._pending.pop(self._pkey(chat_id), None)
            if (not pending or pending[0] == 'admin_user') and not text.startswith('/'):
                if not in_group:
                    await self._return_admin_target(chat_id, int(mid), '\n请选择卡片上的操作。')
                return
        try:
            await self._dispatch_bound_message(message, chat_id, tg_user_id, tg_username,
                                                tg_name, text, in_group)
        finally:
            self._save_admin_pending(chat_id)

    async def _dispatch_bound_message(self, message: dict[str, Any], chat_id: Any,
                                    tg_user_id: str, tg_username: str, tg_name: str,
                                    text: str, in_group: bool) -> None:
        self._sweep_pending()
        waiting = self._pending.get(self._pkey(chat_id))
        if waiting and waiting[0] == "admin_find" and message.get("forward_origin"):
            origin = message["forward_origin"]
            sender = origin.get("sender_user") or {}
            if origin.get("type") == "user" and sender.get("id"):
                text = str(sender["id"])
            else:
                if in_group:
                    return
                await self._show(chat_id, "这条转发隐藏了发送者身份，请发送对方的 Telegram 数字 ID。",
                                 [[{"text": "◀ 返回管理", "callback_data": "admin"}]])
                return
        text = self._with_reply_target(text, message)
        if waiting and text and not text.startswith("/"):
            if in_group:
                extra = waiting[2] or {}
                reply_id = ((message.get("reply_to_message") or {}).get("message_id"))
                if extra.get("message_id") and reply_id != extra.get("message_id"):
                    return
                if waiting[0] not in ("admin_renew_days", "admin_score_delta", "admin_find"):
                    return
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
                    self._pending.pop(self._pkey(chat_id), None)
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
                    self._pending.pop(self._pkey(chat_id), None)
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
            if kind == "rebind_target":
                await self._pick_rebind_target(chat_id, tg_user_id, text)
                return
            if kind == "rebind_verify":
                await self._submit_rebind(message, chat_id, tg_user_id, tg_username, text)
                return
            if kind == "claim":
                self._pending.pop(self._pkey(chat_id), None)
                await self._show(chat_id, "认领已停用。已绑定用户直接使用；更换 TG 请申请换绑。", self.guest_menu())
                return

        # Commands are dispatched after the pending-conversation check above,
        # which deliberately ignores anything starting with '/': a member who
        # typed a command instead of the answer they were asked for meant the
        # command, not a title called "/help".
        if text.startswith("/"):
            shown = await self._handle_command(
                chat_id, tg_user_id, tg_username, text, display_name=tg_name)
            command = text.split()[0].lower().split('@', 1)[0]
            if shown and command in ('/kk', '/me'):
                await self._delete_trigger_command(chat_id, message.get('message_id'))
            return

        if in_group:
            return

        if looks_like_credential(text) and not self._member_for_chat(tg_user_id):
            await self._start_registration(chat_id, tg_user_id, credential=text)
            return

        await self._show_home(chat_id, tg_user_id, tg_name)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        from_user = callback.get("from") or {}
        tg_user_id = str(from_user.get("id") or "")
        tg_name = from_user.get("first_name") or "朋友"
        callback_id = str(callback.get("id") or "")
        await self._run_callback(data, chat_id, message_id, callback_id,
                                 tg_user_id, tg_name, message)

    async def _run_callback(self, data: str, chat_id: Any, message_id: Any,
                            callback_id: str, tg_user_id: str, tg_name: str,
                            message: dict[str, Any]) -> None:
        if not chat_id or not message_id:
            if callback_id:
                await self._answer_callback(callback_id)
            return
        in_group = not self._private_chat(message)
        if in_group:
            if self._anonymous_sender(message) or not tg_user_id:
                if callback_id:
                    await self._answer_callback(callback_id, '无法核实操作者身份，请使用本人账号。' if data.startswith('admin_gift') else '')
                return
            if not self._group_chat_allowed(message.get("chat") or {}):
                if callback_id:
                    await self._answer_callback(callback_id, '此群未获授权，不能赠送开号。' if data.startswith('admin_gift') else '')
                return
        with self._bind_session(chat_id, tg_user_id, group=in_group,
                                thread_id=self._thread_id(message)):
            await self._run_bound_callback(
                data, chat_id, message_id, callback_id, tg_user_id, tg_name,
                message, in_group)

    async def _run_bound_callback(self, data: str, chat_id: Any, message_id: Any,
                                   callback_id: str, tg_user_id: str, tg_name: str,
                                   message: dict[str, Any], in_group: bool) -> None:
        # Committed receipt retries are recipient-only transport work, never
        # a replay of registration or an administrative operation.
        if data == 'urank_close' or data.startswith('urank:'):
            if data == 'urank_close':
                await self._answer_callback(callback_id)
                await self._call("editMessageReplyMarkup", {
                    "chat_id": chat_id, "message_id": message_id,
                    "reply_markup": {"inline_keyboard": []}})
                return
            try:
                page_s, days_s = data.split(":", 1)[1].split("_", 1)
                page, days = int(page_s), int(days_s)
            except (ValueError, IndexError):
                await self._answer_callback(callback_id, "页码无效。")
                return
            pages = self._watch_rank_pages(days)
            if page < 1 or page > len(pages):
                await self._answer_callback(callback_id, "没有这一页。")
                return
            await self._answer_callback(callback_id, f"第 {page} 页")
            keyboard = self._watch_rank_keyboard(page, len(pages), days)
            if message.get("photo") or (str(chat_id), int(message_id)) in self._photo_panels:
                await self._call("editMessageCaption", {
                    "chat_id": chat_id, "message_id": message_id,
                    "caption": pages[page - 1], "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": keyboard}})
                return
            await self._edit(chat_id, message_id, pages[page - 1], keyboard)
            return
        if data.startswith('gift_receipt_retry:'):
            if in_group or not self._gift_receipts:
                await self._answer_callback(callback_id, '请在本人私聊查看注册群回执。')
                return
            try:
                rid = int(data.split(':', 1)[1])
                row = await self._gift_receipts.deliver(rid, retry_tg=tg_user_id)
                if row is None:
                    text = '这不是你的注册群回执。'
                elif row['status'] == 'sent':
                    text = '注册群回执已送达。'
                elif row['attempts'] >= 2:
                    text = '回执仍未送达，已保留失败记录，请联系管理员；账号注册不受影响。'
                else:
                    text = '原群或绑定状态暂不可用，请恢复后重试；账号注册不受影响。'
            except (ValueError, TypeError):
                text = '回执标识无效。'
            await self._answer_callback(callback_id, text)
            return
        # Review cards have independent persisted notice/authority checks.
        if data.startswith('tg_rebind_review:'):
            await self._dispatch_bound_callback(data, chat_id, message_id, callback_id,
                                                tg_user_id, tg_name, message, in_group)
            return
        self._sweep_pending()
        panel_key = (str(chat_id), int(message_id))
        if panel_key in self._retired_panels:
            await self._answer_callback(callback_id, '这张卡片已关闭或过期，请重新打开。')
            return
        owner = self._card_owner(chat_id, message_id)
        if in_group and owner != str(tg_user_id):
            await self._answer_callback(callback_id, '这不是你的操作卡片，请发送自己的命令。')
            return
        panel = self._admin_panel(chat_id, message_id)
        targeted = data.startswith(('admin_gift', 'admin_renew', 'admin_group_', 'rm_self:', 'rm_cascade:')) or data in (
            'admin_card', 'admin_groups', 'admin_score', 'admin_rm', 'admin_usage',
            'admin_binding', 'admin_pro', 'admin_rev', 'admin_prouser')
        if panel:
            if not self._admin_panel_authorized(panel, tg_user_id):
                await self._answer_callback(callback_id, '管理员身份或权限已变化，请重新查询。')
                return
            pending = panel.get('pending')
            if pending and pending[1] > time.time():
                self._pending[self._pkey(chat_id)] = pending
            else:
                self._pending.pop(self._pkey(chat_id), None)
        elif targeted:
            # Never borrow the selected user from another message in this chat.
            await self._answer_callback(callback_id, '目标管理卡已失效，请重新 /kk 查询。')
            return
        if data.startswith('admin_gift') and panel and panel.get('user_id'):
            await self._answer_callback(callback_id, '对方已有账号，不能重复赠送开号。')
            return
        if data.startswith('admin_renew:'):
            # Old amount-only buttons have no one-use confirmation identity.
            await self._answer_callback(callback_id, '旧版快捷续期已失效，请重新选择续期并输入天数。')
            return
        if in_group:
            public = data in ('membership_recheck', 'me', 'me_status', 'usage', 'home', 'help', 'rules', 'rank', 'top',
                              'panel_close', 'admin', 'admin_root', 'admin_find', 'admin_card',
                              'admin_groups', 'admin_renew', 'admin_score', 'admin_usage',
                              'admin_rm', 'admin_cancel', 'admin_pro', 'admin_rev', 'admin_prouser', 'admin_gift')
            public = public or data.startswith(('admin_gift_ok:', 'rank:', 'top:', 'heat:', 'admin_group_', 'rm_self:', 'rm_cascade:'))
            if not public:
                await self._answer_callback(callback_id, '请使用卡片上的私聊入口继续此操作。')
                return
        # Navigation ACKs remain prompt, but validation failures answer once
        # with their explanation instead of racing an earlier empty ACK.
        ack = None
        if not data.startswith(SELF_ANSWERING_CALLBACKS):
            ack = asyncio.create_task(self._answer_callback(callback_id))
            await asyncio.sleep(0)
        try:
            self._touch_panel(chat_id, message_id)
            if (data not in ('help', 'rules', 'panel_close', 'admin_cancel')
                    and not await self.membership.gate(chat_id, tg_user_id)):
                return
            if data == 'membership_recheck':
                current_mid = self._panel.get(self._pkey(chat_id), message_id)
                if not in_group and await self._resume_gift_claim(chat_id, current_mid, tg_user_id):
                    return
                await self._show_home(chat_id, tg_user_id, tg_name)
                return
            if data in ('home', 'panel_close', 'register', 'personal_home'):
                self._gift_claims.pop(panel_key, None)
            if data == 'panel_close':
                self._pending.pop(self._pkey(chat_id), None)
                self._admin_panels.pop(panel_key, None)
                self._retired_panels[panel_key] = time.time() + PENDING_TTL
                await self._retire_menu(chat_id, int(message_id))
                self._panel.pop(self._pkey(chat_id), None)
                return
            if panel and data in ('admin_card', 'admin', 'home', 'admin_cancel'):
                await self._return_admin_target(chat_id, int(message_id),
                    '\n已取消，未执行本次修改。' if data == 'admin_cancel' else '')
                return
            if data == 'admin_root':
                await self._admin_home(chat_id, int(message_id), self._member_for_chat(tg_user_id))
                return
            if data == 'personal_home' and not in_group:
                self._admin_panels.pop(panel_key, None)
                self._pending.pop(self._pkey(chat_id), None)
                data = 'home'
            if in_group and data in ('me', 'me_status', 'usage', 'home'):
                member = self._member_for_chat(tg_user_id)
                if member:
                    await self._edit(chat_id, int(message_id), self._brief_card(member), self._group_account_menu())
                return
            if in_group and data in ('help', 'rules'):
                body = RULES_TEXT if data == 'rules' else self._group_help_text(self._member_for_chat(tg_user_id))
                await self._edit(chat_id, int(message_id), body, self._group_account_menu())
                return
            await self._dispatch_bound_callback(data, chat_id, message_id, callback_id,
                                                tg_user_id, tg_name, message, in_group)
        finally:
            self._save_admin_pending(chat_id)
            if ack is not None:
                with contextlib.suppress(Exception):
                    await ack

    async def _dispatch_bound_callback(self, data: str, chat_id: Any, message_id: Any,
                                   callback_id: str, tg_user_id: str, tg_name: str,
                                   message: dict[str, Any], in_group: bool) -> None:
        if data.startswith("tg_rebind_review:"):
            if in_group:
                await self._review_rebind_callback(data, chat_id, message_id, tg_user_id, callback_id)
            else:
                await self._answer_callback(callback_id, "请在绑定群审核。")
            return  # independent review cards must never become personal menus
        owner = self._card_owner(chat_id, message_id)
        if in_group and owner and owner != str(tg_user_id):
            return
        if in_group and data in ("register", "claim", "rebind", "resetpw", "me_nodes",
                                 "req_new", "request_center", "watch_recent", "transfer", "shop", "invites"):
            return
        if in_group and data.startswith("resetpw"):
            return
        self._touch_panel(chat_id, message_id)
        if message.get('photo'):
            self._photo_panels.add((str(chat_id), int(message_id)))

        # Re-read binding state on every tap: the member could have been
        # unlinked from the panel while this keyboard sat on their screen.
        self._sweep_pending()
        member = self._member_for_chat(tg_user_id)
        waiting = self._pending.get(self._pkey(chat_id))
        # Leaving an input/confirmation screen also abandons its hidden draft.
        # Otherwise the next ordinary message can still transfer points or
        # submit a request after the member has returned to the parent menu.
        if data in ('home', 'me', 'bag', 'request_center', 'admin', 'help', 'rules',
                    'shop', 'invites', 'orders', 'my_requests', 'usage', 'watch_recent',
                    'me_status', 'me_points', 'me_nodes', 'devices', 'expiry'):
            self._pending.pop(self._pkey(chat_id), None)
            waiting = None
        if (waiting and ((waiting[0] == "password_reset" and not data.startswith("resetpw_ok:"))
                         or (waiting[0] == 'shop_confirm' and not data.startswith('buyok:')))):
            self._pending.pop(self._pkey(chat_id), None)

        if data == "register":
            await self._start_registration(chat_id, tg_user_id)
            return
        if data == "claim":
            self._pending.pop(self._pkey(chat_id), None)
            await self._edit(chat_id, message_id, "认领功能已停用。已绑定用户直接使用；更换 TG 请申请换绑。", self.guest_menu())
            return
        if data.startswith("rebind_retry:") and not in_group:
            await self._retry_rebind_notice(chat_id, tg_user_id, data.split(":", 1)[1])
            return
        if data == "rebind":
            await self._start_rebind(chat_id, tg_user_id)
            return
        if data == "help":
            await self._edit(
                chat_id, message_id, self._help_text(member),
                self._with_admin_row(self.member_menu(), member) if member else self.guest_menu())
            return
        if data == "rules":
            await self._edit(
                chat_id, message_id, RULES_TEXT,
                self._with_admin_row(self.member_menu(), member) if member else self.guest_menu())
            return
        if data == "home":
            self._pending.pop(self._pkey(chat_id), None)
            await self._show_home(chat_id, tg_user_id, tg_name)
            return
        if data == "admin_ok":
            waiting = self._pending.get(self._pkey(chat_id))
            if waiting and waiting[2].get("action") == "rm":
                return  # legacy generic confirmations cannot authorise deletion
            await self._admin_confirm(chat_id, message_id, member)
            return
        if data.startswith(("rm_self:", "rm_cascade:")):
            waiting = self._pending.get(self._pkey(chat_id))
            if (not waiting or waiting[0] != "admin_confirm" or waiting[1] < time.time()
                    or waiting[2].get("action") != "rm"
                    or data.split(":", 1)[1] != waiting[2].get("nonce")
                    or (waiting[2].get("message_id") is not None
                        and waiting[2]["message_id"] != message_id)):
                return
            extra = dict(waiting[2])
            extra["cascade"] = data.startswith("rm_cascade:")
            self._pending[self._pkey(chat_id)] = (waiting[0], waiting[1], extra)
            await self._admin_confirm(chat_id, message_id, member)
            return
        if data == "admin_cancel":
            self._pending.pop(self._pkey(chat_id), None)
            await self._edit(chat_id, message_id, "已取消，什么都没做。")
            return
        if data == "admin":
            await self._admin_home(chat_id, message_id, member)
            return
        if data == "admin_find":
            await self._admin_prompt_find(chat_id, message_id, member)
            return
        if data == "admin_gift" or data.startswith("admin_gift_ok:"):
            await self._admin_gift(chat_id, message_id, member, data)
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
            self._pending[self._pkey(chat_id)] = (
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
            self._pending[self._pkey(chat_id)] = (
                "admin_auth", time.time() + PENDING_TTL, {})
            await self._edit(
                chat_id, message_id,
                "✅ <b>预授权注册</b>\n\n请发送对方的 Telegram 数字 ID。",
                [[{"text": "◀ 返回管理", "callback_data": "admin"}]])
            return
        if data.startswith(("admin_renew", "admin_group_pick:", "admin_group_apply:")) or data in (
                "admin_pro", "admin_rev", "admin_prouser", "admin_score", "admin_rm",
                "admin_usage", "admin_binding", "admin_groups", "admin_card"):
            await self._admin_user_action(chat_id, message_id, member, data)
            return
        if data == "top" or data.startswith("top:"):
            days = 1
            if data.startswith("top:"):
                tail = data.split(":", 1)[1]
                if tail.isdigit():
                    days = 7 if int(tail) >= 3 else 1
            hours = 168 if days >= 3 else 24
            await self._edit(chat_id, message_id, self._watch_rankings_text(hours),
                             self._watch_rankings_keyboard(hours))
            return
        if data.startswith("heat:"):
            tail = data.split(":", 1)[1]
            days = 7 if tail.isdigit() and int(tail) >= 3 else 1
            hours = 168 if days >= 3 else 24
            await self._edit(chat_id, message_id, self._heat_rankings_text(days),
                             self._watch_rankings_keyboard(hours, heat=True))
            return
        if data in ('points_rank', 'titles_rank'):
            if in_group:
                return
            if data == 'titles_rank':
                await self._edit(chat_id, message_id, self._heat_rankings_text(7),
                                 self._watch_rankings_keyboard(168, heat=True))
                return
            lines = ['💰 <b>积分榜</b>\n']
            rows = self._points.top(limit=10) if self._points else []
            lines += [f"{i}. {escape(str(r.get('username') or '—'))} · {int(r.get('balance') or 0)} 分" for i, r in enumerate(rows, 1)]
            if not rows:
                lines += ['暂无排行数据。']
            await self._edit(chat_id, message_id, '\n'.join(lines), [[{'text':'◀ 返回观看榜','callback_data':'rank'}]])
            return
        if data == "rank" or data.startswith("rank:"):
            hours = 24
            if data.startswith("rank:"):
                tail = data.split(":", 1)[1]
                if tail.isdigit():
                    hours = 168 if int(tail) >= 48 else 24
            await self._edit(chat_id, message_id, self._watch_rankings_text(hours),
                             self._watch_rankings_keyboard(hours))
            return

        if not member:
            await self._edit(chat_id, message_id,
                             "这个 Telegram 还没有账号。", self.guest_menu())
            return

        if data == "me":
            await self._edit(
                chat_id, message_id,
                self._account_card(member),
                self.info_menu())
            return
        if data == "me_status":
            await self._edit(
                chat_id, message_id,
                self._whitelist_decoration(member)
                + f"📋 <b>{escape(str(member.get('username') or '-'))}</b>\n\n"
                f"状态：{self._status_label(member)}\n"
                f"用户组：{self._group_label(member)}\n"
                f"有效期：{_fmt_expiry(member.get('expires_at_effective', member.get('expires_at')))}\n"
                f"备注：{escape(str(member.get('note') or '—'))}",
                self.info_menu())
            return
        if data == "me_points":
            await self._edit(chat_id, message_id, self._points_text(member),
                             self.info_menu())
            return
        if data == "me_nodes":
            await self._edit(chat_id, message_id, await self._nodes_text(),
                             self.nodes_menu())
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
        if data == "request_center":
            await self._edit(chat_id, message_id, "🎬 <b>求片中心</b>\n\n提交想看的影片，或查看已有求片进度。", [
                [{"text": "➕ 发起求片", "callback_data": "req_new"},
                 {"text": "📋 我的求片", "callback_data": "my_requests"}], *BACK_HOME])
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
            expires = member.get("expires_at_effective", member.get("expires_at"))
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
                    rows.append(f"{flag}{escape(str(d.get('device_name') or d.get('device_id')))} · {when}")
                text = "📺 <b>我的设备</b>\n\n" + "\n".join(rows)
            await self._edit(chat_id, message_id, text, self.info_menu())
            return
        if data == "usage":
            await self._edit(chat_id, message_id, self._usage_text(member), [
                [{"text": "🎞 最近观看", "callback_data": "watch_recent"},
                 {"text": "🔄 更新数据", "callback_data": "usage"}], *BACK_HOME])
            return
        if data == "watch_recent":
            rows = self._stats.recent_watches(str(member['emby_user_id'])) if self._stats and hasattr(self._stats, 'recent_watches') else []
            lines = ["🎞 <b>最近观看</b>\n"]
            lines += [f"{escape(str(r.get('series_name') or r.get('item_name') or '未命名'))} · {duration(r['seconds'])}" for r in rows]
            await self._edit(chat_id, message_id, '\n'.join(lines) if rows else "暂无观看记录。", [[{"text": "◀ 返回用量与观看", "callback_data": "usage"}]])
            return
        if data in ("invites", "invite_new"):
            await self._invites_view(chat_id, message_id, member, mint=data == "invite_new")
            return
        if data == "resetpw" or data.startswith("resetpw_ok:"):
            await self._password_reset(chat_id, message_id, member, data)
            return

    async def _password_reset(self, chat_id: Any, message_id: int,
                              member: dict[str, Any], data: str) -> None:
        user_id = str(member.get("emby_user_id") or "")
        if self._emby is None:
            await self._edit(chat_id, message_id, "后台未连接 Emby，暂时无法重置。",
                             self.info_menu())
            return
        if data == "resetpw":
            nonce = secrets.token_hex(6)
            self._pending[self._pkey(chat_id)] = (
                "password_reset", time.time() + 120,
                {"user_id": user_id, "message_id": message_id, "nonce": nonce})
            await self._edit(
                chat_id, message_id,
                "🔐 <b>确认重置密码？</b>\n\n"
                "确认后会生成随机新密码，旧密码立即失效。\n"
                "你需要在客户端重新填写密码；账号权益不会改变。\n\n"
                "<i>新密码只在这里显示，请保存后再离开。</i>",
                [[{"text": "确认重置", "callback_data": f"resetpw_ok:{nonce}"},
                  {"text": "取消", "callback_data": "me"}]])
            return
        waiting = self._pending.get(self._pkey(chat_id))
        extra = waiting[2] if waiting else {}
        if (not waiting or waiting[0] != "password_reset" or waiting[1] < time.time()
                or extra.get("user_id") != user_id
                or extra.get("message_id") != message_id
                or data != f"resetpw_ok:{extra.get('nonce')}"):
            # Do not overwrite a successful password result on a double tap.
            return
        self._pending.pop(self._pkey(chat_id), None)  # single-use, consumed before I/O
        password = generate_password()
        ok = False
        with contextlib.suppress(Exception):
            ok = await self._emby.set_user_password(user_id, password)
        cache_notice = ''
        if ok and self._on_password_changed is not None:
            try:
                self._on_password_changed()  # no password or token crosses this callback
            except Exception:  # noqa: BLE001 - password changed; never pretend rollback
                cache_notice = '\n⚠ 密码已更改，面板会话缓存失效未确认，请联系管理员。'
        if hasattr(self._members, "audit"):
            self._members.audit(f"tg:{chat_id}", "member.password_reset", user_id,
                                "success" if ok else "failed", ok=bool(ok))
        current = self._member_for_chat(_ACTOR.get() or str(chat_id))
        if not current or str(current.get('emby_user_id')) != user_id:
            await self._edit(chat_id, message_id,
                '绑定已变化，未展示新密码；请当前绑定的 Telegram 重新发起重置。', BACK_HOME)
            return
        await self._edit(
            chat_id, message_id,
            (f"🔑 <b>新密码</b>\n\n<code>{password}</code>\n\n"
             "<i>请先保存。返回菜单后不会再次显示。</i>" + cache_notice) if ok
            else "❌ 重置失败，请稍后重试或联系管理员。",
            self.info_menu())

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
            # Membership events must invalidate earlier leaves immediately,
            # not wait behind a slow chat callback or a scan's account lock.
            if await self.membership.handle_update(update):
                return
            async with self._lock_for(self._chat_key(update)):
                if "message" in update:
                    await self._handle_message(update["message"])
                elif "callback_query" in update:
                    await self._handle_callback(update["callback_query"])
                message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
                sender = (update.get("callback_query") or message).get("from") or {}
                chat = message.get("chat") or {}
                if chat.get("id") and sender.get("id"):
                    group = not self._private_chat(message)
                    if group and not self._group_chat_allowed(chat):
                        return
                    await self._sync_chat_commands(
                        chat["id"], str(sender.get("id") or ""),
                        str(sender.get("language_code") or ""),
                        group=group)
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
             "allowed_updates": ["message", "callback_query", "chat_member", "my_chat_member"]},
            timeout=HTTP_TIMEOUT)
        self._last_poll_at = time.time()
        if not isinstance(result, list):
            raise RuntimeError("Telegram polling unavailable")  # noqa: TRY004 - failed transport, not a caller type error
        for update in result:
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
        if self._gift_receipts and self.enabled:
            receipt_task = asyncio.create_task(self._gift_receipts.run())
            self._in_flight.add(receipt_task)
            receipt_task.add_done_callback(self._in_flight.discard)

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
                f"账号 <b>{escape(str(member.get('username') or '-'))}</b> "
                f"{_fmt_expiry(member.get('expires_at_effective', member.get('expires_at')))}。\n"
                "需要续期请联系管理员。")
            sent += 1 if ok else 0
        return sent

    async def broadcast_rankings(self, chat_id: str, days: int = 1) -> bool:
        """Scheduled heat bulletin with poster when covers can be drawn."""
        if not chat_id or not self.enabled:
            return False
        caption = self._rankings_text(days)
        photo = await self._rankings_poster(days)
        parts = self._split_bulletin(caption)
        if photo:
            result = await self._call_multipart(
                "sendPhoto",
                {"chat_id": str(chat_id), "caption": parts[0], "parse_mode": "HTML"},
                {"photo": ("ranks.jpg", photo, "image/jpeg")})
            if result is not None:
                rest_ok = True
                for extra in parts[1:]:
                    rest_ok = await self.send(chat_id, extra) and rest_ok
                return rest_ok
        sent = True
        for part in parts:
            sent = await self.send(chat_id, part) and sent
        return sent

    async def broadcast_watch_rank(self, chat_id: str, days: int = 1) -> bool:
        """Scheduled watch-time board: podium photo plus a numbered pager."""
        if not chat_id or not self.enabled:
            return False
        pages = self._watch_rank_pages(days)
        keyboard = self._watch_rank_keyboard(1, len(pages), days)
        photo = await self._watch_rank_poster(days)
        if photo:
            result = await self._call_multipart(
                "sendPhoto",
                {"chat_id": str(chat_id), "caption": pages[0][:1024],
                 "parse_mode": "HTML",
                 "reply_markup": {"inline_keyboard": keyboard}},
                {"photo": ("watch-rank.jpg", photo, "image/jpeg")})
            if isinstance(result, dict) and result.get("message_id"):
                self._photo_panels.add((str(chat_id), int(result["message_id"])))
                return True
            if result is not None:
                return True
        return await self.send(chat_id, pages[0], keyboard)

    @staticmethod
    def _image_bytes(content: bytes, content_type: str = "") -> bytes | None:
        if not content:
            return None
        if content[:2] == b"\xff\xd8" or content[:8] == b"\x89PNG\r\n\x1a\n" or content[:6] in (b"GIF87a", b"GIF89a"):
            return content
        if str(content_type or "").lower().startswith("image/"):
            return content
        return None

    async def _tg_avatar_bytes(self, tg_user_id: str) -> bytes | None:
        if not str(tg_user_id).isdigit():
            return None
        photos = await self._call(
            "getUserProfilePhotos",
            {"user_id": int(tg_user_id), "limit": 1}, timeout=10)
        sizes = ((photos or {}).get("photos") or [None])[0] if isinstance(photos, dict) else None
        file_id = str((sizes[-1] or {}).get("file_id") or "") if sizes else ""
        if not file_id:
            chat = await self._call("getChat", {"chat_id": int(tg_user_id)}, timeout=10)
            file_id = str(((chat or {}).get("photo") or {}).get("big_file_id") or "") if isinstance(chat, dict) else ""
        if not file_id:
            return None
        info = await self._call("getFile", {"file_id": file_id}, timeout=10)
        path = str((info or {}).get("file_path") or "") if isinstance(info, dict) else ""
        if not path:
            return None
        token = self._token()
        client = await self._client()
        if not token or client is None:
            return None
        try:
            r = await client.get(f"{API_ROOT}/file/bot{token}/{path}", timeout=15)
        except Exception:  # noqa: BLE001 - avatar is optional
            return None
        if r.status_code != 200:
            return None
        return self._image_bytes(r.content or b"", str(r.headers.get("content-type") or ""))

    async def _watch_rank_poster(self, days: int) -> bytes | None:
        days = max(1, int(days or 1))
        rows: list[dict[str, Any]] = []
        if self._stats is not None:
            with contextlib.suppress(Exception):
                rows = self._stats.top_users(days=days, limit=3, calendar=True)
        if not rows:
            return None
        avatars: dict[str, bytes] = {}
        for row in rows:
            tg_id = str(row.get("tg_user_id") or "")
            if not tg_id or tg_id in avatars:
                continue
            with contextlib.suppress(Exception):
                blob = await self._tg_avatar_bytes(tg_id)
                if blob:
                    avatars[tg_id] = blob
        covers = await self._ranking_covers(days)
        from app.modules.rank_poster import render_watch_poster
        try:
            return render_watch_poster(
                rows, weekly=days >= 3, avatars=avatars, covers=covers,
                when=ranking_stamp(days))
        except Exception:  # noqa: BLE001 - fall back to text
            return None

    async def _ranking_covers(self, days: int) -> dict[str, bytes]:
        covers: dict[str, bytes] = {}
        if self._stats is None:
            return covers
        fetch = getattr(self._emby, "item_primary_image", None)
        if not callable(fetch):
            return covers
        try:
            movies, shows = self._stats.top_titles_split(
                days=days, limit=5, calendar=True)
        except Exception:  # noqa: BLE001 - covers are optional
            return covers
        for row in [*movies, *shows]:
            item_id = str(row.get("item_id") or "")
            if not item_id or item_id in covers:
                continue
            with contextlib.suppress(Exception):
                blob = await fetch(item_id)
                if blob:
                    covers[item_id] = blob
        return covers

    async def _rankings_poster(self, days: int) -> bytes | None:
        if self._stats is None:
            return None
        days = max(1, int(days or 1))
        try:
            movies, shows = self._stats.top_titles_split(
                days=days, limit=5, calendar=True)
        except Exception:  # noqa: BLE001 - poster is optional
            return None
        if not movies and not shows:
            return None
        covers = await self._ranking_covers(days)
        from app.modules.rank_poster import render_rank_poster
        try:
            return render_rank_poster(
                movies, shows, weekly=days >= 3,
                covers=covers, when=ranking_stamp(days))
        except Exception:  # noqa: BLE001 - fall back to text bulletin
            return None

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
        unknown = 0
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
            if status == "group-check-unavailable":
                unknown += 1
            if not allowed:
                left.append({
                    "emby_user_id": member.get("emby_user_id"),
                    "username": member.get("username"),
                    "tg_user_id": tg_id,
                    "status": status,
                })
        return {"checked": checked, "left": left, "unavailable": bool(unknown), "unknown": unknown}
