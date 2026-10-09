"""A shared nine-cell activity. Fees and independent system rewards are not a pool."""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.modules.economy_rules import BEIJING, economy_write, encode
from app.modules.play_money import PlayError
from app.modules.play_rounds import PlayAccess
from app.modules.plugins import Field, Plugin, Spec
from app.modules.red_packets import public_name

TIERS = [{'min': low, 'max': high, 'probability': p} for low, high, p in
         ((0, 0, 3), (1, 30, 70), (31, 50, 20), (51, 100, 4),
          (101, 200, 2), (201, 400, .8), (401, 888, .2))]
DEFAULTS = {'cost': 30, 'per_person': 1, 'duration_minutes': 30,
            'schedule_times': '12:00,20:00', 'reward_tiers': encode(TIERS)}
DRAW_SIZE = 1_000_000


def configuration(raw):
    cfg = {**DEFAULTS, **(raw or {})}
    for key, maximum in (('cost', 100000), ('per_person', 9), ('duration_minutes', 1440)):
        value = cfg[key]
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'{key} 须为1～{maximum}的整数')
    hours = [v.strip() for v in str(cfg['schedule_times']).split(',') if v.strip()]
    if len(hours) > 12 or len(set(hours)) != len(hours) or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', v) for v in hours):
        raise ValueError('定时时段须为不重复的北京时间HH:MM，以逗号分隔，最多12个')
    cfg['schedule_times'] = ','.join(sorted(hours))
    try:
        tiers = json.loads(cfg['reward_tiers'], parse_float=Decimal)
        if not isinstance(tiers, list) or not 1 <= len(tiers) <= 40:
            raise ValueError('奖励档位须为1～40项JSON数组')
        total = Decimal(0)
        bounds = []
        normalized = []
        # Snapshot shape is the rules version: all old reward entries remain
        # fixed-value draws, while min/max entries are closed uniform ranges.
        shape = None
        for tier in tiers:
            if not isinstance(tier, dict):
                raise TypeError('奖励档位须为对象')
            keys = set(tier)
            if keys not in ({'reward', 'probability'}, {'min', 'max', 'probability'}):
                raise ValueError('区间档位仅含min、max、probability；旧固定档兼容reward、probability')
            if shape is not None and keys != shape:
                raise ValueError('同一配置不可混用固定奖励与区间档位')
            shape = keys
            low, high = (tier['reward'], tier['reward']) if 'reward' in tier else (tier['min'], tier['max'])
            if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 1000000:
                raise ValueError('奖励边界须为0～1000000整数且min不大于max')
            if any(low <= end and high >= start for start, end in bounds):
                raise ValueError('奖励区间不可重叠，闭区间端点也不可重复')
            if 'reward' not in tier and (isinstance(tier['probability'], (bool, str))):
                raise ValueError('概率须为数值百分比')
            probability = Decimal(str(tier['probability']))
            if (not probability.is_finite() or not 0 <= probability <= 100
                    or probability * 10000 != (probability * 10000).to_integral_value()):
                raise ValueError('概率须为0～100的数，最多4位小数')
            total += probability
            bounds.append((low, high))
            normalized.append({**tier, 'probability': float(probability)})
        if total != 100:
            raise ValueError('奖励概率之和必须严格为100%')
    except (TypeError, InvalidOperation, json.JSONDecodeError, KeyError) as exc:
        raise ValueError('奖励档位JSON无效') from exc
    cfg['reward_tiers'] = encode(normalized)
    return {k: cfg[k] for k in DEFAULTS}


