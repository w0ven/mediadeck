"""Reply-to-message group transfers and explicit system-admin minting.

Financial intent is durable and immutable. Telegram chat-admin status, names,
forward origins and client callback parameters never confer mint authority.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from html import escape

from app.modules.economy_rules import economy_write, encode, receipt, save_receipt

COMMANDS = {"/transfer", "/转账"}
CALLBACKS = ("gpok:", "gpcancel:")


class GroupPointsError(ValueError):
    """Safe to show in a group: never includes a member's balance."""


def positive_amount(raw):
    text = str(raw)
    if isinstance(raw, (bool, float)) or not re.fullmatch(r"[0-9]{1,19}", text):
        raise GroupPointsError("金额必须是正整数，例如 /transfer 100；不能用负数扣分。")
    amount = int(text)
    if not 0 < amount <= 2**63 - 1:
        raise GroupPointsError("金额必须是可存储的正整数。")
    return amount


def reliable_user(message):
    user = (message or {}).get("from") or {}
    if (
        not isinstance(user, dict)
        or (message or {}).get("sender_chat")
        or (message or {}).get("is_automatic_forward")
        or user.get("is_bot") is not False
    ):
        raise GroupPointsError(
            "不能使用匿名管理员、频道代发或机器人消息；请回复真人本人发出的消息。"
        )
    uid = user.get("id")
    if type(uid) is not int or uid <= 0:
        raise GroupPointsError("无法核实真实Telegram用户ID。")
    return str(uid)


def public_transfer_error(exc):
    text = str(exc)
    if "积分不足" in text or "余额" in text:
        return GroupPointsError("积分不足，未转账；余额请在本人私聊查看。")
    if "每日" in text or "今日已转" in text:
        return GroupPointsError("超过每日转出上限，未转账。")
    # Business plugin errors are controlled messages (not HTTP exceptions).
    return GroupPointsError(text)


