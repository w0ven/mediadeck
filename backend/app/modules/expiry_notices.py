"""Confirmed expiry deletions use the existing durable Bot jobs and receipt group.

Only the remote DELETE success hook stages a job. No historical scans, account
lookups or deletion calls occur during delivery/retry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from html import escape
from uuid import uuid4

from app.modules.economy_rules import BEIJING, economy_write, encode
from app.modules.report_delivery import CALL_DELIVERY, failure, record_failure
from app.modules.usage import run_usage_io

KIND = 'activity.expiry.notice'


def destination(config):
    bot_id = str(config.get('bot_token') or '').split(':', 1)[0]
    chat = str(config.get('registration_notify_chat_id') or '').strip()
    if not bot_id.isdecimal() or int(bot_id) <= 0 or not chat:
        return None
    return bot_id, chat, config.get('registration_notify_thread_id')


class ExpiryNotices:
    def __init__(self, db, config):
        self.db, self.config = db, config

    def stage(self, member):
        """Called only after an actual successful remote DELETE, before local removal."""
        target = destination(self.config())
        if not member or not target:
            return
        bot_id, chat, thread = target
        now = int(time.time())
        tg = str(member.get('tg_user_id') or '')
        # Never use the media login as a public label. Match existing safe
        # member labels when there is no linked Telegram identity.
        label = str(member.get('tg_display_name') or '').strip()
        handle = str(member.get('tg_username') or '').strip().lstrip('@')
        if tg.isdecimal() and int(tg) > 0:
            label = label or ('@' + handle if handle else f'TG {tg}')
            who = f'<a href="tg://user?id={tg}">{escape(label[:80])}</a>'
        else:
            who = '未绑定 Telegram 的成员'
        expiry = member.get('expires_at_effective', member.get('expires_at'))
        text = (f'🗑 {who} 到期清理成功，账号已删除。\n'
                f'到期：{datetime.fromtimestamp(expiry, BEIJING).strftime("%Y-%m-%d %H:%M")}\n'
                f'清理：{datetime.fromtimestamp(now, BEIJING).strftime("%Y-%m-%d %H:%M")}（北京时间）')
        payload = {'chat_id': chat, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
        if thread:
            payload['message_thread_id'] = thread
        key = 'expiry-delete:' + hashlib.sha256(f'{member["emby_user_id"]}:{expiry}'.encode()).hexdigest()
        with economy_write(self.db) as conn:
            conn.execute('INSERT OR IGNORE INTO play_jobs(bot_id,job_key,kind,payload_json,created_at,due_at,expires_at) '
                         'VALUES(?,?,?,?,?,?,?)', (bot_id, key, KIND, encode(payload), now, now, now+86400))

    def claim(self):
        now = int(time.time())
        target = destination(self.config())
        if not target or not self.config().get('enabled'):
            return None
        bot_id, chat, thread = target
        with economy_write(self.db) as conn:
            # A lost acknowledgement is never an automatic re-send instruction.
            conn.execute("UPDATE play_jobs SET state='unknown',last_error='发送结果不明，需人工核实' "
                         "WHERE kind=? AND state='sending' AND lease_until<=?", (KIND, now))
            rows = conn.execute("SELECT * FROM play_jobs WHERE kind=? AND state IN ('queued','retry') AND due_at<=? ORDER BY id",
                                (KIND, now)).fetchall()
            for raw in rows:
                row = dict(raw)
                payload = json.loads(row['payload_json'])
                if (row['bot_id'] != bot_id or payload['chat_id'] != chat
                        or (payload.get('message_thread_id') or None) != (thread or None)):
                    conn.execute("UPDATE play_jobs SET state='cancelled',last_error='通知目标已变化，未转发' WHERE id=?", (row['id'],))
                    continue
                if row['expires_at'] <= now:
                    conn.execute("UPDATE play_jobs SET state='failed',last_error='超过自动发送时限' WHERE id=?", (row['id'],))
                    continue
                token = uuid4().hex
                conn.execute("UPDATE play_jobs SET state='sending',attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?",
                             (now+90, token, row['id']))
                return dict(row, attempts=row['attempts']+1, lease_token=token)
        return None

    def finish(self, row, result, error):
        now = int(time.time())
        mid = result.get('message_id') if isinstance(result, dict) else None
        outcome = {'state': 'sent', 'reason': '', 'next_at': 0, 'attempts': row['attempts']}
        if type(mid) is not int or mid <= 0:
            record_failure(outcome, error or failure(), now)
        self.db.execute('UPDATE play_jobs SET state=?,last_error=?,due_at=?,lease_until=0,result_json=? '
                        "WHERE id=? AND state='sending' AND lease_token=?",
                        (outcome['state'], outcome['reason'], outcome['next_at'],
                         encode({'message_id': mid} if outcome['state'] == 'sent' else {}), row['id'], row['lease_token']))

    async def deliver(self, bot):
        if not bot or not getattr(bot, 'enabled', False):
            return 0
        # Finish acknowledgement persistence even when plugin shutdown cancels
        # the caller; a later process must not re-send a confirmed success.
        task = asyncio.create_task(self._deliver(bot))
        cancelled = None
        while True:
            try:
                sent = await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                cancelled = exc
        if cancelled:
            raise cancelled
        return sent

    @staticmethod
    async def _allowed_origin(bot, chat_id):
        allowed = bot._group_allowlist()
        if chat_id in allowed:
            return True
        if any(chat.startswith('@') for chat in allowed):
            chat = await bot._call('getChat', {'chat_id': chat_id}, timeout=15)
            return isinstance(chat, dict) and str(chat.get('id')) == chat_id and bot._group_chat_allowed(chat)
        return False

    async def _deliver(self, bot):
        sent = 0
        for _ in range(20):
            row = await run_usage_io(self.claim)
            if not row:
                break
            payload = json.loads(row['payload_json'])
            result, error = None, None
            CALL_DELIVERY.set(None)
            try:
                # Same single configured group as registration receipts. Never
                # broaden to all interaction groups or redirect a frozen job.
                target = destination(self.config())
                if (not bot.enabled or target != (row['bot_id'], payload['chat_id'], payload.get('message_thread_id'))
                        or not await self._allowed_origin(bot, payload['chat_id'])):
                    error = {'state': 'failed', 'reason': '通知目标不可用，未发送'}
                else:
                    result = await bot._call('sendMessage', payload)
                    error = CALL_DELIVERY.get()
            except Exception as exc:  # noqa: BLE001 - classify uncertainty without storing credentials
                error = failure(exc=exc)
            await run_usage_io(self.finish, row, result, error)
            sent += bool(isinstance(result, dict) and type(result.get('message_id')) is int and result['message_id'] > 0)
        return sent
