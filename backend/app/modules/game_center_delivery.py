"""Durable single-card outbox. Unknown initial sends are never blindly repeated."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time

from app.modules.economy_rules import economy_write
from app.modules.report_delivery import CALL_DELIVERY, failure
from app.modules.usage import run_usage_io


class GameDelivery:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def claim(self, bot_id, now=None):
        now = time.time() if now is None else now
        with economy_write(self.db) as conn:
            # A crash could have occurred after Telegram accepted sendMessage.
            conn.execute("UPDATE gc_outbox SET state='unknown',lease_token='',lease_until=0,"
                         "last_error='发卡结果不明，未重复发送' WHERE bot_id=? AND state='sending' AND message_id IS NULL AND lease_until<=?", (bot_id, now))
            rows = conn.execute("SELECT * FROM gc_outbox WHERE bot_id=? AND state NOT IN ('unknown','deleted','sending') "
                                "AND due_at<=? AND lease_until<=? AND (revision>rendered_revision OR desired='delete') ORDER BY due_at,table_id LIMIT 50", (bot_id, now, now)).fetchall()
            for row in rows:
                row = dict(row)
                if row['desired'] == 'delete' and row['message_id'] is None:
                    conn.execute("UPDATE gc_outbox SET state='deleted',rendered_revision=revision WHERE table_id=? AND chat_id=?", (row['table_id'], row['chat_id']))
                    continue
                token = secrets.token_hex(12)
                op = 'delete' if row['desired'] == 'delete' else 'edit' if row['message_id'] else 'send'
                conn.execute('UPDATE gc_outbox SET state=?,lease_token=?,lease_until=?,attempts=attempts+1 WHERE table_id=? AND chat_id=?',
                             ('sending' if op == 'send' else 'sent', token, now + 90, row['table_id'], row['chat_id']))
                return {**row, 'lease_token': token, 'op': op}
        return None

    def done(self, job, result=None, error=None, now=None):
        now = time.time() if now is None else now
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM gc_outbox WHERE table_id=? AND chat_id=?', (job['table_id'], job['chat_id'])).fetchone()
            if not row or row['lease_token'] != job['lease_token']:
                return False
            success = bool(result)
            mid = result.get('message_id') if isinstance(result, dict) else None
            if job['op'] == 'send':
                success = type(mid) is int and mid > 0
            elif job['op'] == 'edit' and (error or {}).get('not_modified') or job['op'] == 'delete' and (error or {}).get('message_not_found'):
                success = True
            if success:
                conn.execute("UPDATE gc_outbox SET state=?,message_id=COALESCE(?,message_id),rendered_revision=MAX(rendered_revision,?),lease_token='',lease_until=0,due_at=0,last_error='' WHERE table_id=? AND chat_id=?",
                             ('deleted' if job['op'] == 'delete' else 'sent', mid, job['revision'], job['table_id'], job['chat_id']))
            else:
                error = error or {'state': 'unknown'}
                # Known Telegram rejection/connect failure is retryable. 5xx or a lost ACK
                # cannot prove sendMessage failed. Edits/deletes target a known message ID.
                uncertain = error.get('state') == 'unknown' or error.get('reason') == 'Telegram 服务暂不可用'
                state = 'unknown' if job['op'] == 'send' and uncertain else 'pending' if job['op'] == 'send' else 'sent'
                delay = max(min(300, 10 * 2 ** min(row['attempts'], 5)), int(error.get('retry_after') or 0))
                conn.execute("UPDATE gc_outbox SET state=?,lease_token='',lease_until=0,due_at=?,last_error=? WHERE table_id=? AND chat_id=?",
                             (state, now + delay, '发卡结果不明，未重复发送' if state == 'unknown' else '群卡操作未确认，等待重试', job['table_id'], job['chat_id']))
        return success

    async def deliver(self, limit=20):
        from app.modules.telegram import _CALL_ERROR
        bot = self.service.bot()
        if not bot or not bot.enabled:
            return
        bot_id = str(bot._token()).split(':')[0]
        for _ in range(limit):
            job = await run_usage_io(self.claim, bot_id)
            if not job:
                break
            # Revoking broadcast permission never permits new sends. Bound deletion is
            # cleanup, so it still runs after the game/broadcast switch is turned off.
            if job['op'] != 'delete':
                permitted = self.service.cfg()['broadcast_enabled']
                allowed = bot._group_chat_allowed({'id': job['chat_id'], 'type': 'supergroup'})
                if permitted and not allowed:
                    try:
                        chat = await bot._call('getChat', {'chat_id': job['chat_id']})
                    except Exception:  # noqa: BLE001 - read-only admission failure, no card sent
                        chat = None
                    allowed = (isinstance(chat, dict) and str(chat.get('id')) == job['chat_id']
                               and chat.get('type') in ('group', 'supergroup') and bot._group_chat_allowed(chat))
                if not permitted or not allowed:
                    await run_usage_io(self.done, job, error={'state': 'retry'})
                    continue
            payload = json.loads(job['payload_json'])
            if job['op'] == 'send':
                # A plain HTTPS page button is not a Telegram Mini App launch. Verify
                # Main Mini App support before sending any group recruitment card.
                try:
                    identity = await bot._call('getMe')
                except Exception:  # noqa: BLE001 - no send was attempted, safe to retry readiness
                    identity = None
                if (not isinstance(identity, dict) or not identity.get('has_main_web_app')
                        or str(identity.get('id')) != bot_id
                        or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', str(identity.get('username') or ''))):
                    await run_usage_io(self.done, job, error={'state': 'retry'})
                    continue
                payload['reply_markup']['inline_keyboard'][0][0]['url'] = (
                    f"https://t.me/{identity['username']}?startapp=gc_{job['table_id']}")
            if job['op'] == 'delete':
                method = 'deleteMessage'
                payload = {'chat_id': job['chat_id'], 'message_id': job['message_id']}
            else:
                method = 'sendMessage' if job['op'] == 'send' else 'editMessageText'
                payload.update(chat_id=job['chat_id'], parse_mode='HTML', disable_web_page_preview=True)
                if job['op'] == 'edit':
                    payload['message_id'] = job['message_id']
            CALL_DELIVERY.set(None)
            _CALL_ERROR.set('')
            try:
                result = await bot._call(method, payload)
                error = CALL_DELIVERY.get()
                if job['op'] == 'delete' and any(text in str(_CALL_ERROR.get() or '').lower()
                        for text in ('message to delete not found', 'message_id_invalid')):
                    error = {'message_not_found': True}
            except Exception as exc:  # noqa: BLE001 - safe delivery classification, no credential logs
                result, error = None, failure(exc=exc)
            await run_usage_io(self.done, job, result, error)

    async def run(self):
        while True:
            try:
                await self.deliver()
            except Exception:  # noqa: BLE001 - durable leases are recovered on subsequent ticks
                logging.getLogger(__name__).warning('Game-center outbox interrupted; durable jobs retained')
            await asyncio.sleep(1)
