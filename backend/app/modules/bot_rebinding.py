"""Telegram transport for verified reassignment; review cards are not menus."""

from __future__ import annotations

import contextlib
import time
from html import escape
from typing import Any


class RebindBotMixin:
    async def _start_rebind(self, chat_id: Any, tg_id: str) -> None:
        back = [[{"text": "取消并返回", "callback_data": "home"}]]
        if not self._rebinding or self._emby is None:
            await self._show(chat_id, "换绑服务暂不可用，请联系管理员。", back)
            return
        if self._member_for_chat(tg_id):
            await self._show(
                chat_id,
                "这个 Telegram 已绑定账号，无需认领或换绑。请用新的 Telegram 发起申请。",
                back,
            )
            return
        if not self._cfg().get("group_interaction_chats"):
            await self._show(chat_id, "尚未配置审核群，请联系管理员。", back)
            return
        self._pending[self._pkey(chat_id)] = ("rebind_verify", time.time() + 300, {})
        await self._show(
            chat_id,
            "🔗 <b>申请 TG 换绑</b>\n\n仅用于将现存账号从原 Telegram 换到当前 Telegram。\n\n请在私聊发送：<code>Emby用户名 密码</code>\n验证后提交绑定群管理员审核，24 小时有效。\n\n<i>密码不会回显或保存，输入消息会及时删除；请勿在群里发送。发送 /cancel 取消。</i>",
            back,
        )

    async def _submit_rebind(
        self, message: dict, chat_id: Any, tg_id: str, tg_name: str, text: str
    ) -> None:
        # Best effort deletion is not a promise to erase Telegram's cloud copy.
        if message.get("message_id"):
            with contextlib.suppress(Exception):
                await self._call(
                    "deleteMessage", {"chat_id": chat_id, "message_id": message["message_id"]}
                )
        back = [[{"text": "取消并返回", "callback_data": "home"}]]
        if self._member_for_chat(tg_id) or not self._rebinding or self._emby is None:
            self._pending.pop(self._pkey(chat_id), None)
            await self._show(chat_id, "当前绑定状态已变化，无法申请换绑。", back)
            return
        if not self._rebinding.allow_attempt(tg_id):
            self._pending.pop(self._pkey(chat_id), None)
            await self._show(chat_id, "尝试过于频繁，请 15 分钟后再试。", back)
            return
        fields = text.split(None, 1)
        if len(fields) != 2:
            await self._show(chat_id, "格式应为：用户名 空格 密码。请重新输入，或取消。", back)
            return
        # Neither pending state, logs, requests nor audit retain the password.
        verified = None
        try:
            verified = await self._emby.authenticate_user(fields[0], fields[1])
        except Exception:  # noqa: BLE001 - never expose credential-bearing adapter errors
            verified = None
        finally:
            fields.clear()
        if not verified or not verified.get("Id"):
            await self._show(chat_id, "账号验证未通过或 Emby 暂不可用，请检查后重试。", back)
            return
        try:
            row = self._rebinding.create(str(verified["Id"]), tg_id, tg_name)
        except ValueError as exc:
            await self._show(chat_id, escape(str(exc)), back)
            return
        self._pending.pop(self._pkey(chat_id), None)
        delivered = await self._publish_rebind(row)
        await self._show(
            chat_id,
            f"📨 <b>换绑申请 #{row['id']}</b>\n\n账号验证通过。"
            + (
                "已发送到绑定群，请等待管理员审核。"
                if delivered
                else "申请已保存，群通知暂未送达；管理员仍可在面板审核。"
            )
            + "\n审核前原绑定保持不变；24 小时内有效。",
            self.guest_menu(),
        )

    @staticmethod
    def _rebind_card(row: dict) -> str:
        labels = {
            "pending": "等待管理员审核",
            "approved": "已通过换绑",
            "rejected": "已拒绝",
            "expired": "已过期",
            "conflict": "绑定已变化，未执行",
        }
        status = labels.get(row["status"], row["status"])
        return (
            f"🔗 <b>TG 换绑申请 · #{int(row['id'])}</b>\n\n"
            f"Emby账号：{escape(str(row['wanted_username']))}\n"
            f"原绑定：<code>{escape(str(row['old_tg_user_id']))}</code>\n"
            f'申请人：<a href="tg://user?id={int(row["tg_user_id"])}">新 Telegram</a>\n'
            f"验证：已通过 Emby 账号验证\n状态：{escape(status)}"
        )

    async def _publish_rebind(self, row: dict) -> int:
        count = 0
        for chat in self._cfg().get("group_interaction_chats") or []:
            payload = {
                "chat_id": chat,
                "text": self._rebind_card(row),
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "✅ 通过换绑",
                                "callback_data": f"tg_rebind_review:{row['id']}:yes",
                            },
                            {
                                "text": "❌ 拒绝申请",
                                "callback_data": f"tg_rebind_review:{row['id']}:no",
                            },
                        ]
                    ]
                },
            }
            with contextlib.suppress(Exception):
                result = await self._call("sendMessage", payload)
                if isinstance(result, dict) and result.get("message_id"):
                    actual = str((result.get("chat") or {}).get("id") or chat)
                    self._db.execute(
                        "INSERT OR REPLACE INTO tg_rebind_notices VALUES(?,?,?)",
                        (row["id"], actual, int(result["message_id"])),
                    )
                    count += 1
        return count

    async def review_rebind(
        self, request_id: int, approve: bool, reviewer: str, reviewer_tg: str | None = None
    ) -> dict:
        if not self._rebinding:
            raise ValueError("换绑服务未配置")
        row = self._rebinding.get(request_id)
        if row["status"] == "pending" and approve and int(row.get("expires_at") or 0) > time.time():
            if self._emby is None:
                raise ValueError("无法核验 Emby，请稍后重试")
            try:
                users = await self._emby.list_users()
            except Exception:  # noqa: BLE001 - public error must not contain adapter secrets
                raise ValueError("无法核验 Emby，请稍后重试") from None
            if not users or not any(str(u.get("Id")) == row["emby_user_id"] for u in users):
                raise ValueError("账号已不存在或 Emby 不可用，未更改绑定")
        if reviewer_tg is not None and not self.is_admin(self._member_for_chat(reviewer_tg)):
            raise ValueError("管理员权限已变化，未执行换绑")
        result = self._rebinding.review(request_id, approve, reviewer)
        notices = self._db.query(
            "SELECT chat_id,message_id FROM tg_rebind_notices WHERE request_id=?", (request_id,)
        )
        for notice in notices:
            with contextlib.suppress(Exception):
                await self._call(
                    "editMessageText",
                    {
                        "chat_id": notice["chat_id"],
                        "message_id": notice["message_id"],
                        "text": self._rebind_card(result),
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
        if result["changed"]:
            texts = [
                (
                    result["tg_user_id"],
                    "✅ 换绑已通过，可发送 /start 使用原账号。"
                    if result["approved"]
                    else "换绑申请未生效：" + str(result.get("note") or result["status"]),
                )
            ]
            if result["approved"]:
                texts.append(
                    (
                        result["old_tg_user_id"],
                        "🔗 你的 Emby 账号已由管理员审核换绑到新的 Telegram；如非本人操作，请立即联系管理员。",
                    )
                )
            for chat, text in texts:
                with contextlib.suppress(Exception):
                    await self._call("sendMessage", {"chat_id": chat, "text": text})
        return result

    async def _review_rebind_callback(
        self, data: str, chat_id: Any, message_id: Any, tg_id: str, callback_id: str
    ) -> None:
        member = self._member_for_chat(tg_id)
        if not self.is_admin(member):
            await self._answer_callback(callback_id, "仅管理员可以审核换绑。")
            return
        try:
            _, raw, decision = data.split(":")
            rid = int(raw)
            if decision not in ("yes", "no"):
                raise ValueError("无效审核动作")
            known = self._db.one(
                "SELECT 1 AS n FROM tg_rebind_notices WHERE request_id=? AND chat_id=? AND message_id=?",
                (rid, str(chat_id), int(message_id)),
            )
            if not known:
                raise ValueError("这不是有效的审核卡")
            result = await self.review_rebind(
                rid, decision == "yes", self._admin_actor(member, tg_id), reviewer_tg=tg_id
            )
            await self._answer_callback(
                callback_id, "已处理" if result["changed"] else "申请已经处理过了"
            )
        except (ValueError, KeyError) as exc:
            await self._answer_callback(callback_id, str(exc)[:150])
