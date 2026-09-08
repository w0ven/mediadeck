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
            self._pending[self._pkey(chat_id)] = ('rebind_target', time.time() + 300, {})
            await self._show(chat_id,
                '🔗 <b>更换 Telegram</b>\n\n请输入新 Telegram 的数字 ID，生成仅供该账号使用的确认链接。\n新 TG 打开链接并验证 Emby 密码后，交绑定群管理员审核；审核前不改变当前绑定。\n\n<i>原 TG 已失效或不能发言？直接用新 TG 打开机器人选择「TG 换绑」，无需旧号确认。</i>', back)
            return
        pending = self._db.one("SELECT * FROM tg_requests WHERE kind='rebind' AND status='pending' AND tg_user_id=? AND expires_at>?", (tg_id, int(time.time())))
        if pending:
            await self._show(chat_id, '📨 换绑申请已提交，请等待群管理员审核。', self._rebind_pending_menu(pending['id']))
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
        waiting = self._pending.get(self._pkey(chat_id))
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
        if (not waiting or waiting[0] != 'rebind_verify' or waiting[1] <= time.time()
                or self._pending.get(self._pkey(chat_id)) is not waiting):
            await self._show(chat_id, '验证会话已取消或超时，请重新发起。', back)
            return
        try:
            extra = waiting[2]
            row = self._rebinding.create(str(verified["Id"]), tg_id, tg_name, handoff=extra.get('handoff', ''))
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
                else "申请已保存，群通知暂未送达；请点击下方重试，仍失败时联系群管理员。"
            )
            + "\n审核前原绑定保持不变；24 小时内有效。",
            self._rebind_pending_menu(row['id']),
        )

    @staticmethod
    def _rebind_pending_menu(request_id: int) -> list:
        return [[{'text': '重试群通知', 'callback_data': f'rebind_retry:{request_id}'}],
                [{'text': '返回首页', 'callback_data': 'home'}]]

    async def _pick_rebind_target(self, chat_id: Any, tg_id: str, text: str) -> None:
        back = [[{'text': '取消并返回', 'callback_data': 'home'}]]
        try:
            if not self._private_link():
                raise ValueError('Bot 地址暂不可用，请稍后重试。')
            if not self._rebinding.allow_attempt(tg_id):
                raise ValueError('操作频繁，请 15 分钟后再试。')
            token = self._rebinding.handoff(tg_id, text.strip())
        except ValueError as exc:
            await self._show(chat_id, escape(str(exc)), back)
            return
        self._pending.pop(self._pkey(chat_id), None)
        link = self._private_link('rebind_' + token)
        await self._show(chat_id, '🔗 <b>新 Telegram 确认</b>\n\n请将下方链接交给目标新 TG，30 分钟内打开并验证 Emby 密码。之后在群中审核；此时原绑定尚未变化。\n\n' + escape(link),
                         [[{'text': '新 TG 打开确认', 'url': link}], *back])

    async def _open_rebind_handoff(self, chat_id: Any, tg_id: str, token: str) -> None:
        try:
            if not self._rebinding or self._member_for_chat(tg_id):
                raise ValueError('当前 Telegram 已绑定账号，或换绑服务不可用。')
            self._rebinding.open_handoff(token, tg_id)
            await self._start_rebind(chat_id, tg_id)
            key = self._pkey(chat_id)
            pending = self._pending.get(key)
            if pending and pending[0] == 'rebind_verify':
                self._pending[key] = (pending[0], pending[1], {'handoff': token})
        except ValueError as exc:
            await self._show(chat_id, escape(str(exc)), [[{'text': '返回', 'callback_data': 'home'}]])

    async def _retry_rebind_notice(self, chat_id: Any, tg_id: str, request_id: str) -> None:
        try:
            row = self._rebinding.get(int(request_id))
            if row['tg_user_id'] != tg_id or row['status'] != 'pending' or row['expires_at'] <= time.time():
                raise ValueError('该申请不能由当前账号重发，或已处理/过期。')
            cooldown = getattr(self, '_rebind_retry_at', {})
            if time.time() - cooldown.get(tg_id, 0) < 30:
                raise ValueError('请 30 秒后再试，避免重复发送。')
            cooldown[tg_id] = time.time()
            self._rebind_retry_at = cooldown
            delivered = await self._publish_rebind(row)
            await self._show(chat_id, '已送达审核群，请等待管理员处理。' if delivered else '暂未送达审核群，请稍后重试或联系管理员。', self._rebind_pending_menu(row['id']))
        except (ValueError, TypeError) as exc:
            await self._show(chat_id, escape(str(exc)), [[{'text': '返回', 'callback_data': 'home'}]])

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
        for configured_chat in self._group_allowlist():
            chat = configured_chat
            if chat.startswith('@'):
                # Telegram returns a numeric chat ID in the send result; use
                # that same identity for deduplication on subsequent retries.
                resolved = await self._call('getChat', {'chat_id': chat})
                if not isinstance(resolved, dict) or not resolved.get('id'):
                    continue
                chat = str(resolved['id'])
            if self._db.one('SELECT 1 FROM tg_rebind_notices WHERE request_id=? AND chat_id=?', (row['id'], str(chat))):
                count += 1
                continue
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
        reviewer_before = self._member_for_chat(reviewer_tg) if reviewer_tg is not None else None
        groups_before = self._group_allowlist()
        if reviewer_tg is not None and not self.is_admin(reviewer_before):
            raise ValueError('管理员权限已变化，未执行换绑')
        if row["status"] == "pending" and approve and int(row.get("expires_at") or 0) > time.time():
            if self._emby is None:
                raise ValueError("无法核验 Emby，请稍后重试")
            try:
                users = await self._emby.list_users()
            except Exception:  # noqa: BLE001 - public error must not contain adapter secrets
                raise ValueError("无法核验 Emby，请稍后重试") from None
            if not users or not any(str(u.get("Id")) == row["emby_user_id"] for u in users):
                raise ValueError("账号已不存在或 Emby 不可用，未更改绑定")
        if reviewer_tg is not None:
            reviewer_after = self._member_for_chat(reviewer_tg)
            if (not self.is_admin(reviewer_after)
                    or reviewer_after['emby_user_id'] != reviewer_before['emby_user_id']
                    or self._group_allowlist() != groups_before):
                raise ValueError("管理员身份、权限或审核群已变化，未执行换绑")
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
