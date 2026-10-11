"""Private, one-shot account deletion; never delete ledger/history/media rows."""
from __future__ import annotations

import secrets
import time
from html import escape

from app.modules.member_ops import execute_delete


class _SelfDeleteAdapter:
    def __init__(self, emby, authorize):
        self.emby = emby
        self.authorize = authorize

    async def list_users(self):
        # Do not let execute_delete infer success from an incomplete user list.
        return []

    async def delete_user(self, user_id):
        return await self.emby.delete_selfservice_user(user_id, authorize=self.authorize)


class AccountBotMixin:
    def _account_binding_revision(self, user_id):
        # Audit IDs detect even an unbind/rebind within the same second.
        row = self._db.one(
            "SELECT MAX(id) AS revision FROM audit_log WHERE subject=? AND action IN "
            "('member.telegram.bind','member.telegram.unbind','telegram.rebind.approved')",
            (user_id,),
        )
        return (row or {}).get("revision")

    async def _account_delete_callback(self, data, chat, mid, tg_id):
        if str(chat) != str(tg_id) or str(chat).startswith("-"):
            return
        key = self._pkey(chat)
        current = self._member_for_chat(tg_id)
        if data == "delete_account":
            self._pending.pop(key, None)
            if not current:
                return
            if self.is_admin(current):
                await self._edit(chat, mid, "管理员账号不能自助删除。", self.info_menu())
                return
            if self._emby is None:
                await self._edit(chat, mid, "Emby 未连接，不能删除账号。", self.info_menu())
                return
            self._rq_abandon(chat)
            self._admin_panels.pop((str(chat), int(mid)), None)
            nonce = secrets.token_hex(16)
            p = {"tg_id": str(tg_id), "user_id": str(current["emby_user_id"]),
                 "nonce": nonce, "message_id": mid,
                 "binding_revision": self._account_binding_revision(str(current["emby_user_id"]))}
            entry = ("account_delete", time.time() + 120, p)
            self._pending[key] = entry
            shown = await self._edit(
                chat, mid,
                "⚠️ <b>永久删除本人账号</b>\n账号：<code>"
                + escape(str(current.get("username") or p["user_id"]))
                + "</code>\n\n这不是解绑：将实际删除 Emby 账号及会员、设备记录。"
                "登录、会员权益、个人播放记录和收藏将无法恢复。"
                "原账号的积分、订单和玩法历史按现有规范保留，但不能通过新账号继承；"
                "未完成玩法按既有规则结算或取消，资金仍记在原账号，不转移到新账号。"
                "不会删除媒体文件，也不会删除邀请人。\n\n"
                "<b>不可恢复，确定永久注销吗？</b>（确认 2 分钟内有效）",
                [[{"text": "确认永久删除本人账号", "callback_data": f"delself:{nonce}:confirm"}],
                 [{"text": "取消 / 保留账号", "callback_data": f"delself:{nonce}:cancel"}]],
            )
            if self._pending.get(key) is entry:
                if shown:
                    p["message_id"] = self._panel.get(key, mid)
                else:
                    self._pending.pop(key, None)
            return
        waiting = self._pending.get(key)
        if not waiting or waiting[0] != "account_delete":
            return
        p = waiting[2]
        if waiting[1] <= time.time():
            self._pending.pop(key, None)
            return
        if (p["tg_id"] != str(tg_id) or p["message_id"] != mid
                or data not in (f"delself:{p['nonce']}:confirm", f"delself:{p['nonce']}:cancel")):
            return
        # Consume before any I/O, including cancel and failed confirmation.
        self._pending.pop(key, None)
        if data.endswith(":cancel"):
            await self._edit(chat, mid, "已取消，账号未删除。", self.info_menu())
            return

        def authorized():
            member = self._member_for_chat(tg_id)
            return bool(time.time() < waiting[1]
                        and member and str(member["emby_user_id"]) == p["user_id"]
                        and not self.is_admin(member)
                        and self._account_binding_revision(p["user_id"]) == p["binding_revision"])

        if not authorized():
            await self._edit(chat, mid, "绑定或权限已变化，未执行删除。", self.info_menu())
            return
        try:
            result = await execute_delete(
                self._members, _SelfDeleteAdapter(self._emby, authorized), p["user_id"],
                actor=f"tg:{tg_id}", cascade=False, delete_emby=True,
                confirm_ids=[p["user_id"]], authorize=authorized,
            )
        except Exception:  # noqa: BLE001 - remote deletion may have happened; no rollback claim
            await self._edit(chat, mid, "❌ 删除未确认全部完成，可能已删除远端账号。请联系管理员核查，勿重复操作。")
            return
        if not result.get("ok") or not result.get("deleted"):
            await self._edit(chat, mid, "❌ 删除未完成或远端未确认，未移除本地会员。请联系管理员核查。", self.info_menu())
            return
        await self._edit(chat, mid, "账号已永久删除（仅本人），媒体文件未删除。", self.guest_menu())