class GroupPointsService:
    def __init__(self, db, members, points, transfer, is_admin, allowed, transfer_enabled):
        self.db, self.members, self.points, self.transfer = db, members, points, transfer
        self.is_admin, self.allowed, self.transfer_enabled = is_admin, allowed, transfer_enabled

    def _group(self, chat):
        if chat.get("type") not in ("group", "supergroup") or not self.allowed(chat):
            raise GroupPointsError("仅限现有授权群内操作。")
        return str(chat["id"])

    def get(self, nonce):
        return self.db.one("SELECT * FROM group_points_intents WHERE nonce=?", (str(nonce),))

    def prepare(self, message, amount, thread_id=0):
        chat = message.get("chat") or {}
        cid = self._group(chat)
        actor_tg = reliable_user(message)
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict) or not reply.get("message_id"):
            raise GroupPointsError("请回复收款人本人消息发送 /transfer 100 或 /转账 100。")
        if (reply.get("chat") or {}).get("id", chat["id"]) != chat["id"]:
            raise GroupPointsError("不能使用其他群/外部引用消息作为收款目标。")
        # Always the actual author of the replied message, NOT forward_origin.
        to_tg = reliable_user(reply)
        amount = positive_amount(amount)
        mid = message.get("message_id")
        if type(mid) is not int or mid <= 0:
            raise GroupPointsError("无法核实命令消息ID。")
        with economy_write(self.db) as conn:
            sender = self.members.find_by_telegram(actor_tg)
            target = self.members.find_by_telegram(to_tg)
            if not sender or not target:
                raise GroupPointsError("发起人和收款人都必须已绑定系统账号，不能按显示名匹配。")
            mode = "mint" if self.is_admin(sender) else "transfer"
            expected = {
                "chat_id": cid,
                "command_message_id": mid,
                "actor_tg_id": actor_tg,
                "actor_user_id": sender["emby_user_id"],
                "to_tg_id": to_tg,
                "to_user_id": target["emby_user_id"],
                "reply_message_id": int(reply["message_id"]),
                "amount": amount,
                "mode": mode,
                "thread_id": int(thread_id or 0),
            }
            existing = conn.execute(
                "SELECT * FROM group_points_intents WHERE chat_id=? AND command_message_id=?",
                (cid, mid),
            ).fetchone()
            if existing:
                if any(existing[k] != v for k, v in expected.items()):
                    raise GroupPointsError("同一命令消息已绑定其他操作，请重新发送新命令。")
                return dict(existing)
            fee = 0
            if mode == "transfer":
                if not self.transfer or not self.transfer_enabled():
                    raise GroupPointsError("普通积分转账未开启。")
                if sender["emby_user_id"] == target["emby_user_id"]:
                    raise GroupPointsError("不能转给自己。")
                ok, reason = self.transfer.can_transfer(sender["emby_user_id"], amount)
                if not ok:
                    raise public_transfer_error(reason)
                fee = self.transfer.fee_for(amount)
            nonce = secrets.token_hex(16)
            now = int(time.time())
            values = dict(
                expected,
                nonce=nonce,
                to_name=str(target.get("username") or "已绑定用户"),
                fee=fee,
                created_at=now,
                expires_at=now + 600,
            )
            columns = ",".join(values)
            conn.execute(
                f"INSERT INTO group_points_intents({columns}) VALUES({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM group_points_intents WHERE nonce=?", (nonce,)
                ).fetchone()
            )

    def bind_card(self, nonce, mid):
        with economy_write(self.db) as conn:
            conn.execute(
                "UPDATE group_points_intents SET confirmation_message_id=? WHERE nonce=? AND confirmation_message_id IS NULL AND status='pending'",
                (int(mid), nonce),
            )
            row = conn.execute(
                "SELECT confirmation_message_id FROM group_points_intents WHERE nonce=?", (nonce,)
            ).fetchone()
            return bool(row and row["confirmation_message_id"] == int(mid))

    def resolve(self, nonce, actor_tg, chat, message_id, thread_id=0, *, cancel=False):
        cid = self._group(chat)
        with economy_write(self.db) as conn:
            record = conn.execute(
                "SELECT * FROM group_points_intents WHERE nonce=?", (str(nonce),)
            ).fetchone()
            if not record:
                raise GroupPointsError("确认卡不存在，请重新发起。")
            row = dict(record)
            if (
                row["chat_id"] != cid
                or row["actor_tg_id"] != str(actor_tg)
                or row["confirmation_message_id"] != int(message_id)
                or row["thread_id"] != int(thread_id or 0)
            ):
                raise GroupPointsError("只有发起人能在原授权群、原话题、原确认卡确认/取消。")
            if row["status"] == "confirmed":
                return json.loads(row["result_json"])
            if row["status"] == "cancelled":
                raise GroupPointsError("此操作已取消，未执行。")
            if row["expires_at"] <= time.time():
                raise GroupPointsError("确认已过期，请重新发起。")
            if cancel:
                conn.execute(
                    "UPDATE group_points_intents SET status='cancelled' WHERE nonce=?", (nonce,)
                )
                return {"ok": False, "cancelled": True, "mode": row["mode"]}
            sender = self.members.find_by_telegram(row["actor_tg_id"])
            target = self.members.find_by_telegram(row["to_tg_id"])
            if (
                not sender
                or not target
                or sender["emby_user_id"] != row["actor_user_id"]
                or target["emby_user_id"] != row["to_user_id"]
            ):
                raise GroupPointsError("账号绑定已变化，请重新发起；未执行本次操作。")
            current_mode = "mint" if self.is_admin(sender) else "transfer"
            if current_mode != row["mode"]:
                raise GroupPointsError("系统管理员权限已变化，请重新发起；不会切换发放/扣款模式。")
            amount = positive_amount(row["amount"])
            now = int(time.time())
            key = "gp:" + nonce
            actor = "tg:" + row["actor_tg_id"]
            if row["mode"] == "mint":
                request = {
                    k: row[k]
                    for k in (
                        "chat_id",
                        "command_message_id",
                        "actor_tg_id",
                        "to_tg_id",
                        "to_user_id",
                        "amount",
                        "mode",
                    )
                }
                prior = receipt(conn, "admin.mint", key, row["actor_user_id"], request)
                if prior is None:
                    if self.points.balance(row["to_user_id"]) + amount > 2**63 - 1:
                        raise GroupPointsError("积分总额超出可存储范围，未发放。")
                    self.points._apply(
                        conn, row["to_user_id"], amount, "admin.mint", key, actor, now
                    )
                    prior = {
                        "ok": True,
                        "mode": "mint",
                        "amount": amount,
                        "fee": 0,
                        "received": amount,
                    }
                    save_receipt(conn, "admin.mint", key, row["actor_user_id"], request, prior)
                result = prior
            else:
                if not self.transfer or not self.transfer_enabled():
                    raise GroupPointsError("普通积分转账未开启。")
                try:
                    transferred = self.transfer.transfer(
                        row["actor_user_id"],
                        row["to_user_id"],
                        amount,
                        request_id=key,
                        expected_fee=row["fee"],
                        conn=conn,
                        actor=actor,
                    )
                except ValueError as exc:
                    raise public_transfer_error(exc) from None
                result = {k: transferred[k] for k in ("ok", "amount", "fee", "received")}
                result["mode"] = "transfer"
            detail = {
                k: row[k]
                for k in (
                    "chat_id",
                    "command_message_id",
                    "reply_message_id",
                    "actor_tg_id",
                    "actor_user_id",
                    "to_tg_id",
                    "to_user_id",
                    "amount",
                    "fee",
                    "mode",
                )
            }
            detail["request_id"] = key
            conn.execute(
                "INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)",
                (now, actor, "points.group." + row["mode"], row["to_user_id"], encode(detail)),
            )
            conn.execute(
                "UPDATE group_points_intents SET status='confirmed',result_json=? WHERE nonce=?",
                (encode(result), nonce),
            )
            return result


