"""Persisted three-card poker. Real ledger budgets, no hidden mint or new deal on retry."""
from __future__ import annotations

import json
import secrets
import time

from app.modules.economy_rules import economy_write, encode
from app.modules.play_money import CashBook, PlayError, integer
from app.modules.play_rounds import RoundCards, fair_split
from app.modules.plugins import Field, Plugin, Spec
from app.modules.red_packets import public_name

DEFAULTS = {'default_ante': 10, 'min_ante': 10, 'max_ante': 50, 'budget': 500, 'default_budget': 30,
            'lobby_seconds': 600, 'step_seconds': 60}
CATEGORIES = ('单张', '对子', '顺子', '金花', '顺金', '豹子')


def strength(cards):
    """Cards 0..51: ranks 2..A, four suits; equal ranks never compare suits."""
    if len(cards) != 3 or len(set(cards)) != 3 or any(type(c) is not int or not 0 <= c < 52 for c in cards):
        raise ValueError('invalid three-card hand')
    ranks = sorted((c % 13 + 2 for c in cards), reverse=True)
    flush = len({c // 13 for c in cards}) == 1
    straight = 3 if ranks == [14, 3, 2] else ranks[0] if ranks[0] == ranks[1]+1 == ranks[2]+2 else 0
    if len(set(ranks)) == 1: return (5, ranks[0])
    if straight and flush: return (4, straight)
    if flush: return (3, *ranks)
    if straight: return (2, straight)
    if len(set(ranks)) == 2:
        pair = next(r for r in ranks if ranks.count(r) == 2)
        return (1, pair, next(r for r in ranks if r != pair))
    return (0, *ranks)


class PokerService(RoundCards):
    def __init__(self, db, members, points, config, enabled, allowed, bot_id):
        super().__init__(db, members, allowed, bot_id)
        self.points, self.cash = points, CashBook(points)
        self.config, self.enabled = config, enabled

    def players(self, nonce):
        return self.db.query('SELECT p.*,h.seat,h.cards_json,h.seen,h.folded,h.invested FROM play_players p JOIN poker_hands h ON h.player_id=p.id WHERE p.nonce=? ORDER BY h.seat,p.id', (nonce,))

    def get(self, nonce):
        return self.db.one('SELECT r.*,s.turn,s.current_seat,s.blind,s.start_error FROM play_rounds r JOIN poker_state s ON s.nonce=r.nonce WHERE r.nonce=?', (str(nonce),))

    def _occupied(self, conn, uid, tg):
        return conn.execute("SELECT 1 FROM play_players p JOIN play_rounds r ON r.nonce=p.nonce WHERE r.kind='poker' AND r.state IN ('lobby','running') AND (p.user_id=? OR p.tg_id=?)", (uid, tg)).fetchone()

    def _register(self, conn, row, actor, tg, uid, clock):
        if self._occupied(conn, uid, tg): raise PlayError('请先结束另一局炸金花')
        budget = json.loads(row['config_json'])['budget']
        if self.points.balance(uid) < budget:
            raise PlayError(f'本局每人需 {budget} 积分可用积分以冻结预算；积分不足，未报名')
        cur = conn.execute('INSERT INTO play_players(nonce,user_id,tg_id,display_name,joined_at) VALUES(?,?,?,?,?)',
                           (row['nonce'], uid, tg, public_name(actor), clock))
        conn.execute('INSERT INTO poker_hands(player_id) VALUES(?)', (cur.lastrowid,))

    def create(self, message, ante=None, budget=None, *, now=None):
        clock = time.time() if now is None else float(now)
        self.expire_due(now=clock)
        chat, thread = self.group(message)
        integer(message.get('message_id'), '消息ID')
        with economy_write(self.db) as conn:
            tg, member = self.actor(message.get('from') or {}, now=clock)
            if not self.enabled(): raise PlayError('炸金花暂未开放')
            cfg = {**DEFAULTS, **self.config()}
            ante = cfg['default_ante'] if ante is None else ante
            integer(ante, '底注', cfg['min_ante'], cfg['max_ante'])
            requested_budget = budget
            budget = cfg['default_budget'] if budget is None else budget
            integer(budget, '本局每人预算', ante, cfg['budget'])
            prior = conn.execute("SELECT nonce,actor_user_id,actor_tg_id,thread_id,stake,config_json FROM play_rounds WHERE kind='poker' AND bot_id=? AND chat_id=? AND command_message_id=?", (self.bot_id, chat, message['message_id'])).fetchone()
            if prior:
                if (prior['actor_user_id'] != member['emby_user_id'] or prior['actor_tg_id'] != tg
                        or prior['thread_id'] != thread or prior['stake'] != ante
                        or (requested_budget is not None and json.loads(prior['config_json'])['budget'] != budget)):
                    raise PlayError('原消息已有其他游戏意图')
                return self.get(prior['nonce'])
            if conn.execute("SELECT 1 FROM play_rounds WHERE kind='poker' AND bot_id=? AND chat_id=? AND thread_id=? AND state IN ('lobby','running')", (self.bot_id, chat, thread)).fetchone():
                raise PlayError('本话题已有炸金花，请使用原卡')
            cfg = {**cfg, 'budget_limit': cfg['budget'], 'budget': budget}
            nonce = secrets.token_hex(12)
            values = {'nonce': nonce, 'kind': 'poker', 'bot_id': self.bot_id, 'chat_id': chat,
                      'thread_id': thread, 'command_message_id': message['message_id'], 'actor_tg_id': tg,
                      'actor_user_id': member['emby_user_id'], 'actor_name': public_name(message['from']),
                      'stake': ante, 'config_json': encode(cfg), 'secret': secrets.token_hex(32),
                      'created_at': clock, 'expires_at': clock + cfg['lobby_seconds']}
            conn.execute(f"INSERT INTO play_rounds({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
            conn.execute('INSERT INTO poker_state(nonce,blind) VALUES(?,?)', (nonce, ante))
            self._register(conn, self.get(nonce), message['from'], tg, member['emby_user_id'], clock)
            return self.get(nonce)

    def _start(self, conn, row, clock):
        players = self.players(row['nonce'])
        if len(players) < 2: raise PlayError('至少2人才能开局')
        cfg = json.loads(row['config_json'])
        # All eligibility/budget checks are inside the same SQLite write lock.
        for p in players:
            try:
                _, member = self.actor({'id': int(p['tg_id']), 'is_bot': False}, now=clock)
                valid = member['emby_user_id'] == p['user_id'] and self.points.balance(p['user_id']) >= cfg['budget']
            except PlayError:
                valid = False
            if not valid:
                conn.execute("UPDATE poker_state SET start_error='有成员资格变化或积分不足，尚未开局' WHERE nonce=?", (row['nonce'],))
                self._bump(conn, row['nonce'])
                return False
        deck = list(range(52))
        rng = secrets.SystemRandom()
        rng.shuffle(deck)
        rng.shuffle(players)
        for seat, p in enumerate(players, 1):
            self.cash.reserve(conn, 'poker', row['nonce'], p['user_id'], cfg['budget'], now=clock)
            self.cash.consume(conn, 'poker', row['nonce'], p['user_id'], row['stake'], 'poker', row['nonce'], now=clock)
            conn.execute('UPDATE poker_hands SET seat=?,cards_json=?,invested=? WHERE player_id=?',
                         (seat, encode(deck[(seat-1)*3:seat*3]), row['stake'], p['id']))
        conn.execute("UPDATE poker_state SET turn=1,current_seat=1,start_error='' WHERE nonce=?", (row['nonce'],))
        conn.execute("UPDATE play_rounds SET state='running',expires_at=? WHERE nonce=?", (clock+cfg['step_seconds'], row['nonce']))
        self._bump(conn, row['nonce'])
        if row['stake'] == cfg['budget']: self._settle(conn, self.get(row['nonce']), 'cap', clock)
        return True

    @staticmethod
    def _bump(conn, nonce):
        conn.execute('UPDATE play_rounds SET revision=revision+1,next_publish_at=0 WHERE nonce=?', (nonce,))

    def lobby(self, nonce, actor, message, op, *, now=None):
        clock = time.time() if now is None else float(now)
        self.expire(nonce, now=clock)
        if op not in ('join', 'leave', 'start'): raise PlayError('游戏操作无效')
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row: raise PlayError('游戏不存在')
            self.context(conn, row, message, f'pg:{nonce}:0:{op}')
            tg, member = self.actor(actor, now=clock)
            uid = member['emby_user_id']
            players = self.players(nonce)
            prior = next((p for p in players if p['user_id'] == uid or p['tg_id'] == tg), None)
            if prior and (prior['user_id'] != uid or prior['tg_id'] != tg): raise PlayError('原报名账号绑定已变化')
            if op == 'join' and prior: return row
            if row['state'] != 'lobby': raise PlayError('组局已结束，请使用最新操作')
            if op == 'join':
                if not self.enabled(): raise PlayError('炸金花已关闭')
                if len(players) >= 5: raise PlayError('本局已满员')
                self._register(conn, row, actor, tg, uid, clock)
                if len(players) == 4: self._start(conn, row, clock)
            elif op == 'leave':
                if not prior: return row
                if uid == row['actor_user_id']:
                    conn.execute("UPDATE play_rounds SET state='cancelled',result_json=? WHERE nonce=?", (encode({'mode': 'cancelled'}), nonce))
                else:
                    conn.execute('DELETE FROM poker_hands WHERE player_id=?', (prior['id'],))
                    conn.execute('DELETE FROM play_players WHERE id=?', (prior['id'],))
            else:
                if uid != row['actor_user_id'] or tg != row['actor_tg_id']: raise PlayError('请由发起人开局')
                if not self.enabled(): raise PlayError('炸金花已关闭')
                self._start(conn, row, clock)
            self._bump(conn, nonce)
            return self.get(nonce)

    def _owner(self, nonce, actor, clock):
        tg, member = self.actor(actor, now=clock)
        p = next((p for p in self.players(nonce) if p['tg_id'] == tg and p['user_id'] == member['emby_user_id']), None)
        if not p: raise PlayError('仅本局本人可操作')
        return p

    def look(self, nonce, actor, message=None, data=None, *, now=None):
        clock = time.time() if now is None else float(now)
        self.expire(nonce, now=clock)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row: raise PlayError('游戏不存在')
            if message is not None: self.context(conn, row, message, data)
            p = self._owner(nonce, actor, clock)
            if not p['seen']:
                if row['state'] != 'running' or p['folded']: raise PlayError('当前不可看牌')
                conn.execute('UPDATE poker_hands SET seen=1 WHERE player_id=?', (p['id'],))
                self._bump(conn, nonce)
            if not json.loads(p['cards_json']): raise PlayError('尚未发牌')
            content = [{'name': p['display_name'], 'cards': json.loads(p['cards_json'])}]
            self._photo(conn, row, p['tg_id'], 'private', content, clock, p['user_id'])
            # Failed/unreachable recipients can explicitly retry only their existing cards.
            conn.execute("UPDATE play_photos SET attempts=CASE WHEN state IN ('blocked','failed') THEN 0 ELSE attempts END,state='queued',due_at=0,last_error='' WHERE bot_id=? AND nonce=? AND recipient=? AND mode='private' AND state IN ('blocked','failed','queued')", (self.bot_id, nonce, p['tg_id']))
            return self.get(nonce)

    def action(self, nonce, turn, actor, message, op, target=0, *, now=None):
        clock = time.time() if now is None else float(now)
        self.expire(nonce, now=clock)
        integer(turn, '轮次')
        if op not in ('follow', 'raise', 'double', 'fold', 'compare'): raise PlayError('游戏操作无效')
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row: raise PlayError('游戏不存在')
            data = f'pg:{nonce}:{turn}:{op}' + (f':{target}' if op == 'compare' else '')
            self.context(conn, row, message, data)
            p = self._owner(nonce, actor, clock)
            prior = conn.execute('SELECT * FROM poker_actions WHERE nonce=? AND turn=?', (nonce, turn)).fetchone()
            if prior:
                if prior['player_id'] == p['id'] and prior['action'] == f'{op}:{target}': return row
                raise PlayError('轮次已变化，请使用最新卡片')
            if row['state'] != 'running' or row['turn'] != turn or p['seat'] != row['current_seat'] or p['folded']:
                raise PlayError('尚未轮到你，或轮次已变化')
            alive = [q for q in self.players(nonce) if not q['folded']]
            other = None
            if op == 'compare':
                other = next((q for q in alive if q['seat'] == target and q['id'] != p['id']), None)
                if not other: raise PlayError('请选择仍在局的对手')
            blind = row['blind'] + row['stake'] if op == 'raise' else row['blind'] * 2 if op == 'double' else row['blind']
            cost = 0 if op == 'fold' else blind * (2 if p['seen'] else 1) * (2 if op == 'compare' else 1)
            held = self.cash.held(conn, 'poker', nonce, p['user_id'])
            if cost > held:
                self._record_action(conn, row, p, op, target, 0, clock)
                self._settle(conn, row, 'cap', clock)
                return self.get(nonce)
            if cost:
                self.cash.consume(conn, 'poker', nonce, p['user_id'], cost, 'poker', nonce, now=clock)
                conn.execute('UPDATE poker_hands SET invested=invested+? WHERE player_id=?', (cost, p['id']))
                conn.execute('UPDATE poker_state SET blind=? WHERE nonce=?', (blind, nonce))
            if op == 'fold': conn.execute('UPDATE poker_hands SET folded=1 WHERE player_id=?', (p['id'],))
            if other:
                loser = other if strength(json.loads(p['cards_json'])) > strength(json.loads(other['cards_json'])) else p
                conn.execute('UPDATE poker_hands SET folded=1 WHERE player_id=?', (loser['id'],))
            self._record_action(conn, row, p, op, target, cost, clock)
            survivors = [q for q in self.players(nonce) if not q['folded']]
            if len(survivors) == 1 or (cost and cost == held): self._settle(conn, row, 'last' if len(survivors) == 1 else 'cap', clock)
            else: self._next(conn, row, survivors, clock)
            return self.get(nonce)

    @staticmethod
    def _record_action(conn, row, p, op, target, amount, clock):
        conn.execute('INSERT INTO poker_actions(nonce,turn,player_id,action,amount,created_at) VALUES(?,?,?,?,?,?)',
                     (row['nonce'], row['turn'], p['id'], f'{op}:{target}', amount, clock))

    def _next(self, conn, row, survivors, clock):
        seats = sorted(p['seat'] for p in survivors)
        seat = next((s for s in seats if s > row['current_seat']), seats[0])
        conn.execute('UPDATE poker_state SET current_seat=?,turn=turn+1 WHERE nonce=?', (seat, row['nonce']))
        conn.execute('UPDATE play_rounds SET expires_at=? WHERE nonce=?', (clock+json.loads(row['config_json'])['step_seconds'], row['nonce']))
        self._bump(conn, row['nonce'])

    def _settle(self, conn, row, mode, clock):
        if row['state'] != 'running': return
        players = self.players(row['nonce'])
        alive = [p for p in players if not p['folded']]
        best = max(strength(json.loads(p['cards_json'])) for p in alive)
        winners = [p['user_id'] for p in alive if strength(json.loads(p['cards_json'])) == best]
        fund = conn.execute("SELECT amount FROM play_funds WHERE kind='poker' AND ref=?", (row['nonce'],)).fetchone()
        pot = fund['amount'] if fund else 0
        awards = fair_split(pot, winners, row['secret'])
        for p in players:
            if awards.get(p['user_id']): self.cash.payout(conn, 'poker', row['nonce'], p['user_id'], awards[p['user_id']], now=clock)
            self.cash.release(conn, 'poker', row['nonce'], p['user_id'], now=clock)
            conn.execute('UPDATE play_players SET result_amount=? WHERE id=?', (awards.get(p['user_id'], 0), p['id']))
        conn.execute("UPDATE play_rounds SET state='settled',result_json=? WHERE nonce=?", (encode({'mode': mode, 'pot': pot}), row['nonce']))
        self._bump(conn, row['nonce'])
        final = [{'name': p['display_name'], 'cards': json.loads(p['cards_json']), 'award': awards.get(p['user_id'], 0)} for p in alive]
        self._photo(conn, row, row['chat_id'], 'result', final, clock)

    @staticmethod
    def _photo(conn, row, recipient, mode, content, clock, uid=''):
        conn.execute('INSERT OR IGNORE INTO play_photos(bot_id,nonce,recipient,mode,user_id,thread_id,content_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (row['bot_id'], row['nonce'], str(recipient), mode, uid, row['thread_id'] if mode == 'result' else 0, encode(content), clock))

    def expire(self, nonce, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row: return None
            if row['state'] == 'lobby' and (row['expires_at'] <= clock or not self.enabled()):
                # No funds or cards exist before opening; release defensively for recovery.
                for p in self.players(nonce): self.cash.release(conn, 'poker', nonce, p['user_id'], now=clock)
                conn.execute("UPDATE play_rounds SET state=?,result_json=? WHERE nonce=?", ('expired' if row['expires_at'] <= clock else 'cancelled', encode({'mode': 'timeout' if row['expires_at'] <= clock else 'cancelled'}), nonce))
                self._bump(conn, nonce)
            elif row['state'] == 'running' and row['expires_at'] <= clock:
                current = next(p for p in self.players(nonce) if p['seat'] == row['current_seat'])
                conn.execute('UPDATE poker_hands SET folded=1 WHERE player_id=?', (current['id'],))
                self._record_action(conn, row, current, 'timeout', 0, 0, clock)
                alive = [p for p in self.players(nonce) if not p['folded']]
                if len(alive) == 1: self._settle(conn, row, 'last', clock)
                else: self._next(conn, row, alive, clock)
            return self.get(nonce)

    def expire_due(self, *, now=None):
        clock = time.time() if now is None else float(now)
        rows = self.db.query("SELECT nonce FROM play_rounds WHERE kind='poker' AND (state='running' AND expires_at<=? OR state='lobby' AND (expires_at<=? OR ?=0)) LIMIT 100", (clock, clock, int(self.enabled())))
        for row in rows: self.expire(row['nonce'], now=clock)
        return len(rows)

    def claim_photo(self, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            job = conn.execute("SELECT * FROM play_photos WHERE bot_id=? AND (state='queued' AND due_at<=? OR state='sending' AND lease_until<=?) ORDER BY id LIMIT 1", (self.bot_id, clock, clock)).fetchone()
            if not job: return None
            job = dict(job)
            if job['mode'] == 'private':
                member = self.members.find_by_telegram(job['recipient'])
                if not member or member['emby_user_id'] != job['user_id'] or member.get('state') != 'active' or member.get('emby_missing_since'):
                    conn.execute("UPDATE play_photos SET state='blocked',last_error='原绑定不可用，未发送私密牌图' WHERE id=?", (job['id'],))
                    return {'skip': True}
            token = secrets.token_hex(12)
            conn.execute("UPDATE play_photos SET state='sending',lease_token=?,lease_until=?,attempts=attempts+1 WHERE id=?", (token, clock+90, job['id']))
            return dict(job, lease_token=token, attempts=job['attempts']+1)

    def photo_finished(self, job, mid=None, *, blocked=False, retry_after=0, now=None):
        clock = time.time() if now is None else float(now)
        valid = type(mid) is int and mid > 0
        state = 'sent' if valid else 'blocked' if blocked else 'failed' if job['attempts'] >= 20 else 'queued'
        error = '' if valid else '私聊不可达，请启动Bot后用“看牌”重试原牌' if blocked and job['mode'] == 'private' else '牌图未发送确认，保留原图待重试'
        delay = max(retry_after, min(300, 5*2**min(job['attempts'], 6)))
        self.db.execute('UPDATE play_photos SET state=?,message_id=?,last_error=?,due_at=?,lease_until=0,lease_token=? WHERE id=? AND lease_token=?',
                        (state, mid if valid else None, error, clock+delay, '', job['id'], job['lease_token']))


class PokerPlugin(Plugin):
    spec = Spec(id='poker', name='旧三张牌局收尾', icon='🃏', category='points', hidden=True, description='仅兼容升级前牌局，不能创建新局。', fields=[
        Field('default_ante', '默认底注', kind='int', default=10, min=10, max=50),
        Field('min_ante', '最低底注', kind='int', default=10, min=10, max=50),
        Field('max_ante', '最高底注', kind='int', default=50, min=10, max=50),
        Field('default_budget', '默认每人本局预算', kind='int', default=30, min=10, max=500),
        Field('budget', '允许的最高局预算', kind='int', default=500, min=10, max=500),
        Field('lobby_seconds', '组局超时秒数', kind='int', default=600, min=60, max=3600),
        Field('step_seconds', '操作超时秒数', kind='int', default=60, min=15, max=300)])

    def validate_config(self, cfg):
        if not cfg['min_ante'] <= cfg['default_ante'] <= cfg['max_ante']:
            raise ValueError('最低底注≤默认底注≤最高底注')
        if not cfg['default_ante'] <= cfg['default_budget'] <= cfg['budget']:
            raise ValueError('默认底注≤默认局预算≤允许的最高局预算')

    async def run(self, config):
        if self.ctx.telegram: await self.ctx.telegram._poker_tick()
        return self.readonly_status()

    def readonly_status(self):
        states = {r['state']: r['n'] for r in self.ctx.db.query("SELECT state,COUNT(*) n FROM play_rounds WHERE kind='poker' GROUP BY state")}
        failed = self.ctx.db.one("SELECT COUNT(*) n FROM play_photos WHERE state IN ('blocked','failed') OR last_error<>''")['n']
        report = {'组局': states.get('lobby', 0), '进行中': states.get('running', 0), '已结算': states.get('settled', 0), '牌图待处理': failed}
        issue = self.ctx.db.one("SELECT last_error FROM play_photos WHERE last_error<>'' ORDER BY id DESC LIMIT 1")
        if issue: report['最近牌图异常'] = issue['last_error'][:240]
        group = self.ctx.db.one("SELECT publish_error FROM play_rounds WHERE kind='poker' AND publish_error<>'' ORDER BY created_at DESC LIMIT 1")
        if group: report['最近群卡异常'] = group['publish_error'][:240]
        return report
