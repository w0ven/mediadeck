"""Two-member dice duel: challenge, accept or decline, settle in one ledger transaction."""
from __future__ import annotations

import json
import secrets
import time

from app.modules.economy_rules import economy_write, encode
from app.modules.game_mentions import telegram_username
from app.modules.play_money import CashBook, PlayError, integer
from app.modules.play_rounds import RoundCards
from app.modules.plugins import Field, Plugin, Spec
from app.modules.red_packets import public_name

DEFAULTS = {'wait_seconds': 120}
DICE = 3
MAX_STAKE = (2**63 - 1) // 4


def migrate(db):
    db._conn.execute("""CREATE TABLE IF NOT EXISTS pk_rounds (
        nonce TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'pk' CHECK(kind='pk'),
        bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
        command_message_id INTEGER NOT NULL,
        actor_tg_id TEXT NOT NULL, actor_user_id TEXT NOT NULL, actor_name TEXT NOT NULL,
        actor_username TEXT NOT NULL DEFAULT '',
        target_tg_id TEXT NOT NULL, target_user_id TEXT NOT NULL, target_name TEXT NOT NULL,
        target_username TEXT NOT NULL DEFAULT '',
        stake INTEGER NOT NULL CHECK(stake>=0), state TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL, expires_at REAL NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 0, rendered_revision INTEGER NOT NULL DEFAULT -1,
        card_message_id INTEGER, card_send_state TEXT NOT NULL DEFAULT 'pending',
        publish_token TEXT NOT NULL DEFAULT '', publish_lease REAL NOT NULL DEFAULT 0,
        next_publish_at REAL NOT NULL DEFAULT 0, publish_error TEXT NOT NULL DEFAULT '',
        UNIQUE(bot_id,chat_id,command_message_id))""")
    db._conn.execute("CREATE INDEX IF NOT EXISTS pk_rounds_pending ON pk_rounds(state,expires_at)")


def roll():
    return [secrets.randbelow(6) + 1 for _ in range(DICE)]