class GroupPointsBotMixin:
    def _group_points_service(self):
        if self._db is None or self._points is None:
            raise GroupPointsError("积分服务不可用。")
        return GroupPointsService(
            self._db,
            self._members,
            self._points,
            self._plugin("points_transfer"),
            self.is_admin,
            self._group_chat_allowed,
            lambda: self._plugin_on("points_transfer"),
        )

    async def _group_points_command(self, message):
        text = str(message.get("text") or "").strip()
        parts = text.split()
        first = parts[0].lower() if parts else ""
        if first.split("@", 1)[0] not in COMMANDS:
            return False
        self._check_bot_identity()
        if "@" in first and (
            not self._bot_username or first.split("@", 1)[1] != self._bot_username.lower()
        ):
            return True  # unknown/other bot identity fails closed, no mint intent
        chat_id = message["chat"]["id"]
        try:
            if len(parts) != 2:
                raise GroupPointsError(
                    "用法：回复真人收款人消息，发送 /transfer 100 或 /转账 100。"
                )
            service = self._group_points_service()
            row = service.prepare(message, parts[1], self._thread_id(message))
            if row["confirmation_message_id"]:
                return True  # duplicate delivery does not create a second card/operation
            if row["status"] != "pending" or row["expires_at"] <= time.time():
                raise GroupPointsError("此请求已结束，请重新发起。")
            mint = row["mode"] == "mint"
            body = "🛠 <b>管理员发放</b>" if mint else "💸 <b>普通积分转账</b>"
            if not mint:
                body += "\n确认后从本人积分扣除数量，手续费包含在该数量内。"
            body += (
                f"\n收款账号：<b>{escape(row['to_name'])}</b>\n收款人TG ID：<code>{row['to_tg_id']}</code>"
                f"\n数量：<b>{row['amount']}</b> · 手续费：{row['fee']} · 到账：<b>{row['amount'] - row['fee']}</b>"
                "\n仅发起人可确认，10分钟有效。"
            )
            keys = [
                [
                    {
                        "text": "✅ 确认发放" if mint else "✅ 确认转账",
                        "callback_data": "gpok:" + row["nonce"],
                    },
                    {"text": "取消", "callback_data": "gpcancel:" + row["nonce"]},
                ]
            ]
            # Independent durable card: do not borrow/replace an admin target or
            # an unrelated member menu. This also survives a Bot process restart.
            payload = {
                "chat_id": chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keys},
                "reply_parameters": {"message_id": message["message_id"]},
            }
            if row["thread_id"]:
                payload["message_thread_id"] = row["thread_id"]
            posted = await self._call("sendMessage", payload)
            mid = posted.get("message_id") if isinstance(posted, dict) else None
            if type(mid) is not int or mid <= 0:
                raise GroupPointsError("确认卡发送失败或结果未知；未发放/转账，可重新发送命令。")
            if not service.bind_card(row["nonce"], mid):
                await self._call(
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": mid,
                        "text": "重复请求；请使用原确认卡，未重复发放。",
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
            return True
        except GroupPointsError as exc:
            await self.send(chat_id, "❌ " + escape(str(exc)))
            return True
        except (sqlite3.Error, OverflowError) as exc:
            self._last_error = type(exc).__name__
            await self.send(
                chat_id, "❌ 积分数据库暂不可用，请稍后重试原命令；未创建可执行的新确认卡。"
            )
            return True

    async def _group_points_callback(self, data, message, actor_tg, callback_id):
        try:
            service = self._group_points_service()
            nonce = data.split(":", 1)[1]
            intent = service.get(nonce)
            if (
                intent
                and intent["actor_tg_id"] == str(actor_tg)
                and intent["chat_id"] == str((message.get("chat") or {}).get("id"))
                and intent["confirmation_message_id"] == message.get("message_id")
                and intent["status"] == "pending"
                and not await self.membership.gate(message["chat"]["id"], str(actor_tg))
            ):
                await self._answer_callback(callback_id, "请先通过现有成员门禁，未执行。")
                return
            result = service.resolve(
                nonce,
                actor_tg,
                message.get("chat") or {},
                message.get("message_id"),
                self._thread_id(message),
                cancel=data.startswith("gpcancel:"),
            )
        except (GroupPointsError, ValueError, TypeError) as exc:
            # ACK only: a different user cannot edit the owner's confirmation.
            await self._answer_callback(
                callback_id,
                str(exc) if isinstance(exc, GroupPointsError) else "确认信息无效，未执行。",
            )
            return
        except (sqlite3.Error, OverflowError) as exc:
            self._last_error = type(exc).__name__
            await self._answer_callback(callback_id, "积分数据库暂不可用，请稍后重试原确认卡。")
            return
        await self._answer_callback(
            callback_id, "已取消" if result.get("cancelled") else "操作已完成"
        )
        text = (
            "已取消，未执行。"
            if result.get("cancelled")
            else (
                "✅ 积分奖励发放成功"
                if result["mode"] == "mint"
                else "✅ 积分转账成功"
            )
        )
        if not result.get("cancelled"):
            row = self._db.one('SELECT to_name,to_tg_id FROM group_points_intents WHERE nonce=?', (data.split(':', 1)[1],))
            if row:
                text += f"\n收款人：{row['to_name']} · TG ID：{row['to_tg_id']}"
            text += f"\n数量：{result['amount']} · 手续费：{result['fee']} · 对方到账：{result['received']}"
        await self._call(
            "editMessageText",
            {
                "chat_id": message["chat"]["id"],
                "message_id": message["message_id"],
                "text": text,
                "reply_markup": {"inline_keyboard": []},
            },
        )
