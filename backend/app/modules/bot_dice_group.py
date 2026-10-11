"""Group-size dispatch, durable coalesced edits and shared ledger navigation."""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from html import escape

from app.modules.dice_group import TERMINAL, is_group
from app.modules.dice_group_view import buttons, group_card, report_pages
from app.modules.economy_rules import economy_write
from app.modules.group_points import GroupPointsError
from app.modules.play_money import PlayError


class GroupDiceBotMixin:
    async def _dice_group_bet(self, message):
        parts = str(message.get('text') or '').strip().split()
        verb = parts[0] if parts else ''
        if verb.split('@')[0] not in ('/大', '/小'):
            return False
        self._check_bot_identity()
        if '@' in verb and verb.split('@', 1)[1].lower() != (self._bot_username or '').lower():
            return True
        if self._db is None or not self._play_entry_valid(message):
            return True
        chat = message.get('chat') or {}
        if chat.get('type') not in ('group', 'supergroup') or not self._group_chat_allowed(chat):
            return True
        try:
            if len(parts) != 2 or not re.fullmatch(r'[0-9]{1,16}', parts[1]):
                raise PlayError('群内发送：/大 金额 或 /小 金额，金额须为正整数')
            row = self._dice_service().group_bet(message, verb.split('@')[0][1:], int(parts[1]))
            self._dice_group_schedule(row['nonce'])
        except (PlayError, GroupPointsError) as exc:
            await self._dice_feedback(message, '🍃 '+escape(str(exc)))
            self._dice_cleanup(message)
        return True

    def _dice_group_schedule(self, nonce):
        # Financial state is already committed. One sleeper per round, not per bet.
        tasks = getattr(self, '_dice_group_tasks', None)
        if tasks is None:
            tasks = self._dice_group_tasks = {}
        if nonce in tasks and not tasks[nonce].done():
            return

        async def update():
            await asyncio.sleep(2.1)
            try:
                await self._dice_group_refresh(nonce)
            except Exception:  # noqa: BLE001 - persisted revision is retried by the worker
                self._last_error = '骰局群卡同步待重试'
        task = asyncio.create_task(update())
        tasks[nonce] = task
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)
        task.add_done_callback(lambda done: tasks.pop(nonce, None) if tasks.get(nonce) is done else None)

    async def _dice_group_refresh(self, nonce):
        service = self._dice_service()
        with economy_write(self._db) as conn:
            row = service.get(nonce)
            if (not is_group(row) or not self.enabled
                    or not self._group_chat_allowed({'id': int(row['chat_id']), 'type': 'supergroup'})):
                return
            job = conn.execute("SELECT * FROM dice_messages WHERE nonce=? AND purpose='report:0' AND state='sent'", (nonce,)).fetchone()
            mid = job['mid'] if job else row['card_mid']
            if row['state'] in TERMINAL and not job:
                return
            clock = time.time()
            if (not mid or row['group_lease'] > clock or row['group_due'] > clock or row['group_retry'] > clock
                    or (row['group_rendered'] >= row['group_revision'] and row['group_render_mid'] == mid)):
                return
            token = secrets.token_hex(12)
            row, snapshot = service.group_snapshot(nonce)
            body, keys = group_card(row, snapshot, report_body=job['body'] if job else None)
            conn.execute('UPDATE dice_rounds SET group_token=?,group_lease=? WHERE nonce=?', (token, clock+90, nonce))
        from app.modules.report_delivery import CALL_DELIVERY
        context = CALL_DELIVERY.set(None)
        try:
            good = await self._dice_edit(int(row['chat_id']), mid, body, keys)
            problem = CALL_DELIVERY.get() or {}
            with economy_write(self._db) as conn:
                if good:
                    conn.execute('UPDATE dice_rounds SET group_rendered=?,group_render_mid=?,group_lease=0,group_retry=0,group_due=? WHERE nonce=? AND group_token=?', (row['group_revision'], mid, time.time()+2, nonce, token))
                else:
                    conn.execute('UPDATE dice_rounds SET group_lease=0,group_retry=? WHERE nonce=? AND group_token=?', (time.time()+max(3, int(problem.get('retry_after') or 0)), nonce, token))
        finally:
            CALL_DELIVERY.reset(context)

    async def _dice_size_callback(self, data, message, actor, callback_id):
        try:
            parts = data.split(':')
            if len(parts) != 3 or parts[0] != 'dg' or not re.fullmatch('[0-9a-f]{24}', parts[1]):
                raise PlayError('骰局按钮无效')
            nonce, op = parts[1:]
            service = self._dice_service()
            with economy_write(self._db) as conn:
                row = service.get(nonce)
                service.group_context(conn, row, message, actor, data)
                if op in ('artopen', 'artresult'):
                    page, view = row['group_page'], row['group_view']
                elif op == 'latest':
                    page, view = -1, 'ledger'
                elif re.fullmatch(r'page[0-9]{1,9}', op):
                    page, view = int(op[4:]), 'ledger'
                    count = conn.execute('SELECT COUNT(*) FROM dice_tickets WHERE nonce=?', (nonce,)).fetchone()[0]
                    if page >= max(1, (count+7)//8):
                        raise PlayError('流水页码无效')
                elif op in ('details', 'rules', 'proof', 'summary'):
                    page, view = row['group_page'], op
                else:
                    raise PlayError('按钮无效')
                conn.execute('UPDATE dice_rounds SET group_page=?,group_view=?,group_revision=group_revision+1,group_due=0 WHERE nonce=?', (page, view, nonce))
            await self._answer_callback(callback_id)
            await self._dice_group_refresh(nonce)
            await self._dice_deliver(nonce)
        except (PlayError, GroupPointsError) as exc:
            await self._answer_callback(callback_id, str(exc))

    def _dice_group_reports(self, row):
        tickets = self._db.query('SELECT * FROM dice_tickets WHERE nonce=? ORDER BY rowid', (row['nonce'],))
        from app.modules.dice_art import art_name
        with economy_write(self._db) as conn:
            self._dice_service().queue_message(conn, row['nonce'], 'art:result', row['chat_id'], art_name(row), [[{'text': '查看战报', 'callback_data': f'dg:{row["nonce"]}:artresult'}]], thread=row['thread_id'])
            for i, body in enumerate(report_pages(row, tickets)):
                # Only first report is the shared interactive history anchor.
                keys = buttons(row['nonce'], terminal=True, pages=max(1, (len(tickets)+7)//8)) if i == 0 else ()
                self._dice_service().queue_message(conn, row['nonce'], f'report:{i}', row['chat_id'], body, keys, row['thread_id'])
