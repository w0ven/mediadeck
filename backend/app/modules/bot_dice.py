"""Dice-only cards, persisted outbox and exact trusted-message cleanup."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from html import escape

from app.modules import dice_beacon as beacon
from app.modules import dice_group_view
from app.modules.bot_dice_group import GroupDiceBotMixin
from app.modules.checkin_cleanup import CleanupService
from app.modules.dice import DiceService
from app.modules.dice_group import GROUP_VERSION, is_group
from app.modules.dice_rules import POOLS, normalize, validate
from app.modules.dice_view import group_card, private_card, report_pages
from app.modules.economy_rules import economy_write, encode
from app.modules.game_ui import defer_ui
from app.modules.group_points import GroupPointsError
from app.modules.play_money import PlayError
from app.modules.report_delivery import CALL_DELIVERY, failure


class DiceCleanup(CleanupService):
    kind, key_prefix = 'dice.delete', 'dice'

    def enqueue(self, bot_id, chat, thread, mid, source, actor, *, now=None, due=None):
        if (not str(bot_id).isdecimal() or int(bot_id) <= 0 or type(chat) is not int or not chat
                or type(mid) is not int or mid <= 0 or type(thread) is not int or thread < 0
                or source not in ('command', 'feedback') or not str(actor).isdecimal() or int(actor) <= 0
                or (chat > 0 and str(chat) != str(actor))):
            raise ValueError('invalid dice cleanup provenance')
        clock = time.time() if now is None else now
        data = {'chat_id': chat, 'thread_id': thread, 'message_id': mid, 'source': source, 'actor_tg_id': str(actor)}
        with economy_write(self.db) as conn:
            key = f'dice:{chat}:{mid}'
            prior = conn.execute('SELECT * FROM play_jobs WHERE bot_id=? AND job_key=?', (str(bot_id), key)).fetchone()
            if prior:
                if prior['payload_json'] != encode(data):
                    raise ValueError('conflicting dice deletion provenance')
                return
            conn.execute('INSERT INTO play_jobs(bot_id,job_key,kind,payload_json,created_at,due_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                         (str(bot_id), key, self.kind, encode(data), clock, clock+60 if due is None else due, clock+48*3600))


class DiceBotMixin(GroupDiceBotMixin):
    def _dice_service(self):
        if self._db is None or self._points is None:
            raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return DiceService(self._db, self._members, self._points, lambda: self._plugin_on('dice'), self._group_chat_allowed, self._active_bot_id)

    def _dice_cleanup(self, message, mid=None, *, due=None):
        DiceCleanup(self._db).enqueue(self._active_bot_id, message['chat']['id'], self._thread_id(message) or 0,
                                      mid or message['message_id'], 'feedback' if mid else 'command', message['from']['id'], due=due)

    async def _dice_send(self, chat_id, text, keyboard=None, *, thread_id=None):
        data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
        if keyboard:
            data['reply_markup'] = {'inline_keyboard': keyboard}
        if thread_id:
            data['message_thread_id'] = thread_id
        result = await self._call('sendMessage', data)
        return result.get('message_id') if isinstance(result, dict) else None

    async def _dice_feedback(self, message, body):
        mid = await self._dice_send(message['chat']['id'], body, thread_id=self._thread_id(message))
        if type(mid) is int and mid > 0:
            self._dice_cleanup(message, mid)

    async def _dice_command(self, message):
        if await self._dice_group_bet(message):
            return True
        text = str(message.get('text') or '').strip()
        parts = text.split()
        verb = parts[0].lower() if parts else ''
        start = len(parts) == 2 and verb.split('@')[0] == '/start' and parts[1].startswith('dice_')
        command = verb.split('@')[0] in ('/骰局', '/dice', '/骰局帮助')
        if not command and not start:
            return await self._dice_amount_input(message)
        self._check_bot_identity()
        if '@' in verb and (not self._bot_username or verb.split('@')[1] != self._bot_username.lower()):
            return True
        chat = message.get('chat') or {}
        if not self._play_entry_valid(message) or chat.get('type') not in ('private', 'group', 'supergroup'):
            return True
        if chat['type'] != 'private' and not self._group_chat_allowed(chat):
            return True
        if self._db is None:
            return True
        try:
            service = self._dice_service()
            if start:
                if chat['type'] != 'private' or not re.fullmatch('dice_[0-9a-f]{24}', parts[1]):
                    raise PlayError('请从本局群卡进入本人私聊')
                session = service.new_session(parts[1][5:], message['from'])
                await self._dice_draw(session)
            elif verb.split('@')[0] == '/骰局帮助':
                await self._dice_feedback(message, dice_group_view.HELP)
            else:
                if len(parts) != 1:
                    raise PlayError('用法：/骰局；开局后回复群卡发送 /大 金额 或 /小 金额')
                if chat['type'] == 'private':
                    raise PlayError('请在现有授权群发送 /骰局')
                row = service.create(message)
                await self._dice_publish_open(row)
                row = service.get(row['nonce'])
                if row['command_mid'] != message['message_id'] and row['card_mid']:
                    group = row['chat_id']
                    path = (str(row['thread_id'])+'/' if row['thread_id'] else '')+str(row['card_mid'])
                    link = f'https://t.me/c/{group[4:]}/{path}' if group.startswith('-100') else ''
                    await self._dice_feedback(message, '<a href="'+link+'">前往本局</a>' if link else '已有骰局，请使用原群卡。')
            self._dice_cleanup(message)
        except (PlayError, GroupPointsError) as exc:
            await self._dice_feedback(message, '🍃 '+escape(str(exc)))
            self._dice_cleanup(message)
        return True

    async def _dice_amount_input(self, message):
        if self._db is None or (message.get('chat') or {}).get('type') != 'private' or not self._play_entry_valid(message):
            return False
        reply = message.get('reply_to_message') or {}
        if not reply.get('message_id'):
            return False
        self._check_bot_identity()
        session = self._db.one("SELECT * FROM dice_sessions WHERE bot_id=? AND tg_id=? AND mid=? AND state='active' AND expires>?", (self._active_bot_id, str(message['from']['id']), reply['message_id'], time.time()))
        if not session:
            return False
        prior = self._db.one('SELECT 1 FROM dice_inputs WHERE bot_id=? AND chat_id=? AND mid=?', (self._active_bot_id, session['tg_id'], message['message_id']))
        if prior:
            self._dice_cleanup(message)
            return True
        p = json.loads(session['payload'])
        if p.get('view') != 'amount' or not p.get('selected'):
            return False
        try:
            service = self._dice_service()
            tg, member = service.actor(message['from'])
            if tg != session['tg_id'] or member['emby_user_id'] != session['user_id']:
                raise PlayError('绑定已变化，本次未下注')
            raw = str(message.get('text') or '')
            if not re.fullmatch(r'[-+]?[0-9]+(?:\.[0-9]+)?', raw):
                # Non-numeric chat is not treated as a trusted game amount and is never deleted.
                return False
            sender = reply.get('from') or {}
            if sender.get('is_bot') is not True or str(sender.get('id')) != self._active_bot_id or f'输入凭据 {session["token"][:6]}-{session["revision"]}' not in str(reply.get('text') or ''):
                raise PlayError('金额对应的选项已变化，请回复当前金额卡；本次未下注')
            if not re.fullmatch('[0-9]{1,19}', raw):
                raise PlayError('金额须为安全范围内的正整数；本次未下注，已确认投注不变')
            await self._dice_add_amount(session, int(raw), input_mid=message['message_id'])
            self._dice_cleanup(message)
        except PlayError as exc:
            p['notice'] = str(exc)
            with economy_write(self._db) as conn:
                service.update_session(conn, session, p)
            await self._dice_draw(service.session(session['token']))
            self._dice_cleanup(message)
        return True

    async def _dice_add_amount(self, session, amount, *, input_mid=None):
        service = self._dice_service()
        with economy_write(self._db) as conn:
            if input_mid and conn.execute('SELECT 1 FROM dice_inputs WHERE bot_id=? AND chat_id=? AND mid=?', (self._active_bot_id, session['tg_id'], input_mid)).fetchone():
                return
            current = service.session(session['token'])
            if current['revision'] != session['revision']:
                raise PlayError('草稿已更新，请使用当前卡')
            p = json.loads(current['payload'])
            row = service.get(current['nonce'])
            if row['state'] != 'open' or time.time() >= row['deadline'] or not service.enabled():
                raise PlayError('本次未下注：已封盘或停用，旧注不变')
            if not p.get('selected'):
                raise PlayError('请先选择投注项')
            pool, option = p['selected']
            p['draft'] = validate(p['draft']+[{'pool': pool, 'option': option, 'amount': amount}])
            p.update(view='draft', selected=None, notice='')
            service.update_session(conn, current, p)
            if input_mid:
                conn.execute('INSERT INTO dice_inputs VALUES(?,?,?,?,?,?)', (self._active_bot_id, current['tg_id'], input_mid, current['token'], current['revision'], amount))
        await self._dice_draw(service.session(session['token']))

    async def _dice_group_callback(self, data, message, actor, callback_id):
        try:
            service = self._dice_service()
            nonce = data.split(':', 1)[1]
            with economy_write(self._db) as conn:
                row = service.get(nonce)
                service.actor(actor)
                chat, thread = service.group(message)
                sender = message.get('from') or {}
                actions = [b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard', []) for b in line]
                if (chat != row['chat_id'] or thread != row['thread_id'] or sender.get('is_bot') is not True
                        or str(sender.get('id')) != self._active_bot_id or data not in actions
                        or type(message.get('message_id')) is not int or message['message_id'] <= 0):
                    raise PlayError('请使用原群话题中的Bot开局卡')
                mid = message['message_id']
                if row['card_mid'] not in (None, mid):
                    raise PlayError('请使用原开局卡')
                if row['card_mid'] is None:
                    job = conn.execute("SELECT * FROM dice_messages WHERE nonce=? AND purpose='open' AND state IN ('sending','unknown')", (nonce,)).fetchone()
                    if not job:
                        raise PlayError('开局回执无效')
                    conn.execute("UPDATE dice_messages SET state='sent',mid=?,lease=0 WHERE id=?", (mid, job['id']))
                    conn.execute("UPDATE dice_rounds SET card_mid=?,cleanup_queued=0,state=CASE WHEN state='opening' AND deadline>? THEN 'open' ELSE state END WHERE nonce=?", (mid, time.time(), nonce))
                    conn.execute("INSERT INTO dice_delivery_audit(job_id,action,mid,created) VALUES(?,'callback',?,?)", (job['id'],mid,time.time()))
            await self._answer_callback(callback_id, f'固定drand轮次 {row["round_number"]}；请点进入私聊查看承诺及开奖证据。')
        except (PlayError, GroupPointsError) as exc:
            await self._answer_callback(callback_id, str(exc))

    async def _dice_callback(self, data, message, actor, callback_id):
        try:
            service = self._dice_service()
            parts = data.split(':')
            if len(parts) != 5 or parts[0] != 'dc' or not parts[2].isdigit():
                raise PlayError('骰局按钮无效')
            session = service.session(parts[1])
            service.context(session, actor, message, data)
            if int(parts[2]) != session['revision']:
                raise PlayError('草稿已更新，请使用当前卡；已确认投注不变')
            op, arg = parts[3:]
            if op == 'confirm':
                service.confirm(session, actor)
                await self._dice_draw(service.session(session['token']))
            elif op == 'amount':
                if arg not in ('10', '50', '100'):
                    raise PlayError('金额按钮无效')
                await self._dice_add_amount(session, int(arg))
            else:
                with economy_write(self._db) as conn:
                    session = service.session(parts[1])
                    if int(parts[2]) != session['revision']:
                        raise PlayError('草稿已更新')
                    p = json.loads(session['payload'])
                    p['notice'] = ''
                    if op in ('new', 'clear'):
                        p.update(view='home', draft=[], ticket=secrets.token_hex(16), selected=None)
                    elif op == 'pool':
                        if arg not in POOLS:
                            raise PlayError('分池无效')
                        if conn.execute('SELECT 1 FROM dice_tickets WHERE token=?', (p['ticket'],)).fetchone():
                            p.update(draft=[], ticket=secrets.token_hex(16))
                        p.update(view='options', pool=arg, selected=None)
                    elif op == 'pick':
                        if p.get('view') != 'options' or not arg.isdigit() or not 0 <= int(arg) < len(POOLS[p['pool']][1]):
                            raise PlayError('选项已过期')
                        p.update(view='amount', selected=[p['pool'], normalize(p['pool'], POOLS[p['pool']][1][int(arg)])])
                    elif op == 'mine':
                        if arg not in POOLS:
                            raise PlayError('分池无效')
                        p.update(view='mine', pool=arg, selected=None)
                    elif op in ('home', 'rules', 'proof', 'draft'):
                        p.update(view=op, selected=None)
                    else:
                        raise PlayError('操作无效')
                    service.update_session(conn, session, p)
                await self._dice_draw(service.session(session['token']))
            await self._answer_callback(callback_id)
        except (PlayError, GroupPointsError) as exc:
            await self._answer_callback(callback_id, str(exc))

    async def _dice_edit(self, chat, mid, body, keys):
        # Never enter the generic personal/admin panel lifecycle or replace a missing card.
        from app.modules.telegram import _CALL_ERROR
        token = _CALL_ERROR.set(None)
        try:
            result = await self._call('editMessageText', {'chat_id': chat, 'message_id': mid,
                'text': body, 'parse_mode': 'HTML', 'disable_web_page_preview': True,
                'reply_markup': {'inline_keyboard': keys}})
            return result is not None and result is not False or 'not modified' in str(_CALL_ERROR.get() or '').lower()
        except Exception:  # noqa: BLE001 - display failure must never block financial recovery
            return False
        finally:
            _CALL_ERROR.reset(token)

    async def _dice_draw(self, session):
        service = self._dice_service()
        row = service.get(session['nonce'])
        bets = self._db.query('SELECT * FROM dice_bets WHERE nonce=? AND user_id=?', (row['nonce'], session['user_id']))
        body, keys = private_card(session, row, self._points.balance(session['user_id']), bets, json.loads(row['result']))
        if session['mid']:
            if await self._dice_edit(int(session['tg_id']), session['mid'], body, keys):
                self._db.execute('UPDATE dice_sessions SET rendered_revision=? WHERE token=? AND mid=?', (session['revision'], session['token'], session['mid']))
            return
        with economy_write(self._db) as conn:
            service.queue_message(conn, row['nonce'], 'session:'+session['token'], session['tg_id'], body, keys)
        await self._dice_deliver(row['nonce'])

    async def _dice_publish_open(self, row):
        if not self._bot_username:
            raise PlayError('Bot用户名尚未确认，未开放下注')
        if is_group(row):
            with economy_write(self._db):
                row, snapshot = self._dice_service().group_snapshot(row['nonce'])
                body, keys = dice_group_view.group_card(row, snapshot)
        else:
            body, keys = group_card(row, 0, self._bot_username)
        with economy_write(self._db) as conn:
            self._dice_service().queue_message(conn, row['nonce'], 'open', row['chat_id'], body, keys, row['thread_id'])
        await self._dice_deliver(row['nonce'])

    async def _dice_deliver(self, nonce):
        service = self._dice_service()
        row = service.get(nonce)
        if not self.enabled or not self._group_chat_allowed({'id': int(row['chat_id']), 'type': 'supergroup'}):
            return
        for _ in range(20):
            clock = time.time()
            with economy_write(self._db) as conn:
                conn.execute("UPDATE dice_messages SET state='unknown',error='发送回执丢失，需核实' WHERE bot_id=? AND nonce=? AND state='sending' AND lease<=?", (self._active_bot_id, nonce, clock))
                job = conn.execute("SELECT * FROM dice_messages WHERE bot_id=? AND nonce=? AND state='pending' AND due<=? ORDER BY id LIMIT 1", (self._active_bot_id, nonce, clock)).fetchone()
                if not job:
                    break
                job = dict(job)
                if job['purpose'] == 'open' and row['state'] not in ('opening', 'open'):
                    conn.execute("UPDATE dice_messages SET state='obsolete' WHERE id=?", (job['id'],))
                    continue
                if job['purpose'].startswith('detail:'):
                    member = self._members.find_by_telegram(job['recipient'])
                    if not member or member['emby_user_id'] != job['purpose'][7:]:
                        conn.execute("UPDATE dice_messages SET state='failed',error='本人绑定已变化，未发送明细' WHERE id=?", (job['id'],))
                        continue
                lease = clock+90
                conn.execute("UPDATE dice_messages SET state='sending',lease=?,attempts=attempts+1 WHERE id=?", (lease, job['id']))
            token = CALL_DELIVERY.set(None)
            try:
                try:
                    mid = await self._dice_send(int(job['recipient']), job['body'], json.loads(job['keyboard']), thread_id=job['thread'] or None)
                    problem = CALL_DELIVERY.get() or failure()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - preserve exact outbox intent across transport errors
                    mid, problem = None, failure(exc=exc)
                good = type(mid) is int and mid > 0
                bound = False
                with economy_write(self._db) as conn:
                    if good:
                        changed = conn.execute("UPDATE dice_messages SET state='sent',mid=?,lease=0 WHERE id=? AND state='sending' AND lease=?", (mid, job['id'], lease))
                        current = conn.execute('SELECT mid FROM dice_messages WHERE id=?', (job['id'],)).fetchone()
                        bound = changed.rowcount == 1 or current['mid'] == mid
                        if bound and job['purpose'] == 'open':
                            conn.execute("UPDATE dice_rounds SET card_mid=?,state=CASE WHEN state='opening' AND deadline>? THEN 'open' ELSE state END WHERE nonce=?", (mid, time.time(), nonce))
                        elif bound and job['purpose'].startswith('session:'):
                            conn.execute('UPDATE dice_sessions SET mid=?,rendered_revision=MAX(rendered_revision,0) WHERE token=?', (mid, job['purpose'][8:]))
                    else:
                        state = 'pending' if problem.get('state') == 'retry' else problem.get('state', 'unknown')
                        conn.execute('UPDATE dice_messages SET state=?,error=?,due=?,lease=0 WHERE id=? AND lease=?', (state, problem.get('reason', '发送未确认'), clock+max(10, int(problem.get('retry_after') or 0)), job['id'], lease))
                if good and not bound and not job['purpose'].startswith('report:'):
                    recipient = int(job['recipient'])
                    DiceCleanup(self._db).enqueue(self._active_bot_id, recipient, job['thread'], mid, 'feedback', self._active_bot_id if recipient < 0 else recipient)
                elif good and job['purpose'].startswith('session:'):
                    session = service.session(job['purpose'][8:])
                    DiceCleanup(self._db).enqueue(self._active_bot_id, int(session['tg_id']), 0, mid, 'feedback', session['tg_id'], due=session['expires'])
                elif good and job['purpose'].startswith('detail:'):
                    DiceCleanup(self._db).enqueue(self._active_bot_id, int(job['recipient']), 0, mid, 'feedback', job['recipient'], due=time.time()+900)
            finally:
                CALL_DELIVERY.reset(token)
        row = service.get(nonce)
        if row['state'] in ('settled', 'refunded', 'empty') and row['card_mid']:
            pending = self._db.one("SELECT COUNT(*) n FROM dice_messages WHERE nonce=? AND purpose LIKE 'report:%' AND state!='sent'", (nonce,))['n']
            count = self._db.one("SELECT COUNT(*) n FROM dice_messages WHERE nonce=? AND purpose LIKE 'report:%'", (nonce,))['n']
            if count and not pending:
                DiceCleanup(self._db).enqueue(self._active_bot_id, int(row['chat_id']), row['thread_id'], row['card_mid'], 'feedback', self._active_bot_id, due=time.time())
                self._db.execute('UPDATE dice_rounds SET cleanup_queued=1 WHERE nonce=?', (nonce,))
        elif row['state'] in ('settled', 'refunded', 'empty'):
            self._db.execute('UPDATE dice_rounds SET cleanup_queued=1 WHERE nonce=?', (nonce,))

    async def _dice_tick(self):
        if self._db is None or self._points is None or not self.enabled:
            return
        service = self._dice_service()
        rows = self._db.query("SELECT * FROM dice_rounds WHERE bot_id=? AND (state IN ('opening','open','locked','fixed') OR EXISTS(SELECT 1 FROM dice_messages m WHERE m.nonce=dice_rounds.nonce AND m.state IN ('pending','sending')) OR (state IN ('settled','refunded','empty') AND cleanup_queued=0 AND NOT EXISTS(SELECT 1 FROM dice_messages m WHERE m.nonce=dice_rounds.nonce AND m.purpose LIKE 'report:%' AND m.state!='sent'))) ORDER BY created LIMIT 50", (self._active_bot_id,))
        for initial in rows:
            nonce = initial['nonce']
            try:
                if initial['state'] == 'opening' and time.time() < initial['deadline']:
                    await self._dice_publish_open(initial)
                row = service.close(nonce)
                if is_group(row) and row['state'] == 'locked':
                    await self._dice_group_refresh(nonce)
                if row['state'] == 'locked':
                    row = service.settle(nonce)
                    if row['state'] == 'locked' and row['card_mid'] and not is_group(row):
                        n = self._db.one('SELECT COUNT(DISTINCT user_id) n FROM dice_tickets WHERE nonce=?', (nonce,))['n']
                        body, keys = group_card(row, n, self._bot_username)
                        defer_ui(self, ('dice', nonce), lambda chat=int(row['chat_id']), mid=row['card_mid'], body=body, keys=keys: self._dice_edit(chat, mid, body, keys))
                    if row['state'] == 'locked' and time.time() >= beacon.round_time(row['round_number']):
                        try:
                            raw = await asyncio.wait_for(beacon.fetch(row['round_number']), timeout=max(0.01, row['timeout']-time.time()))
                            await asyncio.to_thread(service.fix, nonce, raw)
                        except (ValueError, PlayError, TimeoutError):
                            pass
                row = service.settle(nonce)
                if is_group(row):
                    if row['state'] in ('settled', 'refunded', 'empty'):
                        self._dice_group_reports(row)
                    await self._dice_deliver(nonce)
                    await self._dice_group_refresh(nonce)
                    continue
                if row['state'] in ('settled', 'refunded', 'empty'):
                    tickets = self._db.query('SELECT * FROM dice_tickets WHERE nonce=? ORDER BY created,token', (nonce,))
                    with economy_write(self._db) as conn:
                        for i, body in enumerate(report_pages(row, tickets, self._bot_username)):
                            service.queue_message(conn, nonce, f'report:{i}', row['chat_id'], body, thread=row['thread_id'])
                        people = {t['user_id']: t for t in tickets}
                        results = json.loads(row['result'])
                        for uid, ticket in people.items():
                            detail = '🎲 <b>骰局 · 本人结算</b>\n<code>'+nonce+'</code>'
                            for pool, (label, _) in POOLS.items():
                                outcome = results[pool]['users'].get(uid)
                                if outcome:
                                    detail += f'\n\n{label} · 投入 {outcome["invested"]}\n命中本金 {outcome["winning"]} · 奖金 {outcome["bonus"]} · 退款 {outcome["refund"]}\n返还 {outcome["returned"]} · 净盈亏 {outcome["net"]:+d}'
                            detail += f'\n\n<a href="https://t.me/{self._bot_username}?start=dice_{nonce}">投注明细 · 开奖验真</a>'
                            service.queue_message(conn, nonce, 'detail:'+uid, ticket['tg_id'], detail)
                elif row['card_mid']:
                    n = self._db.one('SELECT COUNT(DISTINCT user_id) n FROM dice_tickets WHERE nonce=?', (nonce,))['n']
                    body, keys = group_card(row, n, self._bot_username)
                    defer_ui(self, ('dice', nonce), lambda chat=int(row['chat_id']), mid=row['card_mid'], body=body, keys=keys: self._dice_edit(chat, mid, body, keys))
                await self._dice_deliver(nonce)
            except Exception:  # noqa: BLE001 - retain funds; do not block other fixed-result recovery
                self._db.execute("UPDATE dice_rounds SET error='恢复异常，保留资金及固定结果，待核对' WHERE nonce=?", (nonce,))
        for dirty in self._db.query("SELECT nonce FROM dice_rounds WHERE bot_id=? AND json_extract(rules,'$.version')=? AND group_rendered<group_revision AND group_due<=? AND group_lease<=? AND group_retry<=? LIMIT 50", (self._active_bot_id, GROUP_VERSION, time.time(), time.time(), time.time())):
            await self._dice_group_refresh(dirty['nonce'])
        for session in self._db.query("SELECT * FROM dice_sessions WHERE bot_id=? AND state='active' AND mid IS NOT NULL AND expires>? AND rendered_revision<revision LIMIT 50", (self._active_bot_id, time.time())):
            member = self._members.find_by_telegram(session['tg_id'])
            if member and member['emby_user_id'] == session['user_id']:
                await self._dice_draw(session)
        # Recover a crash after exact send ACK persistence but before deletion enqueue.
        for s in self._db.query("SELECT s.* FROM dice_sessions s WHERE bot_id=? AND mid IS NOT NULL AND NOT EXISTS(SELECT 1 FROM play_jobs j WHERE j.bot_id=s.bot_id AND j.job_key='dice:'||s.tg_id||':'||s.mid)", (self._active_bot_id,)):
            DiceCleanup(self._db).enqueue(self._active_bot_id, int(s['tg_id']), 0, s['mid'], 'feedback', s['tg_id'], due=s['expires'])
        for m in self._db.query("SELECT * FROM dice_messages m WHERE bot_id=? AND purpose LIKE 'detail:%' AND state='sent' AND NOT EXISTS(SELECT 1 FROM play_jobs j WHERE j.bot_id=m.bot_id AND j.job_key='dice:'||m.recipient||':'||m.mid)", (self._active_bot_id,)):
            DiceCleanup(self._db).enqueue(self._active_bot_id, int(m['recipient']), 0, m['mid'], 'feedback', m['recipient'], due=time.time()+900)
        await self._drain_checkin_deletes(service=DiceCleanup(self._db))
