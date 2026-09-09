"""Delivery of committed gift registrations. Never creates or consumes accounts."""
from __future__ import annotations

import asyncio
import hashlib
import time
from html import escape
from typing import Any

from app.modules.usage import run_usage_io


class GiftReceipts:
    def __init__(self, bot: Any):
        self.bot = bot
        self.db = bot._db
        self._lock = asyncio.Lock()

    def bot_id(self) -> str:
        return self.bot._token().split(':', 1)[0]

    def stage(self, conn: Any, admission: Any, member: dict, group_name: str) -> int | None:
        """Called inside the SAME transaction as binding + credential consumption."""
        if not admission or admission.via != 'admin' or not admission.credential:
            return None
        grant = conn.execute('SELECT * FROM admin_grants WHERE tg_user_id=? AND gift_code=?',
                             (admission.tg_user_id, admission.credential)).fetchone()
        if not grant or not grant['used_at'] or not grant['origin_chat_id']:
            return None  # legacy/private gifts have no authorised group origin
        tg_id = str(grant['tg_user_id'])
        expires = member.get('expires_at_effective', member.get('expires_at'))
        term = time.strftime('%Y-%m-%d 到期', time.localtime(expires)) if expires else '永久'
        body = (f'🎉 <a href="tg://user?id={tg_id}">TG {tg_id}</a> 注册成功\n'
                f'账号：<b>{escape(str(member["username"]))}</b>\n'
                f'{escape(group_name)} · {term}')
        now = int(time.time())
        key = hashlib.sha256(admission.credential.encode()).hexdigest()
        conn.execute('INSERT INTO tg_gift_receipts(gift_key,grant_id,tg_user_id,emby_user_id,bot_id,chat_id,'
                     'origin_message_id,thread_id,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) '
                     'ON CONFLICT(gift_key) DO NOTHING',
                     (key, grant['id'], tg_id, member['emby_user_id'], grant['origin_bot_id'],
                      grant['origin_chat_id'], grant['origin_message_id'], grant['origin_thread_id'], body, now, now))
        return int(conn.execute('SELECT id FROM tg_gift_receipts WHERE gift_key=?', (key,)).fetchone()[0])

    async def retry_menu(self, tg_id: str) -> list:
        row = await run_usage_io(self.db.one,
            "SELECT id FROM tg_gift_receipts WHERE tg_user_id=? AND bot_id=? AND status!='sent' ORDER BY id DESC LIMIT 1",
            (tg_id, self.bot_id()))
        if not row or not row.get('id'):
            return []
        return [[{'text': '📨 注册群回执 / 重试', 'callback_data': f'gift_receipt_retry:{row["id"]}'}]]

    async def _allowed_origin(self, chat_id: str) -> bool:
        allowed = self.bot._group_allowlist()
        if chat_id in allowed:
            return True
        if any(chat.startswith('@') for chat in allowed):
            chat = await self.bot._call('getChat', {'chat_id': chat_id}, timeout=15)
            return (isinstance(chat, dict) and str(chat.get('id')) == chat_id
                    and self.bot._group_chat_allowed(chat))
        return False

    async def deliver(self, receipt_id: int, *, retry_tg: str | None = None) -> dict | None:
        # Keep ownership until send + delivery-state persistence settle. A
        # shutdown cannot close SQLite while a worker is still recording success.
        task = asyncio.create_task(self._deliver(receipt_id, retry_tg=retry_tg))
        cancelled = None
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                cancelled = exc
        if cancelled is not None:
            raise cancelled
        return result

    async def _deliver(self, receipt_id: int, *, retry_tg: str | None) -> dict | None:
        async with self._lock:
            row = await run_usage_io(self.db.one, 'SELECT * FROM tg_gift_receipts WHERE id=?', (receipt_id,))
            if not row or row.get('bot_id') != self.bot_id():
                return None
            if retry_tg is not None and row['tg_user_id'] != retry_tg:
                return None
            if row['status'] == 'sent' or row['attempts'] >= 2:
                return row
            if row['status'] != 'pending' and (retry_tg is None or row['status'] != 'failed'):
                return row
            member = await run_usage_io(self.bot._member_for_chat, row['tg_user_id'])
            if (not member or str(member['emby_user_id']) != row['emby_user_id']
                    or not self.bot.enabled or not await self._allowed_origin(row['chat_id'])):
                await run_usage_io(self.db.execute,
                    "UPDATE tg_gift_receipts SET status='failed',last_error='binding_or_origin_unavailable',updated_at=? WHERE id=?",
                    (int(time.time()), receipt_id))
                return await run_usage_io(self.db.one, 'SELECT * FROM tg_gift_receipts WHERE id=?', (receipt_id,))
            changed = await run_usage_io(self.db.execute,
                "UPDATE tg_gift_receipts SET status='sending',attempts=attempts+1,updated_at=? WHERE id=? AND status=? AND attempts=?",
                (int(time.time()), receipt_id, row['status'], row['attempts']))
            if not changed:
                return None
            mid = None
            try:
                # New notice, not a reply to the potentially deleted gift card.
                # Explicit saved topic prevents leaking the private chat context.
                mid = await self.bot.send_message(row['chat_id'], row['body'], thread_id=row['thread_id'])
            except Exception:  # noqa: BLE001 - no transport/credential text in durable errors
                mid = None
            await run_usage_io(self.db.execute,
                'UPDATE tg_gift_receipts SET status=?,message_id=?,last_error=?,updated_at=? WHERE id=?',
                ('sent' if mid else 'failed', mid, '' if mid else 'telegram_delivery_unconfirmed', int(time.time()), receipt_id))
            return await run_usage_io(self.db.one, 'SELECT * FROM tg_gift_receipts WHERE id=?', (receipt_id,))

    async def recover(self) -> None:
        # A previous process may have sent before losing its acknowledgement.
        # Never automatically re-send that uncertain outcome after restart.
        await run_usage_io(self.db.execute,
            "UPDATE tg_gift_receipts SET status='failed',last_error='delivery_interrupted' WHERE bot_id=? AND status='sending'",
            (self.bot_id(),))
        await self.flush()

    async def flush(self) -> None:
        if not self.bot.enabled:
            return  # disabling the Bot is not a failed delivery attempt
        rows = await run_usage_io(self.db.query,
            "SELECT id FROM tg_gift_receipts WHERE bot_id=? AND status='pending' ORDER BY id LIMIT 50", (self.bot_id(),))
        for row in rows:
            await self.deliver(int(row['id']))

    async def run(self) -> None:
        try:
            await self.recover()
            while True:
                await asyncio.sleep(30)
                await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - durable rows remain recoverable, expose worker failure
            self.bot._last_error = '注册群回执处理失败，请重启 Bot 或在私聊重试回执'