def reward(config, randbelow=None):
    rng = randbelow or secrets.randbelow
    draw = rng(DRAW_SIZE)
    if type(draw) is not int or not 0 <= draw < DRAW_SIZE:
        raise ValueError('随机值超出范围')
    bound = 0
    for tier in json.loads(config['reward_tiers']):
        bound += int(Decimal(str(tier['probability'])) * 10000)
        if draw < bound:
            if 'reward' in tier:
                return tier['reward']  # historical fixed-value snapshot: one draw
            width = tier['max'] - tier['min'] + 1
            offset = rng(width)
            if type(offset) is not int or not 0 <= offset < width:
                raise ValueError('区间随机值超出范围')
            return tier['min'] + offset
    raise RuntimeError('奖励权重无效')


def max_reward(config):
    # Zero-weight tiers are not possible prizes. Never peek at an unclaimed draw.
    return max(t.get('max', t.get('reward')) for t in json.loads(config['reward_tiers'])
               if Decimal(str(t['probability'])) > 0)


def next_slots(config, now, count=2):
    local = datetime.fromtimestamp(now, BEIJING)
    values = []
    for day in range(3):
        for hm in config['schedule_times'].split(','):
            if not hm:
                continue
            hour, minute = map(int, hm.split(':'))
            slot = (local + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot.timestamp() > now:
                values.append(slot)
    return sorted(values)[:count]


def migrate(db):
    db._conn.executescript('''
    CREATE TABLE IF NOT EXISTS scratch9_rounds (
        nonce TEXT PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        chat_username TEXT NOT NULL DEFAULT '', thread_id INTEGER NOT NULL DEFAULT 0,
        command_message_id INTEGER, config_json TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', end_reason TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0, rendered_revision INTEGER NOT NULL DEFAULT -1,
        card_message_id INTEGER, card_send_state TEXT NOT NULL DEFAULT 'pending',
        publish_token TEXT NOT NULL DEFAULT '', publish_lease REAL NOT NULL DEFAULT 0,
        publish_attempts INTEGER NOT NULL DEFAULT 0, next_publish_at REAL NOT NULL DEFAULT 0,
        UNIQUE(bot_id,chat_id,command_message_id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS scratch9_one_active
        ON scratch9_rounds(bot_id,chat_id) WHERE state IN ('pending','active','unknown');
    CREATE TABLE IF NOT EXISTS scratch9_cells (
        nonce TEXT NOT NULL REFERENCES scratch9_rounds(nonce), cell INTEGER NOT NULL CHECK(cell BETWEEN 1 AND 9),
        user_id TEXT NOT NULL, tg_id TEXT NOT NULL, display_name TEXT NOT NULL,
        cost INTEGER NOT NULL CHECK(cost>0), reward INTEGER NOT NULL CHECK(reward>=0),
        cost_ledger_id INTEGER NOT NULL, reward_ledger_id INTEGER, claimed_at REAL NOT NULL,
        PRIMARY KEY(nonce,cell)
    );
    CREATE TABLE IF NOT EXISTS scratch9_intents (
        token TEXT PRIMARY KEY, nonce TEXT NOT NULL REFERENCES scratch9_rounds(nonce),
        cell INTEGER NOT NULL CHECK(cell BETWEEN 1 AND 9), user_id TEXT NOT NULL, tg_id TEXT NOT NULL,
        bot_id TEXT NOT NULL, source_request TEXT NOT NULL, chat_id TEXT NOT NULL,
        thread_id INTEGER NOT NULL, card_message_id INTEGER NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'created', message_id INTEGER,
        delivery_lease REAL NOT NULL DEFAULT 0, result_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(bot_id,source_request)
    );
    CREATE TABLE IF NOT EXISTS scratch9_clock (
        bot_id TEXT PRIMARY KEY, boot_key TEXT NOT NULL, config_key TEXT NOT NULL,
        last_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS scratch9_slots (
        bot_id TEXT NOT NULL, destination TEXT NOT NULL, slot_at REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', nonce TEXT,
        PRIMARY KEY(bot_id,destination,slot_at)
    );
    ''')


class Scratch9Service(PlayAccess):
    def __init__(self, db, members, points, config, enabled, allowed, bot_id, *, randbelow=None):
        super().__init__(db, members, allowed, bot_id)
        self.points, self._config, self.enabled = points, config, enabled
        self.randbelow = randbelow

    def config(self):
        return configuration(self._config())

    def get(self, nonce):
        return self.db.one('SELECT * FROM scratch9_rounds WHERE nonce=?', (nonce,))

    def cells(self, nonce):
        return self.db.query('SELECT * FROM scratch9_cells WHERE nonce=? ORDER BY cell', (nonce,))

    def _insert(self, conn, chat, thread, command, now):
        cfg = self.config()
        nonce = secrets.token_hex(12)
        conn.execute('INSERT INTO scratch9_rounds(nonce,bot_id,chat_id,chat_username,thread_id,command_message_id,config_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (nonce, self.bot_id, str(chat['id']), str(chat.get('username') or ''), thread, command, encode(cfg), now, now + cfg['duration_minutes']*60))
        return self.get(nonce)

    def create(self, message, *, now=None):
        clock = time.time() if now is None else now
        chat, thread = self.group(message)
        mid = message.get('message_id')
        if type(mid) is not int or mid <= 0:
            raise PlayError('开场消息无效')
        if not self.enabled():
            raise PlayError('刮刮乐暂未开启')
        with economy_write(self.db) as conn:
            _, member = self.actor(message.get('from') or {}, now=clock)
            if 'admin' not in (member.get('roles') or []):
                raise PlayError('仅当前绑定管理员可开启刮刮乐')
            old = conn.execute('SELECT * FROM scratch9_rounds WHERE bot_id=? AND chat_id=? AND command_message_id=?', (self.bot_id, chat, mid)).fetchone()
            if old:
                return dict(old)
            self._expire(conn, clock)
            old = conn.execute("SELECT * FROM scratch9_rounds WHERE bot_id=? AND chat_id=? AND state IN ('pending','active','unknown')", (self.bot_id, chat)).fetchone()
            if old:
                return dict(old)
            return self._insert(conn, message['chat'], thread, mid, clock)

    def context(self, row, message, data):
        chat, thread = self.group(message)
        sender = message.get('from') or {}
        actions = [b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard', []) for b in line]
        if (row['bot_id'] != self.bot_id or row['chat_id'] != chat or row['thread_id'] != thread
                or row['card_message_id'] is None or message.get('message_id') != row['card_message_id']
                or type(message.get('message_id')) is not int or sender.get('is_bot') is not True
                or str(sender.get('id')) != self.bot_id or data not in actions):
            raise PlayError('请使用原群、原话题的原Bot刮刮乐卡')

    def _live(self, row, now):
        if not self.enabled():
            raise PlayError('刮刮乐已暂停，不会扣积分')
        if (row['state'] != 'active' or row['card_message_id'] is None
                or row['card_send_state'] != 'sent' or now >= row['expires_at']):
            raise PlayError('本场尚未开放或已经结束，不会扣积分')
        if not self.allowed({'id': int(row['chat_id']), 'username': row['chat_username'], 'type': 'supergroup'}):
            raise PlayError('当前群不再获授权，不会扣积分')

    def select(self, nonce, cell, actor, message, request, *, now=None):
        clock = time.time() if now is None else now
        if type(cell) is not int or not 1 <= cell <= 9 or not request or len(request) > 120:
            raise PlayError('格子或请求无效')
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row:
                raise PlayError('活动不存在')
            self.context(row, message, f'gg:{nonce}:{cell}')
            tg, member = self.actor(actor, now=clock)
            uid = member['emby_user_id']
            if conn.execute('SELECT 1 FROM scratch9_cells WHERE nonce=? AND cell=?', (nonce, cell)).fetchone():
                raise PlayError('这个格子已被刮开，不会扣积分')
            self._live(row, clock)
            cfg = json.loads(row['config_json'])
            if conn.execute('SELECT COUNT(*) FROM scratch9_cells WHERE nonce=? AND (user_id=? OR tg_id=?)', (nonce, uid, tg)).fetchone()[0] >= cfg['per_person']:
                raise PlayError('本场已参与，已达到本场次数上限')
            prior = conn.execute('SELECT * FROM scratch9_intents WHERE bot_id=? AND source_request=?', (self.bot_id, request)).fetchone()
            if prior:
                if prior['nonce'] != nonce or prior['cell'] != cell or prior['tg_id'] != tg or prior['user_id'] != uid:
                    raise PlayError('重复请求与原操作不一致')
                if prior['expires_at'] <= clock:
                    raise PlayError('确认已过期，未扣积分，请重新点击选格')
                return dict(prior)
            if self.points.balance(uid) < cfg['cost']:
                raise PlayError(f'积分不足，本次需要{cfg["cost"]}积分')
            # A private v0.40.5 intent is never reused. A first click reserves
            # neither a cell nor money; changing cells cancels only this actor's intent.
            pending = conn.execute("SELECT * FROM scratch9_intents WHERE bot_id=? AND nonce=? AND (user_id=? OR tg_id=?) AND state='group_pending' ORDER BY created_at DESC LIMIT 1",
                                   (self.bot_id, nonce, uid, tg)).fetchone()
            if (pending and pending['user_id'] == uid and pending['tg_id'] == tg
                    and pending['cell'] == cell and pending['expires_at'] > clock):
                return dict(pending, _confirm=True)
            conn.execute("UPDATE scratch9_intents SET state='cancelled' WHERE bot_id=? AND nonce=? AND (user_id=? OR tg_id=?) AND state='group_pending'",
                         (self.bot_id, nonce, uid, tg))
            token = secrets.token_hex(12)
            conn.execute("INSERT INTO scratch9_intents(token,nonce,cell,user_id,tg_id,bot_id,source_request,chat_id,thread_id,card_message_id,created_at,expires_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'group_pending')",
                         (token, nonce, cell, uid, tg, self.bot_id, request, row['chat_id'], row['thread_id'], row['card_message_id'], clock, min(clock+120, row['expires_at'])))
            return dict(conn.execute('SELECT * FROM scratch9_intents WHERE token=?', (token,)).fetchone(),
                        _expired=bool(pending and pending['expires_at'] <= clock))

    def confirm(self, token, actor, message, action, *, request=None, now=None):
        clock = time.time() if now is None else now
        if action not in ('yes', 'no'):
            raise PlayError('确认按钮无效')
        with economy_write(self.db) as conn:
            raw = conn.execute('SELECT * FROM scratch9_intents WHERE token=?', (token,)).fetchone()
            if not raw:
                raise PlayError('确认已失效，请重新选格')
            intent = dict(raw)
            tg, member = self.actor(actor, now=clock)
            row = self.get(intent['nonce'])
            self.context(row, message, f'gg:{intent["nonce"]}:{intent["cell"]}')
            if (intent['bot_id'] != self.bot_id or intent['tg_id'] != tg or intent['user_id'] != member['emby_user_id']
                    or (row['chat_id'], row['thread_id'], row['card_message_id']) != (intent['chat_id'], intent['thread_id'], intent['card_message_id'])
                    or not request or len(request) > 120 or request == intent['source_request']):
                raise PlayError('请由本人再次点击原群原卡的同一格确认')
            if intent['state'] == 'done':
                return json.loads(intent['result_json'])
            if intent['state'] == 'cancelled':
                return {'cancelled': True, 'nonce': intent['nonce']}
            if intent['state'] != 'group_pending':
                raise PlayError('确认已失效，请在原群原卡重新选格，不会扣积分')
            if action == 'no':
                conn.execute("UPDATE scratch9_intents SET state='cancelled' WHERE token=?", (token,))
                return {'cancelled': True, 'nonce': intent['nonce']}
            if clock >= intent['expires_at']:
                raise PlayError('确认已超时，不会扣积分，请重新选格')
            self._live(row, clock)
            if conn.execute('SELECT 1 FROM scratch9_cells WHERE nonce=? AND cell=?', (row['nonce'], intent['cell'])).fetchone():
                raise PlayError('这个格子已被刮开，被别人抢先领取，不会扣积分')
            cfg = json.loads(row['config_json'])
            uid = member['emby_user_id']
            if conn.execute('SELECT COUNT(*) FROM scratch9_cells WHERE nonce=? AND (user_id=? OR tg_id=?)', (row['nonce'], uid, tg)).fetchone()[0] >= cfg['per_person']:
                raise PlayError('本场已参与，已达到本场次数上限，不会扣积分')
            ref = row['nonce']+':'+str(intent['cell'])
            # Debit first validates balance. Any later failure rolls both writes back.
            try:
                self.points._apply(conn, uid, -cfg['cost'], 'scratch9.cost', ref, 'scratch9', int(clock))
            except ValueError as exc:
                raise PlayError(f'积分不足，本次需要{cfg["cost"]}积分') from exc
            cost_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            amount = reward(cfg, self.randbelow)
            reward_id = None
            if amount:
                self.points._apply(conn, uid, amount, 'scratch9.reward', ref, 'scratch9', int(clock))
                reward_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute('INSERT INTO scratch9_cells(nonce,cell,user_id,tg_id,display_name,cost,reward,cost_ledger_id,reward_ledger_id,claimed_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (row['nonce'], intent['cell'], uid, tg, public_name(actor), cfg['cost'], amount, cost_id, reward_id, clock))
            count = conn.execute('SELECT COUNT(*) FROM scratch9_cells WHERE nonce=?', (row['nonce'],)).fetchone()[0]
            conn.execute('UPDATE scratch9_rounds SET revision=revision+1,state=?,end_reason=? WHERE nonce=?', ('closed' if count == 9 else 'active', 'complete' if count == 9 else '', row['nonce']))
            result = {'nonce': row['nonce'], 'cell': intent['cell'], 'reward': amount}
            conn.execute("UPDATE scratch9_intents SET state='done',result_json=? WHERE token=?", (encode(result), token))
            conn.execute("INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,'scratch9','scratch9.claim',?,?,1)",
                         (int(clock), uid, encode({'nonce': row['nonce'], 'cell': intent['cell'], 'cost': cfg['cost'], 'reward': amount, 'cost_ledger_id': cost_id, 'reward_ledger_id': reward_id})))
            return result

    def _expire(self, conn, clock):
        conn.execute("UPDATE scratch9_rounds SET state='closed',end_reason='timeout',revision=revision+1 WHERE bot_id=? AND state IN ('pending','active','unknown') AND expires_at<=?", (self.bot_id, clock))

    def expire_due(self, *, now=None):
        clock = time.time() if now is None else now
        with economy_write(self.db) as conn:
            self._expire(conn, clock)
            if not self.enabled():
                conn.execute("UPDATE scratch9_rounds SET state='closed',end_reason='disabled',revision=revision+1 WHERE bot_id=? AND state IN ('pending','active','unknown')", (self.bot_id,))
            conn.execute("UPDATE scratch9_intents SET state='unknown' WHERE bot_id=? AND state='sending' AND delivery_lease<=?", (self.bot_id, clock))
            conn.execute("UPDATE scratch9_rounds SET state=CASE WHEN state='closed' THEN 'closed' ELSE 'unknown' END,card_send_state='unknown',publish_lease=0,publish_token='' WHERE bot_id=? AND card_message_id IS NULL AND card_send_state='sending' AND publish_lease<=?", (self.bot_id, clock))

    def begin_publish(self, nonce, *, now=None):
        clock = time.time() if now is None else now
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if (not row or row['bot_id'] != self.bot_id or row['next_publish_at'] > clock
                    or row['publish_lease'] > clock or row['rendered_revision'] >= row['revision']):
                return None
            if row['card_message_id'] is None and (row['card_send_state'] != 'pending' or row['state'] != 'pending' or clock >= row['expires_at']):
                return None
            token = secrets.token_hex(12)
            conn.execute("UPDATE scratch9_rounds SET publish_token=?,publish_lease=?,publish_attempts=publish_attempts+1,card_send_state=? WHERE nonce=?", (token, clock+90, 'sending' if row['card_message_id'] is None else 'sent', nonce))
            return dict(row, publish_token=token, _cells=self.cells(nonce))

    def published(self, row, mid):
        if type(mid) is not int or mid <= 0:
            return False
        with economy_write(self.db) as conn:
            current = self.get(row['nonce'])
            if current['publish_token'] != row['publish_token'] or current['card_message_id'] not in (None, mid):
                return False
            conn.execute("UPDATE scratch9_rounds SET card_message_id=?,card_send_state='sent',state=CASE WHEN state='pending' THEN 'active' ELSE state END,rendered_revision=MAX(rendered_revision,?),publish_token='',publish_lease=0,next_publish_at=0 WHERE nonce=?", (mid, row['revision'], row['nonce']))
            return True

    def publish_failed(self, row, failure, *, now=None):
        clock = time.time() if now is None else now
        with economy_write(self.db) as conn:
            current = self.get(row['nonce'])
            if current['publish_token'] != row['publish_token']:
                return
            if current['card_message_id'] is not None:
                conn.execute("UPDATE scratch9_rounds SET publish_token='',publish_lease=0,next_publish_at=? WHERE nonce=?", (clock+max(5, int(failure.get('retry_after') or 10)), row['nonce']))
            else:
                retry = failure.get('state') == 'retry' and current['publish_attempts'] < 3 and clock < current['expires_at']
                explicit = failure.get('state') in ('failed', 'retry')
                state = 'pending' if retry else 'closed' if explicit else 'unknown'
                conn.execute("UPDATE scratch9_rounds SET state=?,end_reason=?,card_send_state=?,publish_token='',publish_lease=0,next_publish_at=? WHERE nonce=?", (state, 'failed' if explicit and not retry else '', 'pending' if retry else 'failed' if explicit else 'unknown', clock+max(5, int(failure.get('retry_after') or 10)), row['nonce']))

    def pending_cards(self, *, now=None):
        clock = time.time() if now is None else now
        return self.db.query("SELECT nonce FROM scratch9_rounds WHERE bot_id=? AND revision>rendered_revision AND (card_message_id IS NOT NULL OR (state='pending' AND card_send_state='pending')) AND publish_lease<=? AND next_publish_at<=? ORDER BY created_at LIMIT 50", (self.bot_id, clock, clock))

    def schedule(self, destinations, boot_key, *, now=None, bot_enabled=True):
        clock = time.time() if now is None else now
        cfg = self.config()
        key = encode([self.enabled(), bot_enabled, sorted(destinations), cfg])
        with economy_write(self.db) as conn:
            previous = conn.execute('SELECT * FROM scratch9_clock WHERE bot_id=?', (self.bot_id,)).fetchone()
            conn.execute('INSERT INTO scratch9_clock(bot_id,boot_key,config_key,last_at) VALUES(?,?,?,?) ON CONFLICT(bot_id) DO UPDATE SET boot_key=excluded.boot_key,config_key=excluded.config_key,last_at=excluded.last_at', (self.bot_id, boot_key, key, clock))
            # New process, enable, config/recipient change, or stalled worker never catches up.
            if (not previous or previous['boot_key'] != boot_key or previous['config_key'] != key
                    or not self.enabled() or not bot_enabled or not 0 <= clock-previous['last_at'] <= 30):
                conn.execute("UPDATE scratch9_slots SET state='skipped' WHERE bot_id=? AND state='queued' AND slot_at<=?", (self.bot_id, clock))
                return []
            conn.execute("UPDATE scratch9_slots SET state='skipped' WHERE bot_id=? AND state='queued' AND slot_at<?", (self.bot_id, clock-30))
            local = datetime.fromtimestamp(clock, BEIJING)
            for hm in cfg['schedule_times'].split(','):
                if not hm:
                    continue
                hour, minute = map(int, hm.split(':'))
                slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
                if previous['last_at'] < slot <= clock:
                    for destination in destinations:
                        conn.execute('INSERT OR IGNORE INTO scratch9_slots(bot_id,destination,slot_at) VALUES(?,?,?)', (self.bot_id, str(destination), slot))
            return [dict(r) for r in conn.execute("SELECT * FROM scratch9_slots WHERE bot_id=? AND state='queued' AND slot_at>=? AND slot_at<=?", (self.bot_id, clock-30, clock))]

    def auto_create(self, slot, chat, *, now=None):
        clock = time.time() if now is None else now
        if (chat.get('type') not in ('group', 'supergroup') or type(chat.get('id')) is not int
                or not self.allowed(chat) or not self.enabled()):
            return None
        key = (self.bot_id, slot['destination'], slot['slot_at'])
        with economy_write(self.db) as conn:
            current = conn.execute('SELECT * FROM scratch9_slots WHERE bot_id=? AND destination=? AND slot_at=?', key).fetchone()
            if not current or current['state'] != 'queued' or not 0 <= clock-current['slot_at'] <= 30:
                return None
            self._expire(conn, clock)
            old = conn.execute("SELECT * FROM scratch9_rounds WHERE bot_id=? AND chat_id=? AND state IN ('pending','active','unknown')", (self.bot_id, str(chat['id']))).fetchone()
            if old:
                conn.execute("UPDATE scratch9_slots SET state='skipped' WHERE bot_id=? AND destination=? AND slot_at=?", key)
                return None
            row = self._insert(conn, chat, 0, None, clock)
            conn.execute("UPDATE scratch9_slots SET state='created',nonce=? WHERE bot_id=? AND destination=? AND slot_at=?", (row['nonce'], *key))
            return row


class Scratch9Plugin(Plugin):
    spec = Spec(id='scratch9', name='九宫格刮刮乐', icon='🎟', category='points',
                description='全群共享9格，确认后扣费随机揭晓；奖励可能为0或低于投入。',
                fields=[Field('cost', '每格积分', kind='int', default=30, min=1, max=100000),
                        Field('per_person', '每人每场最多格数', kind='int', default=1, min=1, max=9),
                        Field('duration_minutes', '每场分钟数', kind='int', default=30, min=1, max=1440),
                        Field('schedule_times', '每日北京时间', default=DEFAULTS['schedule_times'], help='HH:MM逗号分隔；空白仅手动开启。'),
                        Field('reward_tiers', '奖励区间及概率（仅后台）', kind='text', default=DEFAULTS['reward_tiers'], help='JSON数组：min/max为闭区间整数，不能重叠；probability为百分比，最多4位小数且合计100。先选档，再在档内均匀抽整数。旧reward格式保旧规则；新配置仅影响新场。')])

    def validate_config(self, cfg):
        configuration(cfg)

    def normalize_config(self, cfg):
        return configuration(cfg)

    def readonly_status(self):
        cfg = self.ctx.registry.config('scratch9')
        active = self.ctx.db.one("SELECT COUNT(*) n FROM scratch9_rounds WHERE state IN ('pending','active','unknown')")['n']
        future = next_slots(cfg, time.time())
        return {'进行中或发送待确认': active,
                '下一未来时段': '、'.join(d.strftime('%m-%d %H:%M') for d in future) if future else '仅手动',
                '时区': '北京时间', '历史补发': '关闭'}

    async def run(self, config):
        if self.ctx.telegram:
            await self.ctx.telegram._scratch9_tick()
        return self.readonly_status()
