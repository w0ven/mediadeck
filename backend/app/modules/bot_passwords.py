"""Private, short-lived password choices using the existing account operations.

Secrets live only in the existing in-memory conversation until confirm/cancel/TTL.
They never enter callback data, request cards, audit details or persistent settings.
"""

from __future__ import annotations

import contextlib
import secrets
import time
from html import escape

from app.core.errors import ConfigError


def validate_password(password: str) -> None:
    # Same minimum as the existing member password API; do not strip or normalise.
    if len(password) < 6:
        raise ConfigError("密码至少 6 位；不会自动删除空格或修改你输入的内容。")


class PasswordBotMixin:
    @staticmethod
    def _password_navigation(raw: str) -> bool:
        if raw != raw.strip() or not raw:
            return False
        return raw.split()[0].split("@", 1)[0].lower() in (
            "/cancel",
            "/start",
            "/me",
            "/myinfo",
            "/help",
            "/register",
            "/requests",
            "/uploader",
        )

    def _password_custom_waiting(self, chat) -> bool:
        waiting = self._pending.get(str(chat))
        return bool(waiting and waiting[0] == "password_custom" and waiting[1] > time.time())

    async def _pw_render(self, chat, mid, p, stage, body, actions):
        nonce = secrets.token_hex(6)
        extra = dict(p, nonce=nonce, message_id=mid)
        keyboard = [
            [{"text": label, "callback_data": f"pw:{nonce}:{action}"} for label, action in row]
            for row in actions
        ]
        ttl = 120 if p["purpose"] == "reset" else 600
        entry = ("password_" + stage, time.time() + ttl, extra)
        key = self._pkey(chat)
        self._pending[key] = entry
        shown = (
            await self._edit(chat, mid, body, keyboard)
            if mid
            else await self._show(chat, body, keyboard)
        )
        if self._pending.get(key) is not entry:
            return
        if not shown:
            self._pending.pop(key, None)
            return
        extra["message_id"] = self._panel.get(key, mid)

    async def _pw_choose(self, chat, mid, p):
        p = dict(p)
        p.pop("password", None)
        p.pop("password_mode", None)
        title = "注册账号" if p["purpose"] == "register" else "重置本人密码"
        actions = [[("🎲 随机生成", "random"), ("✏️ 自定义密码", "custom")]]
        if p["purpose"] == "register":
            actions.append([("返回修改用户名", "username"), ("取消注册", "cancel")])
        else:
            actions.append([("取消 / 返回", "cancel")])
        await self._pw_render(
            chat,
            mid,
            p,
            "choice",
            f"🔐 <b>{title}</b>\n用户名：<code>{escape(p['username'])}</code>\n\n"
            "请选择密码方式。选择前不会生成密码，也不会创建账号或修改旧密码。",
            actions,
        )

    async def _registration_password_start(
        self, chat, tg_id, tg_username, username, admission=None
    ):
        from app.modules.telegram import USERNAME_RE

        if str(chat) != str(tg_id) or str(chat).startswith("-"):
            return
        username = username.strip()
        if not USERNAME_RE.fullmatch(username):
            await self._show(
                chat,
                "❌ 用户名不符合要求：3–20 字符、字母开头、只含字母数字下划线。\n请重新发送一个。",
                [[{"text": "取消", "callback_data": "home"}]],
            )
            return
        try:
            admission = self._fresh_admission(tg_id, admission)
        except ConfigError as exc:
            self._pending.pop(self._pkey(chat), None)
            await self._show(chat, escape(str(exc)), self.guest_menu())
            return
        mid = self._panel.get(self._pkey(chat))
        self._admin_panels.pop((str(chat), int(mid or 0)), None)
        await self._pw_choose(
            chat,
            mid,
            {
                "purpose": "register",
                "tg_id": str(tg_id),
                "tg_username": tg_username,
                "username": username,
                "admission": admission,
            },
        )

    async def _password_reset(self, chat_id, message_id, member, data):
        if str(chat_id).startswith("-") or not member:
            return
        if data != "resetpw":
            return  # legacy one-step random buttons cannot bypass the new choice
        if self._emby is None:
            await self._edit(
                chat_id, message_id, "后台未连接 Emby，暂时无法重置。", self.info_menu()
            )
            return
        from app.modules.telegram import _ACTOR

        actor = _ACTOR.get() or str(chat_id)
        current = self._member_for_chat(actor)
        if (
            str(chat_id) != actor
            or not current
            or current["emby_user_id"] != member["emby_user_id"]
        ):
            return
        self._rq_abandon(chat_id)
        self._admin_panels.pop((str(chat_id), int(message_id)), None)
        await self._pw_choose(
            chat_id,
            message_id,
            {
                "purpose": "reset",
                "tg_id": actor,
                "user_id": str(member["emby_user_id"]),
                "username": str(member.get("username") or ""),
            },
        )

    async def _pw_confirm_card(self, chat, mid, p):
        title = "确认注册" if p["purpose"] == "register" else "确认重置本人密码"
        mode = "随机生成" if p["password_mode"] == "random" else "自定义"
        body = (
            f"🔐 <b>{title}</b>\n用户名：<code>{escape(p['username'])}</code>\n"
            f"密码方式：{mode}\n新密码（原样）：<pre>{escape(p['password'])}</pre>\n"
        )
        if p["purpose"] == "register":
            admission = p.get("admission")
            group_id = str(
                getattr(admission, "group_id", self._cfg().get("default_group_id") or "")
            )
            group = self._groups.get(group_id) if self._groups and group_id else None
            days = int(getattr(admission, "days", self._cfg().get("register_days") or 0))
            body += "用户组：" + escape(str((group or {}).get("name") or group_id or "默认"))
            body += f"\n有效期：{str(days) + ' 天' if days > 0 else '永久'}\n确认后才会创建账号、使用注册资格。"
        else:
            body += "确认后旧密码立即失效，需在客户端更新密码；账号权益不变。"
        body += "\n\n仅限本人私聊，请确认资料并妥善保存，不要转发。"
        await self._pw_render(
            chat,
            mid,
            p,
            "confirm",
            body,
            [
                [("确认注册" if p["purpose"] == "register" else "确认重置", "confirm")],
                [("返回选择密码", "choose"), ("取消", "cancel")],
            ],
        )

    async def _password_flow_callback(self, data, chat, mid, tg_id):
        if str(chat) != str(tg_id) or str(chat).startswith("-"):
            return
        waiting = self._pending.get(self._pkey(chat))
        if not waiting or waiting[1] <= time.time() or not waiting[0].startswith("password_"):
            return
        p = waiting[2]
        try:
            _, nonce, action = data.split(":", 2)
        except ValueError:
            return
        if nonce != p.get("nonce") or mid != p.get("message_id") or p.get("tg_id") != str(tg_id):
            return
        allowed = {
            "password_choice": {"random", "custom", "username", "cancel"},
            "password_custom": {"choose", "cancel"},
            "password_confirm": {"choose", "confirm", "cancel"},
        }
        if action not in allowed.get(waiting[0], set()):
            return
        if action == "cancel":
            self._pending.pop(self._pkey(chat), None)
            await self._edit(
                chat,
                mid,
                "已取消，未创建账号或修改密码。",
                self.guest_menu() if p["purpose"] == "register" else self.info_menu(),
            )
            return
        if action == "username" and p["purpose"] == "register":
            self._pending[self._pkey(chat)] = (
                "username",
                time.time() + 600,
                {"admission": p.get("admission")},
            )
            await self._edit(
                chat, mid, self._USERNAME_PROMPT, [[{"text": "取消", "callback_data": "home"}]]
            )
            return
        if action == "choose":
            return await self._pw_choose(chat, mid, p)
        if action == "custom":
            return await self._pw_render(
                chat,
                mid,
                p,
                "custom",
                "✏️ <b>自定义密码</b>\n\n请在此私聊发送新密码，至少 6 位。\n会原样使用，包括首尾空格；发送后还需确认，不会立即改密或创建账号。\n发送 /cancel 或 /start 可取消。",
                [[("返回密码方式", "choose"), ("取消", "cancel")]],
            )
        if action == "random":
            from app.modules.telegram import generate_password

            p = dict(p, password=generate_password(), password_mode="random")
            return await self._pw_confirm_card(chat, mid, p)
        if action == "confirm":
            # Consume before any await. A double tap cannot regenerate or reuse
            # registration admissions; the existing registration lock remains.
            self._pending.pop(self._pkey(chat), None)
            if p["purpose"] == "register":
                return await self._finish_registration(
                    chat,
                    tg_id,
                    p["tg_username"],
                    p["username"],
                    admission=p.get("admission"),
                    password=p["password"],
                )
            return await self._execute_password_reset(chat, mid, tg_id, p["user_id"], p["password"])

    async def _password_flow_text(self, chat, tg_id, raw, message):
        waiting = self._pending.get(self._pkey(chat))
        if not waiting or waiting[0] != "password_custom" or waiting[1] <= time.time():
            return
        p = waiting[2]
        if str(chat) != str(tg_id) or p.get("tg_id") != str(tg_id):
            return
        mid = p["message_id"]
        reply = (message.get("reply_to_message") or {}).get("message_id")
        if reply and reply != mid:
            await self._edit(
                chat,
                mid,
                "请回复当前自定义密码卡；本条未保存，也未修改密码。",
                [
                    [
                        {"text": "返回密码方式", "callback_data": f"pw:{p['nonce']}:choose"},
                        {"text": "取消", "callback_data": f"pw:{p['nonce']}:cancel"},
                    ]
                ],
            )
            return
        try:
            validate_password(raw)
        except ConfigError as exc:
            # Do not echo even an invalid input into a diagnostic or group.
            await self._pw_render(
                chat,
                mid,
                p,
                "custom",
                "❌ " + escape(str(exc)) + "\n请重新输入，或返回取消。",
                [[("返回密码方式", "choose"), ("取消", "cancel")]],
            )
            return
        await self._pw_confirm_card(chat, mid, dict(p, password=raw, password_mode="custom"))

    async def _execute_password_reset(self, chat, mid, tg_id, user_id, password):
        current = self._member_for_chat(tg_id)
        if not current or str(current["emby_user_id"]) != user_id:
            await self._edit(chat, mid, "绑定已变化，未执行密码重置。", self.guest_menu())
            return
        validate_password(password)
        ok = False
        with contextlib.suppress(Exception):
            ok = await self._emby.set_user_password(user_id, password)
        cache_notice = ""
        if ok and self._on_password_changed is not None:
            try:
                self._on_password_changed()
            except Exception:  # noqa: BLE001 - never pretend a remote password change rolled back
                cache_notice = "\n⚠ 密码已更改，面板会话缓存失效未确认，请联系管理员。"
        if hasattr(self._members, "audit"):
            self._members.audit(
                f"tg:{chat}",
                "member.password_reset",
                user_id,
                "success" if ok else "failed",
                ok=bool(ok),
            )
        current = self._member_for_chat(tg_id)
        if not current or str(current["emby_user_id"]) != user_id:
            await self._edit(
                chat,
                mid,
                "绑定已变化，未展示新密码；请当前绑定的 Telegram 重新发起重置。",
                self.guest_menu(),
            )
            return
        await self._edit(
            chat,
            mid,
            (
                f"🔑 <b>密码已重置</b>\n用户名：<code>{escape(str(current.get('username') or ''))}</code>\n"
                f"新密码：<pre>{escape(password)}</pre>\n<i>请先保存。返回菜单后不会再次显示。</i>"
                + cache_notice
            )
            if ok
            else "❌ 重置失败或远端未确认，请稍后重新选择重试；没有保存可查询的密码。",
            self.info_menu(),
        )