class PkService(RoundCards):
    rounds_table = 'pk_rounds'

    def __init__(self, db, members, points, config, enabled, allowed, bot_id):
        super().__init__(db, members, allowed, bot_id)
        self.points, self.cash = points, CashBook(points)
        self.config, self.enabled = config, enabled

    def players(self, nonce):
        return []

    @staticmethod
    def _bump(conn, nonce):
        conn.execute('UPDATE pk_rounds SET revision=revision+1,next_publish_at=0 WHERE nonce=?', (nonce,))

    def _opponent(self, conn, target):
        """The challenged member from a reply, a text mention or an @username."""
        tg = str(target.get('tg_id') or '')
        user = dict(target.get('user') or {})
        if not tg:
            handle = telegram_username(target.get('username'))
            if not handle:
                raise PlayError('请回复对方的消息，或 @对方')
            rows = conn.execute("SELECT tg_user_id FROM members WHERE tg_user_id!='' AND LTRIM(tg_username,'@')=? COLLATE NOCASE",
                                (handle,)).fetchall()
            if len(rows) != 1:
                raise PlayError(f'找不到绑定了 @{handle} 的会员')
            tg = str(rows[0]['tg_user_id'])
            user = {'username': handle}
        if user.get('is_bot'):
            raise PlayError('不能和机器人 PK')
        try:
            tg, member = self.actor({'id': int(tg), 'is_bot': False})
        except (PlayError, ValueError):
            raise PlayError('对方不是有效的绑定会员，不能 PK') from None
        name = public_name(user) if user.get('first_name') else str(member.get('tg_display_name') or '成员')[:128]
        username = telegram_username(user.get('username') or member.get('tg_username'))
        return tg, member, name, username

    @staticmethod
    def _busy(conn, *ids):
        marks = ','.join('?' for _ in ids)
        return conn.execute(f"SELECT 1 FROM pk_rounds WHERE state='pending' AND (actor_user_id IN ({marks}) OR target_user_id IN ({marks}))",
                            (*ids, *ids)).fetchone()

    def create(self, message, target, stake=0, *, now=None):
        clock = time.time() if now is None else float(now)
        self.expire_due(now=clock)
        chat, thread = self.group(message)
        integer(message.get('message_id'), '消息ID')
        stake = 0 if not stake else integer(stake, '押注积分', 1, MAX_STAKE)
        with economy_write(self.db) as conn:
            tg, member = self.actor(message.get('from') or {}, now=clock)
            prior = conn.execute('SELECT * FROM pk_rounds WHERE bot_id=? AND chat_id=? AND command_message_id=?',
                                 (self.bot_id, chat, message['message_id'])).fetchone()
            if prior:
                if prior['actor_user_id'] != member['emby_user_id'] or prior['stake'] != stake:
                    raise PlayError('原消息已发起另一场 PK，请发送新命令')
                return dict(prior)
            if not self.enabled():
                raise PlayError('骰子 PK 暂未开放')
            target_tg, target_member, target_name, target_username = self._opponent(conn, target)
            if target_tg == tg or target_member['emby_user_id'] == member['emby_user_id']:
                raise PlayError('不能和自己 PK')
            if self._busy(conn, member['emby_user_id']):
                raise PlayError('你还有一场 PK 在等对方回应')
            if self._busy(conn, target_member['emby_user_id']):
                raise PlayError('对方正在另一场 PK 里，稍后再来')
            nonce = secrets.token_hex(12)
            cfg = {**DEFAULTS, **self.config()}
            row = {'nonce': nonce, 'bot_id': self.bot_id, 'chat_id': chat, 'thread_id': thread,
                   'command_message_id': message['message_id'], 'actor_tg_id': tg,
                   'actor_user_id': member['emby_user_id'], 'actor_name': public_name(message['from']),
                   'actor_username': telegram_username((message.get('from') or {}).get('username')),
                   'target_tg_id': target_tg, 'target_user_id': target_member['emby_user_id'],
                   'target_name': target_name, 'target_username': target_username, 'stake': stake,
                   'created_at': clock, 'expires_at': clock + int(cfg['wait_seconds'])}
            conn.execute(f"INSERT INTO pk_rounds({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
            if stake:
                try:
                    self.cash.reserve(conn, 'pk', nonce + ':actor', member['emby_user_id'], stake, now=clock)
                except ValueError:
                    raise PlayError(f'押注需要 {stake} 积分，你的积分不足') from None
            return self.get(nonce)

    def _close(self, conn, row, state, reason, clock):
        if row['state'] != 'pending':
            return
        refunded = self.cash.release(conn, 'pk', row['nonce'] + ':actor', row['actor_user_id'], now=clock) if row['stake'] else 0
        conn.execute('UPDATE pk_rounds SET state=?,result_json=? WHERE nonce=?',
                     (state, encode({'reason': reason, 'refunded': refunded}), row['nonce']))
        self._bump(conn, row['nonce'])

    def _settle(self, conn, row, clock):
        try:
            _, challenger = self.actor({'id': int(row['actor_tg_id']), 'is_bot': False}, now=clock)
        except PlayError:
            challenger = None
        if not challenger or challenger['emby_user_id'] != row['actor_user_id']:
            self._close(conn, row, 'cancelled', '发起人已不是有效会员', clock)
            return
        stake = row['stake']
        if stake:
            if self.cash.held(conn, 'pk', row['nonce'] + ':actor', row['actor_user_id']) != stake:
                raise ValueError('unsafe challenger escrow')
            try:
                self.cash.reserve(conn, 'pk', row['nonce'] + ':target', row['target_user_id'], stake, now=clock)
            except ValueError:
                raise PlayError(f'应战需要 {stake} 积分，你的积分不足') from None
        dice = {'actor': roll(), 'target': roll()}
        totals = {side: sum(values) for side, values in dice.items()}
        winner = None if totals['actor'] == totals['target'] else max(totals, key=totals.get)
        if stake:
            sides = {'actor': row['actor_user_id'], 'target': row['target_user_id']}
            if winner is None:
                for side, uid in sides.items():
                    self.cash.release(conn, 'pk', f"{row['nonce']}:{side}", uid, now=clock)
            else:
                fund = 'pk:' + row['nonce']
                for side, uid in sides.items():
                    self.cash.consume(conn, 'pk', f"{row['nonce']}:{side}", uid, stake, 'poker', fund, now=clock)
                self.cash.payout(conn, 'poker', fund, sides[winner], 2 * stake, now=clock)
        conn.execute("UPDATE pk_rounds SET state='settled',result_json=? WHERE nonce=?",
                     (encode({'dice': dice, 'totals': totals, 'winner': winner}), row['nonce']))
        self._bump(conn, row['nonce'])

    def respond(self, nonce, actor, message, op, *, now=None):
        clock = time.time() if now is None else float(now)
        self.expire(nonce, now=clock)
        if op not in ('accept', 'decline'):
            raise PlayError('PK 按钮无效')
        try:
            with economy_write(self.db) as conn:
                row = self.get(nonce)
                if not row:
                    raise PlayError('这场 PK 不存在')
                self.context(conn, row, message, f'pk:{nonce}:{op}')
                if row['state'] != 'pending':
                    return row
                tg, member = self.actor(actor, now=clock)
                uid = member['emby_user_id']
                is_target = tg == row['target_tg_id'] and uid == row['target_user_id']
                is_actor = tg == row['actor_tg_id'] and uid == row['actor_user_id']
                if op == 'decline':
                    if not (is_target or is_actor):
                        raise PlayError('这不是你的 PK')
                    self._close(conn, row, 'declined' if is_target else 'withdrawn', '', clock)
                else:
                    if not is_target:
                        raise PlayError('只有被挑战的人可以应战' if not is_actor else '等对方应战')
                    if not self.enabled():
                        raise PlayError('骰子 PK 暂未开放')
                    self._settle(conn, row, clock)
                return self.get(nonce)
        except (ValueError, RuntimeError) as exc:
            if isinstance(exc, PlayError):
                raise
            with economy_write(self.db) as conn:
                self._close(conn, self.get(nonce), 'cancelled', 'PK 未能安全进行', clock)
            return self.get(nonce)

    def expire(self, nonce, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if row and row['state'] == 'pending' and (row['expires_at'] <= clock or not self.enabled()):
                self._close(conn, row, 'expired' if row['expires_at'] <= clock else 'cancelled',
                            '' if row['expires_at'] <= clock else '骰子 PK 已关闭', clock)
            return self.get(nonce)

    def expire_due(self, *, now=None):
        for row in self.db.query("SELECT nonce FROM pk_rounds WHERE state='pending'"):
            self.expire(row['nonce'], now=now)

    def cancel_unsent(self, nonce, reason, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if row and row['card_message_id'] is None:
                self._close(conn, row, 'cancelled', reason, clock)


def result_of(row):
    return json.loads(row['result_json'] or '{}')


class PkPlugin(Plugin):
    spec = Spec(id='pk', name='骰子 PK', icon='🎲', category='points',
                description='回复对方消息或 @对方 发 /pk，可带押注积分；对方接受后各掷三颗骰子，点数大者赢走对方押注，平局退回，无抽成。',
                fields=[Field('wait_seconds', '等待对方回应秒数', kind='int', default=120, min=30, max=3600)])

    def readonly_status(self):
        states = {r['state']: r['n'] for r in self.ctx.db.query('SELECT state,COUNT(*) n FROM pk_rounds GROUP BY state')}
        return {'等待回应': states.get('pending', 0), '已开骰': states.get('settled', 0),
                '拒绝/撤回/超时': states.get('declined', 0) + states.get('withdrawn', 0)
                + states.get('expired', 0) + states.get('cancelled', 0)}

    async def run(self, config):
        if self.ctx.telegram:
            await self.ctx.telegram._pk_tick()
        return self.readonly_status()
