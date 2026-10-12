"""Versioned group-only size pool; old seven-pool records remain untouched."""
from __future__ import annotations

import hashlib
import json
import time

from app.modules.account_deletion import deletion_in_progress
from app.modules.dice_banker import BANKER_VERSION, banker, is_banker
from app.modules.dice_rules import MAX_INT
from app.modules.economy_rules import economy_write, encode
from app.modules.play_money import PlayError, integer
from app.modules.red_packets import public_name

GROUP_VERSION = 'dice-size-group-v1'
PAGE_SIZE = 8
LATEST_SIZE = 3
TERMINAL = ('settled', 'refunded', 'empty')


def is_group(row):
    return json.loads(row['rules']).get('version') in (GROUP_VERSION, BANKER_VERSION)


def migrate(db):
    for name, declaration in (
        ('group_page', 'INTEGER NOT NULL DEFAULT -1'),
        ('group_view', "TEXT NOT NULL DEFAULT 'ledger'"),
        ('group_revision', 'INTEGER NOT NULL DEFAULT 0'),
        ('group_rendered', 'INTEGER NOT NULL DEFAULT -1'),
        ('group_due', 'REAL NOT NULL DEFAULT 0'),
        ('group_retry', 'REAL NOT NULL DEFAULT 0'),
        ('group_lease', 'REAL NOT NULL DEFAULT 0'),
        ('group_token', "TEXT NOT NULL DEFAULT ''"),
        ('group_render_mid', 'INTEGER')):
        db._ensure_column('dice_rounds', name, declaration)
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS dice_group_receipts (
      bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
      nonce TEXT NOT NULL, user_id TEXT NOT NULL, tg_id TEXT NOT NULL,
      option TEXT NOT NULL, amount INTEGER NOT NULL, ticket TEXT NOT NULL,
      PRIMARY KEY(bot_id,chat_id,message_id));
    CREATE INDEX IF NOT EXISTS dice_tickets_round ON dice_tickets(nonce);
    CREATE INDEX IF NOT EXISTS dice_bets_round ON dice_bets(nonce,pool);
    """)
    db._ensure_column('dice_group_receipts', 'input_mode', "TEXT NOT NULL DEFAULT 'amount'")


class GroupDiceService:
    def group_bet(self, message, option, amount, *, now=None):
        chat, thread = self.group(message)
        mid = integer(message.get('message_id'), '消息ID')
        mode = 'allin' if isinstance(amount, str) and amount.lower() == 'allin' else 'amount'
        if mode == 'amount':
            integer(amount, '下注积分')
        if option not in ('大', '小'):
            raise PlayError('请使用 /大 金额 或 /小 金额')
        reply = message.get('reply_to_message') or {}
        if reply:
            reply_chat, reply_thread = self.group(reply)
            sender = reply.get('from') or {}
            if (reply_chat != chat or reply_thread != thread or sender.get('is_bot') is not True
                    or str(sender.get('id')) != self.bot_id
                    or type(reply.get('message_id')) is not int or reply['message_id'] <= 0):
                raise PlayError('请回复原群、原话题中的本Bot游戏卡')
        with economy_write(self.db) as conn:
            clock = time.time() if now is None else now
            tg, member = self.actor(message.get('from') or {}, now=clock)
            uid = member['emby_user_id']
            # Replay resolution precedes active-round selection, including after restart.
            prior = conn.execute('SELECT * FROM dice_group_receipts WHERE bot_id=? AND chat_id=? AND message_id=?', (self.bot_id, chat, mid)).fetchone()
            if prior:
                if ((prior['user_id'], prior['tg_id'], prior['option'], prior['input_mode']) != (uid, tg, option, mode)
                        or (mode == 'amount' and prior['amount'] != amount)):
                    raise PlayError('原消息已处理，不能修改旧注')
                original = self.get(prior['nonce'])
                if original['thread_id'] != thread:
                    raise PlayError('原消息已处理，不能修改旧注')
                return original
            if reply:
                raw = conn.execute("""SELECT * FROM dice_rounds WHERE bot_id=? AND chat_id=? AND thread_id=?
                    AND (card_mid=? OR nonce IN (SELECT nonce FROM dice_messages
                    WHERE purpose='art:open' AND state='sent' AND mid=?))""",
                    (self.bot_id, chat, thread, reply['message_id'], reply['message_id'])).fetchone()
            else:
                raw = conn.execute("SELECT * FROM dice_rounds WHERE bot_id=? AND chat_id=? AND thread_id=? AND state IN ('opening','open','locked','fixed')",
                                   (self.bot_id, chat, thread)).fetchone()
            if not raw or not is_group(raw):
                raise PlayError('当前没有可下注的群内大小局')
            row = dict(raw)
            if not reply and mid <= row['command_mid']:
                raise PlayError('这条指令早于本局，本次未下注')
            if deletion_in_progress(conn, uid):
                raise PlayError('账号正在注销或结果待核查，本次未下注')
            if not self.enabled() or row['state'] != 'open' or clock >= row['deadline']:
                raise PlayError('已封盘或暂停，本次未下注')
            if conn.execute('SELECT 1 FROM dice_tickets WHERE nonce=? AND (user_id=? OR tg_id=?) AND NOT(user_id=? AND tg_id=?)', (row['nonce'], uid, tg, uid, tg)).fetchone():
                raise PlayError('本局账号绑定已变化，本次未下注')
            accepted = conn.execute("SELECT accepted FROM dice_pools WHERE nonce=? AND pool='size'", (row['nonce'],)).fetchone()[0]
            cap = json.loads(row['rules']).get('pool_cap') if is_banker(row) else None
            if mode == 'allin' and not is_banker(row):
                raise PlayError('旧无庄局不支持 allin，请使用数字金额')
            if is_banker(row):
                bank = banker(row)
                if uid == bank['user_id'] or tg == bank['tg_id']:
                    raise PlayError('庄家不能下注自己的局')
                sides = {r['option']: r['amount'] for r in conn.execute('SELECT option,SUM(amount) amount FROM dice_bets WHERE nonce=? GROUP BY option', (row['nonce'],))}
                capacity = bank['capital']+sides.get('小' if option == '大' else '大', 0)-sides.get(option, 0)
                room = MAX_INT-bank['capital']-accepted
                pool_room = cap-accepted if cap is not None else room
                if mode == 'amount' and amount > pool_room and cap is not None:
                    available = max(0, min(self.points.balance(uid), capacity, pool_room, room))
                    raise PlayError(f'超过总投注上限，本方向还可押 {available:,} 分，整笔未下注')
                if mode == 'allin':
                    balance = self.points.balance(uid)
                    if balance <= 0:
                        raise PlayError('可用积分为0，本次未下注')
                    if capacity <= 0:
                        raise PlayError('本方向已满盘，本次未下注')
                    if room <= 0:
                        raise PlayError('本局安全整数额度已满，本次未下注')
                    if pool_room <= 0:
                        raise PlayError('总投注已满额，本次未下注')
                    amount = min(balance, capacity, room, pool_room)
                integer(bank['capital']+accepted+amount, '本局托管安全整数总额')
                sides[option] = sides.get(option, 0)+amount
                if abs(sides.get('大', 0)-sides.get('小', 0)) > bank['capital']:
                    raise PlayError('超过本局庄本承赔范围，整笔未下注')
            integer(accepted+amount, '本局安全整数总额')
            if self.points.balance(uid) < amount:
                raise PlayError('积分不足，本次未下注')
            ticket = hashlib.sha256(encode(['dice-group', self.bot_id, chat, mid]).encode()).hexdigest()
            items = [{'pool': 'size', 'option': option, 'amount': amount}]
            self.cash.reserve(conn, 'dice', ticket+':size:'+option, uid, amount, now=clock)
            conn.execute('INSERT INTO dice_tickets VALUES(?,?,?,?,?,?,?,?)', (ticket, row['nonce'], uid, tg, public_name(message['from']), encode(items), amount, clock))
            conn.execute('INSERT INTO dice_bets VALUES(?,?,?,?,?,?)', (ticket, row['nonce'], uid, 'size', option, amount))
            conn.execute("UPDATE dice_pools SET held=held+?,accepted=accepted+? WHERE nonce=? AND pool='size'", (amount, amount, row['nonce']))
            conn.execute('INSERT INTO dice_group_receipts(bot_id,chat_id,message_id,nonce,user_id,tg_id,option,amount,ticket,input_mode) VALUES(?,?,?,?,?,?,?,?,?,?)', (self.bot_id, chat, mid, row['nonce'], uid, tg, option, amount, ticket, mode))
            conn.execute('UPDATE dice_rounds SET group_revision=group_revision+1,group_due=CASE WHEN group_due=0 THEN ? ELSE group_due END WHERE nonce=?', (clock+2, row['nonce']))
            if cap is not None and accepted+amount == cap:
                conn.execute("UPDATE dice_rounds SET state='locked',group_due=0 WHERE nonce=?", (row['nonce'],))
            # Same transaction as the accepted ticket: a restart cannot lose its deletion intent.
            payload = {'chat_id': int(chat), 'thread_id': thread, 'message_id': mid, 'source': 'command', 'actor_tg_id': tg}
            conn.execute("INSERT OR IGNORE INTO play_jobs(bot_id,job_key,kind,payload_json,created_at,due_at,expires_at) VALUES(?,?,'dice.delete',?,?,?,?)", (self.bot_id, f'dice:{chat}:{mid}', encode(payload), clock, clock+2, clock+48*3600))
            return self.get(row['nonce'])

    def group_snapshot(self, nonce):
        """All UI reads are made under the caller's write lock for a consistent revision."""
        row = self.get(nonce)
        totals = self.db.query("SELECT b.option,SUM(b.amount) amount,COUNT(DISTINCT b.user_id) people FROM dice_bets b WHERE b.nonce=? AND b.pool='size' GROUP BY b.option", (nonce,))
        from app.modules.dice_caption_view import is_caption
        if is_caption(row):
            return row, {'totals': totals, 'tickets': self.db.query('SELECT * FROM dice_tickets WHERE nonce=? ORDER BY rowid DESC', (nonce,))}
        count = self.db.one('SELECT COUNT(*) n FROM dice_tickets WHERE nonce=?', (nonce,))['n']
        pages = max(1, (count+PAGE_SIZE-1)//PAGE_SIZE)
        page = 0 if row['group_page'] < 0 else min(row['group_page'], pages-1)
        latest = self.db.query('SELECT rowid seq,* FROM dice_tickets WHERE nonce=? ORDER BY rowid DESC LIMIT ?', (nonce, LATEST_SIZE))
        ledger = self.db.query('SELECT rowid seq,* FROM dice_tickets WHERE nonce=? ORDER BY rowid DESC LIMIT ? OFFSET ?', (nonce, PAGE_SIZE, page*PAGE_SIZE))
        return row, {'totals': totals, 'count': count, 'page': page, 'pages': pages, 'latest': latest, 'ledger': ledger}

    def group_context(self, conn, row, message, actor, data):
        self.actor(actor)
        chat, thread = self.group(message)
        sender = message.get('from') or {}
        actions = [b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard', []) for b in line]
        mid = message.get('message_id')
        if (not is_group(row) or chat != row['chat_id'] or thread != row['thread_id']
                or sender.get('is_bot') is not True or str(sender.get('id')) != self.bot_id
                or type(mid) is not int or mid <= 0 or data not in actions):
            raise PlayError('请使用本局原群话题中的Bot卡')
        op = data.rsplit(':', 1)[-1]
        if op in ('artopen', 'artresult'):
            purpose = 'art:open' if op == 'artopen' else 'art:result'
            job = conn.execute('SELECT * FROM dice_messages WHERE nonce=? AND purpose=?', (row['nonce'], purpose)).fetchone()
            if (not job or job['state'] not in ('sent', 'unknown', 'sending')
                    or (job['mid'] is not None and job['mid'] != mid)
                    or data not in [b.get('callback_data') for line in json.loads(job['keyboard']) for b in line]):
                raise PlayError('请使用本局原图片')
            if job['state'] != 'sent':
                conn.execute("UPDATE dice_messages SET state='sent',mid=?,lease=0 WHERE id=?", (mid, job['id']))
                conn.execute('UPDATE dice_rounds SET cleanup_queued=0 WHERE nonce=?', (row['nonce'],))
                conn.execute("INSERT INTO dice_delivery_audit(job_id,action,mid,created) VALUES(?,'callback',?,?)", (job['id'], mid, time.time()))
            return
        report = conn.execute("SELECT * FROM dice_messages WHERE nonce=? AND purpose='report:0'", (row['nonce'],)).fetchone()
        if report and report['state'] == 'sent':
            if mid != report['mid']:
                raise PlayError('请使用本局最终战报')
            return
        if row['card_mid'] == mid:
            return
        job = conn.execute("SELECT * FROM dice_messages WHERE nonce=? AND purpose IN ('open','report:0') AND state IN ('unknown','sending') ORDER BY id DESC LIMIT 1", (row['nonce'],)).fetchone()
        if not job or data not in [b.get('callback_data') for line in json.loads(job['keyboard']) for b in line]:
            raise PlayError('原卡回执尚未确认')
        if job['purpose'] == 'open' and row['card_mid'] is not None:
            raise PlayError('请使用原开局卡')
        conn.execute("UPDATE dice_messages SET state='sent',mid=?,lease=0 WHERE id=?", (mid, job['id']))
        if job['purpose'] == 'open':
            conn.execute("UPDATE dice_rounds SET card_mid=?,state=CASE WHEN state='opening' AND deadline>? THEN 'open' ELSE state END WHERE nonce=?", (mid, time.time(), row['nonce']))
        conn.execute('UPDATE dice_rounds SET cleanup_queued=0,group_due=0,group_revision=group_revision+1 WHERE nonce=?', (row['nonce'],))
        conn.execute("INSERT INTO dice_delivery_audit(job_id,action,mid,created) VALUES(?,'callback',?,?)", (job['id'], mid, time.time()))
