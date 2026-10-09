"""New fixed 4N banker collateral and pairwise N matches; legacy snapshots stay legacy."""
from __future__ import annotations

import json
import secrets
import time
from itertools import combinations

from app.modules.economy_rules import economy_write, encode
from app.modules.play_money import CashBook, PlayError, integer
from app.modules.play_rounds import RoundCards
from app.modules.plugins import Field, Plugin, Spec
from app.modules.red_packets import public_name

DEFAULTS = {'default_stake': 10, 'min_stake': 1, 'max_stake': 500, 'lobby_seconds': 600}
CATEGORIES = ('无牛', '牛一', '牛二', '牛三', '牛四', '牛五', '牛六', '牛七', '牛八', '牛九', '牛牛')


def rank(card):
    # Same physical deck as the shared vector renderer: 2..K,A in each suit.
    return 1 if card % 13 == 12 else card % 13 + 2


def strength(cards):
    if len(cards) != 5 or len(set(cards)) != 5 or any(type(c) is not int or not 0 <= c < 52 for c in cards):
        raise ValueError('invalid five-card hand')
    points = [min(rank(c), 10) for c in cards]
    bull = 0
    if any(sum(points[i] for i in triple) % 10 == 0 for triple in combinations(range(5), 3)):
        bull = sum(points) % 10 or 10
    high = max(cards, key=lambda c: (rank(c), 3-c//13))
    return bull, rank(high), 3-high//13


def card_text(card):
    value = rank(card)
    return ('♠', '♥', '♣', '♦')[card//13] + {1:'A', 11:'J', 12:'Q', 13:'K'}.get(value, str(value))


def migrate(db):
    # Reuse only the shared record shape, never old game rows or rule snapshots.
    for old, new in (('play_rounds','niuniu_rounds'), ('play_players','niuniu_players')):
        if db._conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (new,)).fetchone():
            continue
        sql = db._conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (old,)).fetchone()[0]
        sql = sql.replace(old, new).replace('REFERENCES play_rounds', 'REFERENCES niuniu_rounds')
        sql = sql.replace("kind IN ('blackwhite','poker')", "kind='niuniu'")
        db._conn.execute(sql)
    db._ensure_column('niuniu_players', 'cards_json', "TEXT NOT NULL DEFAULT '[]'")
    db._ensure_column('niuniu_players', 'escrow_ref', "TEXT NOT NULL DEFAULT ''")
    db._ensure_column('niuniu_rounds', 'card_format', "TEXT NOT NULL DEFAULT 'text'")
    for key, declaration in (('photo_state', "TEXT NOT NULL DEFAULT 'pending'"), ('photo_lease', 'REAL NOT NULL DEFAULT 0'), ('photo_due', 'REAL NOT NULL DEFAULT 0'), ('photo_attempts', 'INTEGER NOT NULL DEFAULT 0'), ('photo_message_id', 'INTEGER'), ('photo_error', "TEXT NOT NULL DEFAULT ''")):
        db._ensure_column('niuniu_rounds', key, declaration)
    db._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS niuniu_one_active ON niuniu_rounds(bot_id,chat_id,thread_id) WHERE state IN ('lobby','running')")


class NiuniuService(RoundCards):
    rounds_table, players_table = 'niuniu_rounds', 'niuniu_players'

    def __init__(self, db, members, points, config, enabled, allowed, bot_id):
        super().__init__(db, members, allowed, bot_id)
        self.points, self.cash = points, CashBook(points)
        self.config, self.enabled = config, enabled

    @staticmethod
    def _bump(conn, nonce):
        conn.execute('UPDATE niuniu_rounds SET revision=revision+1,next_publish_at=0 WHERE nonce=?', (nonce,))

    def _join(self, conn, row, actor, tg, uid, clock):
        for table, players in (('niuniu_rounds','niuniu_players'), ('play_rounds','play_players')):
            if conn.execute(f"SELECT 1 FROM {players} p JOIN {table} r ON r.nonce=p.nonce WHERE r.state IN ('lobby','running') AND r.kind IN ('poker','niuniu') AND (p.user_id=? OR p.tg_id=?)", (uid,tg)).fetchone():
                raise PlayError('请先结束另一局牌局')
        ref = row['nonce']+':'+secrets.token_hex(8)
        banker = json.loads(row['config_json']).get('game') == 'niuniu-banker-v1' and uid == row['actor_user_id']
        amount = row['stake'] * (4 if banker else 1)
        try:
            self.cash.reserve(conn, 'niuniu', ref, uid, amount, now=clock)
        except ValueError:
            raise PlayError(f'{"坐庄担保" if banker else "加入本局"}需 {amount} 积分，积分不足，未加入') from None
        conn.execute('INSERT INTO niuniu_players(nonce,user_id,tg_id,display_name,joined_at,escrow_ref) VALUES(?,?,?,?,?,?)', (row['nonce'],uid,tg,public_name(actor),clock,ref))

    def create(self, message, stake=None, *, now=None):
        clock = time.time() if now is None else float(now)
        self.expire_due(now=clock)
        chat, thread = self.group(message)
        integer(message.get('message_id'), '消息ID')
        with economy_write(self.db) as conn:
            tg, member = self.actor(message.get('from') or {}, now=clock)
            cfg = {**DEFAULTS, **self.config(), 'game':'niuniu-banker-v1'}
            stake = cfg['default_stake'] if stake is None else stake
            integer(stake,'每人投入',cfg['min_stake'],cfg['max_stake'])
            prior = conn.execute('SELECT * FROM niuniu_rounds WHERE bot_id=? AND chat_id=? AND command_message_id=?', (self.bot_id,chat,message['message_id'])).fetchone()
            if prior:
                if prior['actor_user_id']!=member['emby_user_id'] or prior['actor_tg_id']!=tg or prior['thread_id']!=thread or prior['stake']!=stake:
                    raise PlayError('原消息已创建另一局，请发送新命令')
                return dict(prior)
            if not self.enabled(): raise PlayError('牛牛暂未开放')
            if conn.execute("SELECT 1 FROM niuniu_rounds WHERE bot_id=? AND chat_id=? AND thread_id=? AND state IN ('lobby','running')", (self.bot_id,chat,thread)).fetchone():
                raise PlayError('已有牛牛在等人，请使用原卡')
            if conn.execute("SELECT 1 FROM play_rounds WHERE kind='poker' AND bot_id=? AND chat_id=? AND thread_id=? AND state IN ('lobby','running')", (self.bot_id,chat,thread)).fetchone():
                raise PlayError('原牌局尚未结束，请先安全收尾')
            nonce=secrets.token_hex(12)
            row={'nonce':nonce,'kind':'niuniu','bot_id':self.bot_id,'chat_id':chat,'thread_id':thread,'command_message_id':message['message_id'],'actor_tg_id':tg,'actor_user_id':member['emby_user_id'],'actor_name':public_name(message['from']),'stake':stake,'config_json':encode(cfg),'secret':secrets.token_hex(32),'created_at':clock,'expires_at':clock+cfg['lobby_seconds'],'card_format':'photo','photo_state':'inline'}
            conn.execute(f"INSERT INTO niuniu_rounds({','.join(row)}) VALUES({','.join('?' for _ in row)})",tuple(row.values()))
            self._join(conn,self.get(nonce),message['from'],tg,member['emby_user_id'],clock)
            return self.get(nonce)

    def _refund(self, conn, row, reason, clock):
        if row['state'] not in ('lobby','running'): return
        for p in self.players(row['nonce']):
            amount=self.cash.release(conn,'niuniu',p['escrow_ref'],p['user_id'],now=clock)
            conn.execute('UPDATE niuniu_players SET result_amount=? WHERE id=?',(amount,p['id']))
        conn.execute("UPDATE niuniu_rounds SET state='cancelled',result_json=? WHERE nonce=?",(encode({'mode':'refund','reason':reason}),row['nonce']))
        self._bump(conn,row['nonce'])

    def _finish(self, conn, row, clock):
        players=self.players(row['nonce'])
        if len(players)<2: raise PlayError('至少2人才能开始')
        if len(players)>5: raise ValueError('unsafe player count')
        for p in players:
            try:
                _, member=self.actor({'id':int(p['tg_id']),'is_bot':False},now=clock)
            except PlayError:
                raise ValueError('unsafe membership') from None
            banker_mode = json.loads(row['config_json']).get('game') == 'niuniu-banker-v1'
            required = row['stake'] * (4 if banker_mode and p['user_id'] == row['actor_user_id'] else 1)
            if member['emby_user_id']!=p['user_id'] or self.cash.held(conn,'niuniu',p['escrow_ref'],p['user_id'])!=required:
                raise ValueError('unsafe membership or held share')
        deck=list(range(52));secrets.SystemRandom().shuffle(deck)
        hands=[deck[i*5:(i+1)*5] for i in range(len(players))]
        strengths=[strength(cards) for cards in hands]
        fund='niuniu:'+row['nonce']
        if json.loads(row['config_json']).get('game') == 'niuniu-banker-v1':
            bank_indices = [i for i,p in enumerate(players) if p['user_id'] == row['actor_user_id'] and p['tg_id'] == row['actor_tg_id']]
            if len(bank_indices) != 1:
                raise ValueError('unsafe banker identity')
            bi = bank_indices[0]
            bank = players[bi]
            guests = [(i,p) for i,p in enumerate(players) if i != bi]
            unused = (4-len(guests))*row['stake']
            if unused:
                self.cash.release(conn,'niuniu',bank['escrow_ref'],bank['user_id'],unused,now=clock)
            for i,(p,cards) in enumerate(zip(players,hands,strict=True)):
                held = len(guests)*row['stake'] if i == bi else row['stake']
                self.cash.consume(conn,'niuniu',p['escrow_ref'],p['user_id'],held,'poker',fund,now=clock)
                conn.execute('UPDATE niuniu_players SET cards_json=? WHERE id=?',(encode(cards),p['id']))
            bank_award = 0
            matches = []
            for i,p in guests:
                # Disjoint physical cards and the final rank/suit tie-break give a strict order.
                if strengths[i] == strengths[bi]:
                    raise ValueError('unsafe identical hand strength')
                guest_wins = strengths[i] > strengths[bi]
                winner = p if guest_wins else bank
                self.cash.payout(conn,'poker',fund,winner['user_id'],2*row['stake'],now=clock)
                if guest_wins:
                    conn.execute('UPDATE niuniu_players SET result_amount=? WHERE id=?',(2*row['stake'],p['id']))
                else:
                    bank_award += 2*row['stake']
                matches.append({'guest':p['id'],'winner':winner['id'],'net':row['stake'] if guest_wins else -row['stake']})
            conn.execute('UPDATE niuniu_players SET result_amount=? WHERE id=?',(bank_award+unused,bank['id']))
            result = {'mode':'banker','banker':bank['id'],'stake':row['stake'],'matches':matches,'unused_refund':unused}
        else:
            # Pre-upgrade rules are immutable; old room money is never reinterpreted as collateral.
            winner=players[max(range(len(players)),key=lambda i:strengths[i])]
            pot=row['stake']*len(players)
            for p,cards in zip(players,hands,strict=True):
                self.cash.consume(conn,'niuniu',p['escrow_ref'],p['user_id'],row['stake'],'poker',fund,now=clock)
                conn.execute('UPDATE niuniu_players SET cards_json=? WHERE id=?',(encode(cards),p['id']))
            self.cash.payout(conn,'poker',fund,winner['user_id'],pot,now=clock)
            conn.execute('UPDATE niuniu_players SET result_amount=? WHERE id=?',(pot,winner['id']))
            result = {'mode':'win','pot':pot,'winner':winner['id']}
        conn.execute("UPDATE niuniu_rounds SET state='settled',result_json=? WHERE nonce=?",(encode(result),row['nonce']))
        self._bump(conn,row['nonce'])

    def lobby(self, nonce, actor, message, op, *, now=None):
        clock=time.time() if now is None else float(now)
        if op == 'leave':
            raise PlayError('加入后不能主动退桌；超时或系统异常会退回')
        self.expire(nonce,now=clock)
        if op not in ('join','start'): raise PlayError('牛牛操作无效')
        try:
            with economy_write(self.db) as conn:
                row=self.get(nonce)
                if not row: raise PlayError('这局牛牛不存在')
                self.context(conn,row,message,f'nn:{nonce}:{op}')
                tg,member=self.actor(actor,now=clock);uid=member['emby_user_id']
                players=self.players(nonce)
                prior=next((p for p in players if p['user_id']==uid or p['tg_id']==tg),None)
                if prior and (prior['user_id']!=uid or prior['tg_id']!=tg): raise PlayError('原报名绑定已变化')
                if row['state']!='lobby': return row
                if op=='join':
                    if prior:return row
                    if not self.enabled():raise PlayError('牛牛暂未开放')
                    if len(players)>=5:raise PlayError('这局已满员')
                    self._join(conn,row,actor,tg,uid,clock)
                    if len(players)==4:self._finish(conn,row,clock)
                else:
                    if uid!=row['actor_user_id'] or tg!=row['actor_tg_id']:raise PlayError('请由房主开始')
                    if not self.enabled():raise PlayError('牛牛暂未开放')
                    self._finish(conn,row,clock)
                self._bump(conn,nonce)
                return self.get(nonce)
        except (ValueError, RuntimeError) as exc:
            if isinstance(exc,PlayError): raise
            with economy_write(self.db) as conn:
                self._refund(conn,self.get(nonce),'牌局未能安全开始',clock)
            return self.get(nonce)

    def expire(self, nonce, *, now=None):
        clock=time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row=self.get(nonce)
            if (row and row['state'] in ('lobby','running')
                    and (row['state']=='running' or row['expires_at']<=clock or not self.enabled())):
                self._refund(conn,row,'等待结束' if row['state']=='lobby' else '恢复未完成牌局',clock)
            return self.get(nonce)

    def expire_due(self, *, now=None):
        for row in self.db.query("SELECT nonce FROM niuniu_rounds WHERE state IN ('lobby','running')"):
            self.expire(row['nonce'],now=now)


class NiuniuPlugin(Plugin):
    spec=Spec(id='niuniu',name='牛牛',icon='🐂',category='points',description='创建者坐庄担保4份，最多4闲家各1份，各自对庄固定1倍，无抽成；入座后不退桌。',fields=[Field('default_stake','默认每人投入',kind='int',default=10,min=1,max=500),Field('min_stake','最低每人投入',kind='int',default=1,min=1,max=500),Field('max_stake','最高每人投入',kind='int',default=500,min=1,max=500),Field('lobby_seconds','等人超时秒数',kind='int',default=600,min=60,max=3600)])

    def validate_config(self,cfg):
        if not cfg['min_stake']<=cfg['default_stake']<=cfg['max_stake']:raise ValueError('最低投入≤默认投入≤最高投入')

    def readonly_status(self):
        states={r['state']:r['n'] for r in self.ctx.db.query('SELECT state,COUNT(*) n FROM niuniu_rounds GROUP BY state')}
        return {'等人':states.get('lobby',0),'已揭晓':states.get('settled',0),'已退款':states.get('cancelled',0)}

    async def run(self,config):
        if self.ctx.telegram:await self.ctx.telegram._niuniu_tick()
        return self.readonly_status()
