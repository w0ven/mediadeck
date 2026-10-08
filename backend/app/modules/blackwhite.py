"""Five equal real stakes; sealed choices; no rake and no administrator exemption."""
from __future__ import annotations

import json
import secrets
import time

from app.modules.economy_rules import economy_write, encode
from app.modules.play_money import CashBook, PlayError, integer
from app.modules.play_rounds import RoundCards, fair_split
from app.modules.plugins import Field, Plugin, Spec
from app.modules.red_packets import public_name

DEFAULTS = {'min_stake': 10, 'max_stake': 500, 'default_stake': 10, 'lobby_seconds': 600}


class BlackwhiteService(RoundCards):
    def __init__(self, db, members, points, config, enabled, allowed, bot_id):
        super().__init__(db, members, allowed, bot_id)
        self.points, self.cash = points, CashBook(points)
        self.config, self.enabled = config, enabled

    def create(self, message, stake=None, *, now=None):
        clock = time.time() if now is None else float(now)
        self.expire_due(now=clock)
        chat, thread = self.group(message)
        mid = message.get('message_id')
        integer(mid, '消息ID')
        with economy_write(self.db) as conn:
            tg, member = self.actor(message.get('from') or {}, now=clock)
            if not self.enabled():
                raise PlayError('黑白板暂未开放')
            cfg = {**DEFAULTS, **self.config()}
            stake = cfg['default_stake'] if stake is None else stake
            integer(stake, '押注', cfg['min_stake'], cfg['max_stake'])
            prior = conn.execute("SELECT * FROM play_rounds WHERE kind='blackwhite' AND bot_id=? AND chat_id=? AND command_message_id=?", (self.bot_id, chat, mid)).fetchone()
            if prior:
                if (prior['thread_id'] != thread or prior['actor_tg_id'] != tg
                        or prior['actor_user_id'] != member['emby_user_id'] or prior['stake'] != stake):
                    raise PlayError('原消息已用于其他游戏意图')
                return dict(prior)
            active = conn.execute("SELECT nonce FROM play_rounds WHERE kind='blackwhite' AND bot_id=? AND chat_id=? AND thread_id=? AND state='lobby'", (self.bot_id, chat, thread)).fetchone()
            if active:
                raise PlayError('本话题已有黑白板，请参与原卡')
            if self.points.balance(member['emby_user_id']) < stake:
                raise PlayError(f'发起本局需至少 {stake} 积分可用积分；积分不足，未创建')
            nonce = secrets.token_hex(12)
            values = {'nonce': nonce, 'kind': 'blackwhite', 'bot_id': self.bot_id, 'chat_id': chat,
                      'thread_id': thread, 'command_message_id': mid, 'actor_tg_id': tg,
                      'actor_user_id': member['emby_user_id'], 'actor_name': public_name(message['from']),
                      'stake': stake, 'config_json': encode(cfg), 'secret': secrets.token_hex(32),
                      'created_at': clock, 'expires_at': clock + cfg['lobby_seconds']}
            conn.execute(f"INSERT INTO play_rounds({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
            return self.get(nonce)

    def join(self, nonce, actor, message, choice, *, now=None):
        clock = time.time() if now is None else float(now)
        if choice not in ('black', 'white'):
            raise PlayError('请选择黑板或白板')
        self.expire(nonce, now=clock)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row or row['kind'] != 'blackwhite':
                raise PlayError('游戏不存在')
            self.context(conn, row, message, f'bw:{nonce}:{choice}')
            tg, member = self.actor(actor, now=clock)
            uid = member['emby_user_id']
            prior = conn.execute('SELECT * FROM play_players WHERE nonce=? AND (user_id=? OR tg_id=?)', (nonce, uid, tg)).fetchone()
            if prior:
                if prior['user_id'] != uid or prior['tg_id'] != tg:
                    raise PlayError('账号绑定变化，原选择不会更改')
                if prior['choice'] != choice:
                    raise PlayError('选择已锁定，不能更改')
                return {'already': True, 'row': self.get(nonce)}
            if row['state'] != 'lobby':
                raise PlayError('本局已结束')
            if not self.enabled():
                raise PlayError('黑白板已关闭')
            occupied = conn.execute("SELECT 1 FROM play_players p JOIN play_rounds r ON r.nonce=p.nonce WHERE r.kind='blackwhite' AND r.state='lobby' AND (p.user_id=? OR p.tg_id=?)", (uid, tg)).fetchone()
            if occupied:
                raise PlayError('请先等另一局黑白板结束')
            count = len(self.players(nonce))
            if count >= 5:
                raise PlayError('本局已满员')
            try:
                self.cash.reserve(conn, 'blackwhite', nonce, uid, row['stake'], now=clock)
                self.cash.consume(conn, 'blackwhite', nonce, uid, row['stake'], 'blackwhite', nonce, now=clock)
            except ValueError as exc:
                if '积分不足' in str(exc):
                    raise PlayError(f'参与本局需至少 {row["stake"]} 积分可用积分；积分不足，未参与') from None
                raise
            conn.execute('INSERT INTO play_players(nonce,user_id,tg_id,display_name,choice,joined_at) VALUES(?,?,?,?,?,?)',
                         (nonce, uid, tg, public_name(actor), choice, clock))
            conn.execute('UPDATE play_rounds SET revision=revision+1,next_publish_at=0 WHERE nonce=?', (nonce,))
            if count + 1 == 5:
                self._settle(conn, self.get(nonce), now=clock)
            return {'already': False, 'row': self.get(nonce)}

    def _settle(self, conn, row, *, refund=False, reason='', now=None):
        if row['state'] != 'lobby':
            return
        players = self.players(row['nonce'])
        black = sum(p['choice'] == 'black' for p in players)
        if refund or black in (0, 5):
            awards = {p['user_id']: row['stake'] for p in players}
            mode = reason or 'same'
        else:
            winning = 'black' if black < 5 - black else 'white'
            winners = [p['user_id'] for p in players if p['choice'] == winning]
            awards = fair_split(row['stake'] * 5, winners, row['secret'])
            mode = winning
        for p in players:
            amount = awards.get(p['user_id'], 0)
            if amount:
                self.cash.payout(conn, 'blackwhite', row['nonce'], p['user_id'], amount, now=now)
            conn.execute('UPDATE play_players SET result_amount=? WHERE id=?', (amount, p['id']))
        conn.execute("UPDATE play_rounds SET state=?,result_json=?,revision=revision+1,next_publish_at=0 WHERE nonce=?",
                     ('expired' if reason == 'timeout' else 'cancelled' if reason == 'disabled' else 'settled',
                      encode({'mode': mode, 'participants': len(players), 'pot': row['stake'] * len(players)}), row['nonce']))

    def expire(self, nonce, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if (row and row['kind'] == 'blackwhite' and row['state'] == 'lobby'
                    and (row['expires_at'] <= clock or not self.enabled())):
                self._settle(conn, row, refund=True,
                             reason='timeout' if row['expires_at'] <= clock else 'disabled', now=clock)
            return self.get(nonce)

    def expire_due(self, *, now=None):
        clock = time.time() if now is None else float(now)
        query = "SELECT nonce FROM play_rounds WHERE kind='blackwhite' AND state='lobby'"
        enabled = self.enabled()
        rows = self.db.query(query + (' AND expires_at<=?' if enabled else '') + ' LIMIT 100', (clock,) if enabled else ())
        for row in rows:
            self.expire(row['nonce'], now=clock)
        return len(rows)


class BlackwhitePlugin(Plugin):
    spec = Spec(id='blackwhite', name='黑白板', icon='⚫', category='points',
                description='五人同注，少数胜；同面或组局超时全退。', fields=[
                    Field('default_stake', '默认押注', kind='int', default=10, min=10, max=500),
                    Field('min_stake', '最低押注', kind='int', default=10, min=10, max=500),
                    Field('max_stake', '最高押注', kind='int', default=500, min=10, max=500),
                    Field('lobby_seconds', '组局超时秒数', kind='int', default=600, min=60, max=3600)])

    def validate_config(self, cfg):
        if not cfg['min_stake'] <= cfg['default_stake'] <= cfg['max_stake']:
            raise ValueError('最低押注≤默认押注≤最高押注')

    async def run(self, config):
        if self.ctx.telegram:
            await self.ctx.telegram._blackwhite_tick()
        return self.readonly_status()

    def readonly_status(self):
        db = self.ctx.db
        states = {r['state']: r['n'] for r in db.query("SELECT state,COUNT(*) n FROM play_rounds WHERE kind='blackwhite' GROUP BY state")}
        errors = db.one("SELECT COUNT(*) n FROM play_rounds WHERE kind='blackwhite' AND (publish_error<>'' OR (card_message_id IS NULL AND card_send_state='sending'))")['n']
        report = {'等待中': states.get('lobby', 0), '已结算': states.get('settled', 0),
                  '已全退': states.get('expired', 0) + states.get('cancelled', 0), '群卡待处理': errors}
        issue = db.one("SELECT publish_error FROM play_rounds WHERE kind='blackwhite' AND publish_error<>'' ORDER BY created_at DESC LIMIT 1")
        if issue: report['最近群卡异常'] = issue['publish_error'][:240]
        return report


def results(row):
    return json.loads(row['result_json'])
