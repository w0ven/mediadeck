"""Durable dice rounds. All financial changes use the shared SQLite write lock."""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from app.modules import dice_beacon as beacon
from app.modules.account_deletion import deletion_in_progress
from app.modules.dice_rules import MAX_INT, POOLS, settle_pool, validate
from app.modules.economy_rules import economy_write, encode
from app.modules.play_money import CashBook, PlayError, integer
from app.modules.play_rounds import PlayAccess
from app.modules.plugins import Field, Plugin, Spec
from app.modules.red_packets import public_name


def migrate(db):
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS dice_rounds (
      nonce TEXT PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL,
      thread_id INTEGER NOT NULL, command_mid INTEGER NOT NULL, actor_uid TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'opening', created REAL NOT NULL, deadline REAL NOT NULL,
      timeout REAL NOT NULL, round_number INTEGER NOT NULL, rules TEXT NOT NULL,
      secret TEXT NOT NULL, commitment TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '{}',
      seed TEXT NOT NULL DEFAULT '', dice TEXT NOT NULL DEFAULT '[]',
      card_mid INTEGER, cleanup_queued INTEGER NOT NULL DEFAULT 0,
      result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
      UNIQUE(bot_id,chat_id,command_mid));
    CREATE UNIQUE INDEX IF NOT EXISTS dice_active ON dice_rounds(bot_id,chat_id,thread_id)
      WHERE state IN ('opening','open','locked','fixed');
    CREATE TABLE IF NOT EXISTS dice_pools (
      nonce TEXT NOT NULL, pool TEXT NOT NULL, held INTEGER NOT NULL DEFAULT 0 CHECK(held>=0),
      accepted INTEGER NOT NULL DEFAULT 0 CHECK(accepted>=0), result TEXT NOT NULL DEFAULT '{}',
      PRIMARY KEY(nonce,pool));
    CREATE TABLE IF NOT EXISTS dice_tickets (
      token TEXT PRIMARY KEY, nonce TEXT NOT NULL, user_id TEXT NOT NULL, tg_id TEXT NOT NULL,
      display_name TEXT NOT NULL, items TEXT NOT NULL, amount INTEGER NOT NULL, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS dice_bets (
      ticket TEXT NOT NULL, nonce TEXT NOT NULL, user_id TEXT NOT NULL, pool TEXT NOT NULL,
      option TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
      PRIMARY KEY(ticket,pool,option));
    CREATE TABLE IF NOT EXISTS dice_sessions (
      token TEXT PRIMARY KEY, nonce TEXT NOT NULL, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
      tg_id TEXT NOT NULL, mid INTEGER, revision INTEGER NOT NULL DEFAULT 0,
      rendered_revision INTEGER NOT NULL DEFAULT -1, payload TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'active');
    CREATE TABLE IF NOT EXISTS dice_inputs (
      bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, mid INTEGER NOT NULL,
      session TEXT NOT NULL, revision INTEGER NOT NULL, amount INTEGER NOT NULL,
      PRIMARY KEY(bot_id,chat_id,mid));
    CREATE TABLE IF NOT EXISTS dice_delivery_audit (
      id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, action TEXT NOT NULL,
      mid INTEGER, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS dice_messages (
      id INTEGER PRIMARY KEY, nonce TEXT NOT NULL, bot_id TEXT NOT NULL, purpose TEXT NOT NULL,
      recipient TEXT NOT NULL, thread INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL,
      keyboard TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'pending',
      mid INTEGER, lease REAL NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
      attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
      UNIQUE(nonce,purpose,recipient));
    """)


class DiceService(PlayAccess):
    def __init__(self, db, members, points, enabled, allowed, bot_id):
        super().__init__(db, members, allowed, bot_id)
        self.points, self.cash, self.enabled = points, CashBook(points), enabled

    def get(self, nonce):
        row = self.db.one('SELECT * FROM dice_rounds WHERE nonce=? AND bot_id=?', (nonce, self.bot_id))
        if not row:
            raise PlayError('骰局不存在或Bot归属已变化')
        return row

    def create(self, message, *, now=None):
        chat, thread = self.group(message)
        mid = integer(message.get('message_id'), '消息ID')
        with economy_write(self.db) as conn:
            clock = time.time() if now is None else now
            _, member = self.actor(message.get('from') or {}, now=clock)
            if not self.enabled():
                raise PlayError('骰局暂未开放')
            if conn.execute("SELECT 1 FROM dice_rounds WHERE bot_id=? AND error<>'' AND state IN ('open','locked','fixed')", (self.bot_id,)).fetchone():
                raise PlayError('骰局正在核对异常，暂不接受新局')
            previous = conn.execute('SELECT * FROM dice_rounds WHERE bot_id=? AND chat_id=? AND command_mid=?', (self.bot_id, chat, mid)).fetchone()
            if previous:
                if previous['actor_uid'] != member['emby_user_id'] or previous['thread_id'] != thread:
                    raise PlayError('原命令归属不符')
                return dict(previous)
            active = conn.execute("SELECT * FROM dice_rounds WHERE bot_id=? AND chat_id=? AND thread_id=? AND state IN ('opening','open','locked','fixed')", (self.bot_id, chat, thread)).fetchone()
            if active:
                return dict(active)
            if conn.execute('SELECT 1 FROM dice_rounds WHERE bot_id=? AND actor_uid=? AND created>?', (self.bot_id, member['emby_user_id'], clock-15)).fetchone():
                raise PlayError('请稍后再开局')
            nonce, secret = secrets.token_hex(12), secrets.token_hex(32)
            deadline = clock + 90
            row = {'nonce': nonce, 'bot_id': self.bot_id, 'chat_id': chat, 'thread_id': thread,
                   'command_mid': mid, 'actor_uid': member['emby_user_id'], 'created': clock,
                   'deadline': deadline, 'timeout': deadline+600, 'round_number': beacon.future_round(deadline),
                   'rules': encode({'version': beacon.VERSION, 'chain': beacon.CHAIN, 'window': 90, 'wait': 600, 'fee': 0}),
                   'secret': secret, 'commitment': hashlib.sha256(bytes.fromhex(secret)).hexdigest()}
            conn.execute(f"INSERT INTO dice_rounds({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
            for pool in POOLS:
                conn.execute('INSERT INTO dice_pools(nonce,pool) VALUES(?,?)', (nonce, pool))
            return self.get(nonce)

    def queue_message(self, conn, nonce, purpose, recipient, body, keyboard=(), thread=0):
        conn.execute('INSERT OR IGNORE INTO dice_messages(nonce,bot_id,purpose,recipient,thread,body,keyboard) VALUES(?,?,?,?,?,?,?)',
                     (nonce, self.bot_id, purpose, str(recipient), thread, body, encode(keyboard)))

    def session(self, token):
        row = self.db.one('SELECT * FROM dice_sessions WHERE token=? AND bot_id=?', (token, self.bot_id))
        if not row:
            raise PlayError('请重新进入本局私聊')
        return row

    def new_session(self, nonce, actor, *, view='home', now=None):
        with economy_write(self.db) as conn:
            clock = time.time() if now is None else now
            row = self.get(nonce)
            tg, member = self.actor(actor, now=clock)
            uid = member['emby_user_id']
            # A rebound identity never acquires another account's betting history.
            collision = conn.execute('SELECT 1 FROM dice_tickets WHERE nonce=? AND (user_id=? OR tg_id=?) AND NOT(user_id=? AND tg_id=?)', (nonce, uid, tg, uid, tg)).fetchone()
            if collision:
                raise PlayError('本局绑定已变化，请联系管理员核对')
            current = conn.execute("SELECT * FROM dice_sessions WHERE nonce=? AND user_id=? AND tg_id=? AND bot_id=? AND state='active' AND expires>? ORDER BY rowid DESC LIMIT 1", (nonce, uid, tg, self.bot_id, clock)).fetchone()
            finished = row['state'] in ('settled', 'refunded', 'empty')
            if current:
                if finished:
                    payload = json.loads(current['payload'])
                    payload.update(view='mine', pool='size', selected=None)
                    self.update_session(conn, dict(current), payload)
                    return self.session(current['token'])
                return dict(current)
            token = secrets.token_hex(10)
            payload = {'view': 'mine' if finished else view, 'pool': 'size', 'draft': [], 'ticket': secrets.token_hex(16), 'selected': None}
            conn.execute('INSERT INTO dice_sessions(token,nonce,bot_id,user_id,tg_id,payload,expires) VALUES(?,?,?,?,?,?,?)', (token, nonce, self.bot_id, uid, tg, encode(payload), max(clock+900, row['timeout']+900)))
            return self.session(token)

    def context(self, session, actor, message, data, *, now=None):
        clock = time.time() if now is None else now
        tg, member = self.actor(actor, now=clock)
        chat, sender = message.get('chat') or {}, message.get('from') or {}
        actions = [b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard', []) for b in line]
        if (session['tg_id'] != tg or session['user_id'] != member['emby_user_id'] or chat.get('type') != 'private'
                or str(chat.get('id')) != tg or sender.get('is_bot') is not True or str(sender.get('id')) != self.bot_id
                or (session['mid'] is not None and session['mid'] != message.get('message_id'))
                or type(message.get('message_id')) is not int or message['message_id'] <= 0
                or session['state'] != 'active' or session['expires'] <= clock
                or data not in actions or any(message.get(k) for k in ('forward_origin', 'forward_date', 'sender_chat'))):
            raise PlayError('请使用本人私聊的当前骰局卡')
        self.get(session['nonce'])
        if session['mid'] is None:
            with economy_write(self.db) as conn:
                job = conn.execute("SELECT * FROM dice_messages WHERE nonce=? AND purpose=? AND recipient=? AND bot_id=? AND state IN ('unknown','sending')", (session['nonce'], 'session:'+session['token'], tg, self.bot_id)).fetchone()
                if not job or data not in [b.get('callback_data') for line in json.loads(job['keyboard']) for b in line]:
                    raise PlayError('私聊回执尚未确认')
                mid = message['message_id']
                conn.execute("UPDATE dice_messages SET state='sent',mid=?,lease=0 WHERE id=?", (mid, job['id']))
                conn.execute('UPDATE dice_sessions SET mid=? WHERE token=?', (mid, session['token']))
                conn.execute("INSERT INTO dice_delivery_audit(job_id,action,mid,created) VALUES(?,'callback',?,?)", (job['id'], mid, clock))

    def update_session(self, conn, session, payload):
        conn.execute('UPDATE dice_sessions SET payload=?,revision=revision+1 WHERE token=? AND revision=?', (encode(payload), session['token'], session['revision']))

    def confirm(self, session, actor, *, now=None):
        with economy_write(self.db) as conn:
            clock = time.time() if now is None else now
            expected_revision = session['revision']
            session = self.session(session['token'])
            if expected_revision != session['revision']:
                raise PlayError('草稿已更新，已确认投注不变')
            payload = json.loads(session['payload'])
            tg, member = self.actor(actor, now=clock)
            uid = member['emby_user_id']
            if deletion_in_progress(conn, uid):
                raise PlayError('账号正在注销或结果待核查，本次未下注')
            if (tg, uid) != (session['tg_id'], session['user_id']) or session['state'] != 'active' or session['expires'] <= clock:
                raise PlayError('确认单归属或期限无效')
            ticket = payload['ticket']
            prior = conn.execute('SELECT * FROM dice_tickets WHERE token=?', (ticket,)).fetchone()
            if prior:
                return dict(prior)
            row = self.get(session['nonce'])
            if not self.enabled() or row['state'] != 'open' or clock >= row['deadline'] or not row['card_mid']:
                raise PlayError('本次未下注：已封盘或停用，已确认投注不变')
            items = validate(payload['draft'])
            amount = sum(i['amount'] for i in items)
            prior_identity = conn.execute('SELECT 1 FROM dice_tickets WHERE nonce=? AND (user_id=? OR tg_id=?) AND NOT(user_id=? AND tg_id=?)', (row['nonce'], uid, tg, uid, tg)).fetchone()
            if prior_identity:
                raise PlayError('本局绑定已变化')
            total = conn.execute('SELECT COALESCE(SUM(accepted),0) FROM dice_pools WHERE nonce=?', (row['nonce'],)).fetchone()[0]
            integer(total+amount, '本局安全整数总额')
            balance = self.points.balance(uid)
            if amount > balance:
                raise PlayError('本次未下注：余额不足，已确认投注不变')
            for item in items:
                ref = ticket+':'+item['pool']+':'+item['option']
                self.cash.reserve(conn, 'dice', ref, uid, item['amount'], now=clock)
                conn.execute('INSERT INTO dice_bets VALUES(?,?,?,?,?,?)', (ticket, row['nonce'], uid, item['pool'], item['option'], item['amount']))
                conn.execute('UPDATE dice_pools SET held=held+?,accepted=accepted+? WHERE nonce=? AND pool=?', (item['amount'], item['amount'], row['nonce'], item['pool']))
            conn.execute('INSERT INTO dice_tickets VALUES(?,?,?,?,?,?,?,?)', (ticket, row['nonce'], uid, tg, public_name(actor), encode(items), amount, clock))
            payload.update(view='success', selected=None, notice='')
            self.update_session(conn, session, payload)
            return dict(conn.execute('SELECT * FROM dice_tickets WHERE token=?', (ticket,)).fetchone())

    def recover_delivery(self, job_id, *, confirmed_mid=0, retry=False):
        """Explicit operator action, never automatic resend of an uncertain send."""
        with economy_write(self.db) as conn:
            job = conn.execute('SELECT * FROM dice_messages WHERE id=? AND bot_id=?', (job_id, self.bot_id)).fetchone()
            if not job or job['state'] not in ('unknown', 'failed'):
                raise PlayError('任务不在待核实状态')
            if confirmed_mid:
                if not job['purpose'].startswith('report:'):
                    raise PlayError('过程卡须用原Bot卡按钮确认回执，不接受手填删除目标')
                integer(confirmed_mid, '已核实消息ID')
                conn.execute("UPDATE dice_messages SET state='sent',mid=?,error='' WHERE id=?", (confirmed_mid, job_id))
                conn.execute('UPDATE dice_rounds SET cleanup_queued=0 WHERE nonce=?', (job['nonce'],))
            elif retry:
                conn.execute("UPDATE dice_messages SET state='pending',due=0,error='' WHERE id=?", (job_id,))
            else:
                raise PlayError('请填已核实消息ID，或明确允许重发；重发可能重复送达')
            conn.execute('INSERT INTO dice_delivery_audit(job_id,action,mid,created) VALUES(?,?,?,?)', (job_id, 'ack' if confirmed_mid else 'retry', confirmed_mid or None, time.time()))

    def close(self, nonce, *, now=None):
        with economy_write(self.db) as conn:
            clock = time.time() if now is None else now
            row = self.get(nonce)
            if row['state'] in ('opening', 'open') and clock >= row['deadline']:
                conn.execute("UPDATE dice_rounds SET state='locked' WHERE nonce=?", (nonce,))
            return self.get(nonce)

    def fix(self, nonce, raw, *, now=None):
        row = self.get(nonce)
        verified = beacon.verify(raw, row['round_number'])
        with economy_write(self.db) as conn:
            clock = time.time() if now is None else now
            row = self.get(nonce)
            if row['state'] == 'fixed' or row['state'] == 'settled':
                return row
            if row['state'] != 'locked' or clock < beacon.round_time(row['round_number']):
                raise PlayError('尚未到固定开奖轮次')
            if clock >= row['timeout']:
                raise PlayError('开奖等待已超时')
            seed, dice = beacon.derive(row['secret'], nonce, verified)
            conn.execute("UPDATE dice_rounds SET state='fixed',evidence=?,seed=?,dice=? WHERE nonce=?", (encode(verified), seed, encode(dice), nonce))
            return self.get(nonce)

    def settle(self, nonce, *, now=None):
        with economy_write(self.db) as conn:
            clock = time.time() if now is None else now
            row = self.get(nonce)
            if row['state'] in ('settled', 'refunded', 'empty'):
                return row
            total = conn.execute('SELECT COALESCE(SUM(accepted),0) FROM dice_pools WHERE nonce=?', (nonce,)).fetchone()[0]
            timeout = row['state'] == 'locked' and clock >= row['timeout']
            if row['state'] != 'fixed' and not (row['state'] == 'locked' and (timeout or not total)):
                return row
            if json.loads(row['rules'])['version'] != beacon.VERSION:
                raise RuntimeError('unsupported immutable dice rules')
            result = {}
            for pool in POOLS:
                bets = [dict(b) for b in conn.execute('SELECT * FROM dice_bets WHERE nonce=? AND pool=?', (nonce, pool))]
                allocation = settle_pool(pool, bets, json.loads(row['dice']), row['seed'], force_refund=timeout)
                record = conn.execute('SELECT * FROM dice_pools WHERE nonce=? AND pool=?', (nonce, pool)).fetchone()
                if record['held'] != allocation['total'] or record['accepted'] != allocation['total']:
                    raise RuntimeError('dice pool reconciliation failed')
                for bet in bets:
                    ref = bet['ticket']+':'+pool+':'+bet['option']
                    if self.cash.held(conn, 'dice', ref, bet['user_id']) != bet['amount']:
                        raise RuntimeError('dice escrow reconciliation failed')
                    self.cash._take(conn, 'dice', ref, bet['user_id'], bet['amount'], 'spent')
                    self.cash._event(conn, 'dice', ref, bet['user_id'], 'consume', bet['amount'], nonce+':'+pool, clock)
                for uid, outcome in allocation['users'].items():
                    if not self.members.get(uid):
                        raise RuntimeError('dice recipient account missing; funds retained')
                    amount = outcome['returned']
                    if amount:
                        integer(self.points.balance(uid)+amount, '结算后安全整数余额', low=0, high=MAX_INT)
                        self.points._apply(conn, uid, amount, 'dice.refund' if allocation['reason'] else 'dice.payout', nonce+':'+pool, 'dice', int(clock))
                        self.cash._event(conn, 'dice', nonce+':'+pool, uid, 'payout', amount, now=clock)
                conn.execute('UPDATE dice_pools SET held=0,result=? WHERE nonce=? AND pool=?', (encode(allocation), nonce, pool))
                result[pool] = allocation
            state = 'empty' if not total else 'refunded' if timeout else 'settled'
            conn.execute('UPDATE dice_rounds SET state=?,result=?,error=? WHERE nonce=?', (state, encode(result), '固定信标等待超时，全局退款' if timeout else '', nonce))
            return self.get(nonce)


class DicePlugin(Plugin):
    spec = Spec(id='dice', name='骰局', icon='🎲', category='points', description='七池玩家对赌 · 90秒封盘 · 0抽成 · 签名验证开奖；确认后不可撤注。沿用现有授权群与话题。恢复字段仅在手动运行时执行；不影响资金。', fields=[
        Field('receipt_job', '待核实消息任务ID（0不操作）', kind='int', default=0, min=0, max=MAX_INT),
        Field('receipt_mid', '人工核实已送达的消息ID（原聊天内）', kind='int', default=0, min=0, max=MAX_INT),
        Field('receipt_retry', '未填消息ID时允许重发（可能重复送达）', kind='bool', default=False)])

    def readonly_status(self):
        states = {r['state']: r['n'] for r in self.ctx.db.query('SELECT state,COUNT(*) n FROM dice_rounds GROUP BY state')}
        uncertain = self.ctx.db.one("SELECT COUNT(*) n FROM dice_messages WHERE state IN ('unknown','failed')")['n']
        jobs = self.ctx.db.query("SELECT id,nonce,purpose,state,error FROM dice_messages WHERE state IN ('unknown','failed') ORDER BY id DESC LIMIT 20")
        jobs = [{**j, 'purpose': j['purpose'].split(':')[0]} for j in jobs]
        from app.modules.bot_dice import DiceCleanup
        issues = self.ctx.db.query("SELECT nonce,state,error FROM dice_rounds WHERE error<>'' AND state IN ('opening','open','locked','fixed') ORDER BY created DESC LIMIT 20")
        return {'局状态': states, '财务恢复待核对': issues, '消息待核实': uncertain, '待核实任务': jobs, '过程清理': DiceCleanup(self.ctx.db).summary(), '规则': beacon.VERSION, '封盘秒数': 90, '开奖最长等待秒数': 600}

    async def run(self, config):
        if self.ctx.telegram:
            if config.get('receipt_job'):
                self.ctx.telegram._dice_service().recover_delivery(config['receipt_job'], confirmed_mid=config.get('receipt_mid', 0), retry=config.get('receipt_retry', False))
            from app.modules.bot_dice import DiceCleanup
            DiceCleanup(self.ctx.db).retry_blocked(self.ctx.telegram._active_bot_id)
            await self.ctx.telegram._dice_tick()
        return self.readonly_status()
