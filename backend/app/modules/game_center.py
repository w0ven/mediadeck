"""Durable tables and money, with process-local SSE and waiting-seat presence."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import time
from html import escape
from time import monotonic

from app.modules.economy_rules import day_bounds, economy_write, encode
from app.modules.game_center_board import BOARD_GAMES, BoardGame, BoardRobots
from app.modules.game_center_config import DEFAULTS, deep_link, tiers
from app.modules.game_center_holdem import HoldemGame
from app.modules.game_center_zjh import ZjhGame
from app.modules.play_money import CashBook, PlayError, integer
from app.modules.usage import run_usage_io


class GameError(PlayError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


class GameCenter:
    # Long enough to leave the Mini App and post an invitation in a group chat.
    OFFLINE_SECONDS = 180
    def __init__(self, db, members, points, config=dict, enabled=lambda: False,
                 bot=lambda: None):
        self.db, self.members, self.points = db, members, points
        self.cash = CashBook(points)
        self.config, self.enabled, self.bot = config, enabled, bot
        # Adapters own rule/escrow policy. The shell never serializes raw engine state to clients.
        self.games = {'zjh': ZjhGame(), 'holdem': HoldemGame(),
                      **{game: BoardGame(game) for game in BOARD_GAMES}}
        self.board_robots = BoardRobots()
        self._listeners = {}
        self._listener_lock = threading.Lock()
        self._online = {}
        self._presence = {}
        self._started_at = monotonic()

    def cfg(self):
        return {**DEFAULTS, **self.config()}

    def link(self, table_id=None):
        bot = self.bot()
        base = bot._cfg().get('miniapp_url') if bot else ''
        username = str(getattr(bot, '_bot_username', '') or '')
        if base and re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            base = 'https://t.me/' + username
        return deep_link(base, 'gc_' + table_id if table_id else 'games')

    @staticmethod
    def _clock(now):
        return time.time() if now is None else float(now)

    def _load(self, conn, table_id, uid=None):
        row = conn.execute('SELECT state_json FROM gc_tables WHERE id=?', (table_id,)).fetchone()
        if not row:
            raise GameError('not_found', '牌桌不存在', 404)
        table = json.loads(row['state_json'])
        if uid is not None and table['currency'] == 'beans' and uid != table['owner_uid']:
            raise GameError('not_found', '牌桌不存在', 404)
        return table

    @staticmethod
    def seat(table, uid):
        return next((p for p in table['seats'] if p['uid'] == uid and not p['bot']), None)

    def _member(self, uid):
        from app.modules.account_deletion import deletion_in_progress
        if deletion_in_progress(self.db._conn, uid):
            raise GameError('forbidden', '账号注销处理中，不能入座', 403)
        member = self.members.get(uid)
        if (not member or member.get('state') != 'active' or member.get('emby_missing_since')
                or (member.get('activity_lifecycle') or 'ready') != 'ready'):
            raise GameError('forbidden', '仅有效绑定会员可入座', 403)
        return member

    def _gate(self, game):
        if not isinstance(game, str) or game not in self.games:
            raise GameError('not_found', '游戏不存在', 404)
        if (not self.enabled() or not self.cfg()[game + '_enabled']
                or not getattr(self.games[game], 'available', True)):
            raise GameError('disabled', '游戏暂未开放', 503)

    def _beans(self, conn, uid, clock):
        day = day_bounds(clock)[0]
        row = conn.execute('SELECT * FROM gc_beans WHERE user_id=?', (uid,)).fetchone()
        if row and row['day'] >= day:
            return dict(row)
        amount = self.cfg()['daily_beans']
        conn.execute('INSERT INTO gc_beans VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                     'balance=CASE WHEN day<excluded.day THEN MAX(balance,excluded.balance) ELSE balance END,day=MAX(day,excluded.day)',
                     (uid, amount, day))
        return dict(conn.execute('SELECT * FROM gc_beans WHERE user_id=?', (uid,)).fetchone())

    def _wallet(self, conn, uid, clock):
        beans = self._beans(conn, uid, clock)
        held = conn.execute("SELECT COALESCE(SUM(amount),0) n FROM play_escrows "
                            "WHERE user_id=? AND amount>0 AND scope IN ('gc_zjh','gc_holdem','gc_xiangqi','gc_gomoku')",
                            (uid,)).fetchone()['n']
        return {'points': self.points.balance(uid), 'frozen': held, 'beans': beans['balance'],
                'beans_day': beans['day'], 'daily_beans': self.cfg()['daily_beans']}

    def wallet(self, uid, now=None):
        with economy_write(self.db) as conn:
            return self._wallet(conn, uid, self._clock(now))

    def available(self, conn, table, player, clock):
        if player['bot']:
            return max(10000, table['rules'].get('cap') or 0)
        if table['currency'] == 'beans':
            return self._beans(conn, player['uid'], clock)['balance']
        return self.points.balance(player['uid'])

    def can_fund(self, conn, table, player, clock):
        if not player['bot']:
            try:
                self._member(player['uid'])
            except GameError:
                return False
        return self.available(conn, table, player, clock) >= table['rules']['min_balance']

    def active_tables(self, uid):
        return self.db.query('SELECT t.id table_id,t.game,t.tier,t.currency FROM gc_seats s '
                             'JOIN gc_tables t ON t.id=s.table_id WHERE s.user_id=?', (uid,))

    def me(self, uid, now=None):
        clock = self._clock(now)
        with economy_write(self.db) as conn:
            return {'active_tables': self.active_tables(uid), 'wallet': self._wallet(conn, uid, clock),
                    'server_time': clock}

    def lobby(self, uid, now=None):
        result = self.me(uid, now)
        cfg = self.cfg()
        counts = {r['game']: r['n'] for r in self.db.query('SELECT t.game,COUNT(*) n FROM gc_seats s JOIN gc_tables t ON t.id=s.table_id GROUP BY t.game')}
        names = [('zjh', '炸金花', 5), ('holdem', '德州扑克', 6), ('niuniu', '牛牛', 0),
                 ('doudizhu', '斗地主', 0), ('gomoku', '五子棋', 2), ('xiangqi', '象棋', 2)]
        result.update(enabled=bool(self.enabled()), games=[
            {'id': game, 'name': name, 'enabled': bool(self.enabled() and cfg.get(game + '_enabled')),
             'available': game in self.games and getattr(self.games[game], 'available', True),
             'coming_soon': maximum == 0, 'min_players': 2 if maximum else 0,
             'max_players': maximum, 'seated': counts.get(game, 0)} for game, name, maximum in names])
        return result

    def list_tiers(self, game):
        try:
            result = tiers(self.cfg(), game)
        except ValueError:
            raise GameError('not_found', '游戏不存在', 404) from None
        with self.db.snapshot():
            tables = [json.loads(r['state_json']) for r in self.db.query("SELECT state_json FROM gc_tables WHERE game=? AND phase<>'closed'", (game,))]
        for tier in result:
            same = [t for t in tables if t['tier'] == tier['id']]
            tier['seated'] = sum(sum(not p['bot'] for p in t['seats']) for t in same)
            tier['waiting_tables'] = [self._summary(t) for t in same if t['currency'] == 'points' and t['phase'] == 'waiting']
        return {'game': game, 'tiers': result}

    def _summary(self, table):
        host = next((p['name'] for p in table['seats'] if p['player'] == table['host']), '')
        summary = {'table_id': table['id'], 'game': table['game'], 'tier': table['tier'],
                   'host_name': host, 'players': len(table['seats']), 'max_players': table['max_players'],
                   'phase': table['phase'], 'deep_link': self.link(table['id'])}
        if table['game'] == 'holdem':
            summary['start_status'] = self.games['holdem'].start_status(table)
        return summary

    def _join(self, conn, table, uid, name, body, clock):
        if self.seat(table, uid):
            return
        self._gate(table['game'])
        self._member(uid)
        if table['phase'] == 'closed':
            raise GameError('wrong_phase', '牌桌已经散桌', 409)
        if conn.execute('SELECT 1 FROM gc_seats WHERE user_id=? AND currency=?', (uid, table['currency'])).fetchone():
            raise GameError('already_seated', '请先离开另一张牌桌', 409)
        taken = {p['seat'] for p in table['seats']}
        free = [i for i in range(table['max_players']) if i not in taken]
        if not free:
            raise GameError('table_full', '牌桌已满', 409)
        seat = body.get('seat', free[0])
        if type(seat) is not int or seat not in free:
            raise GameError('invalid', '座位不可用')
        if table['game'] != 'holdem' and body.get('buy_in') is not None:
            raise GameError('invalid', '炸金花入座不带入筹码' if table['game'] == 'zjh' else '棋类入座不带入筹码')
        p = {'uid': uid, 'player': 'p_' + secrets.token_hex(8), 'name': str(name or '会员')[:40],
             'seat': seat, 'bot': False, 'ready': False, 'auto': False, 'auto_hands': 0,
             'away': False, 'leave_after': False, 'joined_at': clock, 'notice': ''}
        if table['game'] == 'holdem':
            self.games['holdem'].buy_in(self, conn, table, p, body, clock)
        table['seats'].append(p)
        table['seats'].sort(key=lambda p: p['seat'])
        if table['game'] != 'holdem':
            table['countdown_at'] = None
        conn.execute('INSERT INTO gc_seats VALUES(?,?,?)', (uid, table['currency'], table['id']))
        if table['host'] is None:
            table.update(host=p['player'], host_active=clock)

    def _create(self, conn, uid, name, body, clock):
        game, tier_id = body.get('game'), body.get('tier')
        self._gate(game)
        tier = next((t for t in tiers(self.cfg(), game) if t['id'] == tier_id), None)
        if not tier:
            raise GameError('invalid', '场次不存在')
        table = {'id': secrets.token_hex(12), 'game': game, 'tier': tier_id,
                 'tier_name': tier['name'], 'currency': tier['currency'], 'phase': 'waiting',
                 'revision': 0, 'hand_no': 0, 'host': None, 'max_players': self.games[game].max_players,
                 'created_at': clock, 'last_activity': clock, 'host_active': clock,
                 'countdown_at': None, 'turn_deadline': None, 'auto_at': None,
                 'idle_deadline': clock + 600, 'owner_uid': uid, 'seats': [], 'hand': None,
                 'last_hand': None, 'rules': self.games[game].rules(tier, self.cfg())}
        if game in BOARD_GAMES and tier_id == 'practice':
            table['rules']['robot_level'] = body.get('robot_level', 'normal')
        conn.execute('INSERT INTO gc_tables VALUES(?,?,?,?,?,?,?,?,?)',
                     (table['id'], game, tier_id, tier['currency'], 'waiting', 0, encode(table), clock, clock))
        self._join(conn, table, uid, name, body, clock)
        if tier['currency'] == 'beans':
            positions = [i for i in range(table['max_players']) if i != table['seats'][0]['seat']]
            for i, label in zip(positions, ('小智', '阿呆', '铁柱', '豆豆', '阿星')):
                table['seats'].append({'uid': 'bot:' + str(i), 'player': 'bot_' + str(i), 'seat': i,
                    'name': label, 'bot': True, 'ready': True, 'auto': game not in BOARD_GAMES, 'auto_hands': 0,
                    'away': False, 'leave_after': False, 'joined_at': clock, 'notice': '',
                    **({'stack': 2000, 'auto_since': None, 'away_since': None} if game == 'holdem' else {})})
        else:
            self._new_cards(conn, table)
        return table

    def _transfer_host(self, table, clock):
        candidates = [p for p in table['seats'] if not p['bot'] and p['player'] != table['host'] and not p['leave_after']]
        if candidates:
            table['host'] = min(candidates, key=lambda p: (p['joined_at'], p['seat']))['player']
        elif not any(p['player'] == table['host'] and not p['leave_after'] for p in table['seats']):
            table['host'] = None
        table['host_active'] = clock

    def _remove(self, conn, table, player, clock):
        if table['game'] == 'holdem':
            self.games['holdem'].release(self, conn, table, player, clock)
        table['seats'].remove(player)
        if not player['bot']:
            conn.execute('DELETE FROM gc_seats WHERE user_id=? AND table_id=?', (player['uid'], table['id']))
        if table['host'] == player['player']:
            self._transfer_host(table, clock)
        if table['game'] != 'holdem':
            table['countdown_at'] = None
        if not any(not p['bot'] for p in table['seats']):
            table.update(phase='closed', seats=[], host=None, countdown_at=None, idle_deadline=None)

    def _countdown(self, table, clock):
        if table['game'] == 'holdem':
            return
        if table['phase'] == 'waiting' and len(table['seats']) >= 2 and all(p['ready'] for p in table['seats']):
            table['countdown_at'] = table['countdown_at'] or clock + 5
        else:
            table['countdown_at'] = None

    def arm_auto(self, table, clock):
        current = self.games[table['game']].engine.current_player(table['hand']['engine'])
        p = next(p for p in table['seats'] if p['player'] == current)
        table['auto_at'] = min(clock + 1.5, table['turn_deadline']) if p['auto'] or p['bot'] else None

    def arm_turn(self, table, clock):
        table['turn_deadline'] = clock + table['rules']['step_seconds']
        self.arm_auto(table, clock)

    def finish_hand(self, conn, table, clock):
        hand = table['hand']
        table.update(phase='waiting', last_hand=hand, hand=None, turn_deadline=None, auto_at=None,
                     countdown_at=None, idle_deadline=clock + 600, last_activity=clock, host_active=clock)
        for p in list(table['seats']):
            if p['player'] in hand['users'] and p['auto'] and not p['bot']:
                p['auto_hands'] += 1
            if p['leave_after'] or not p['bot'] and p['auto_hands'] >= table['rules'].get('auto_hands', float('inf')):
                self._remove(conn, table, p, clock)
            else:
                p['ready'] = bool((p['auto'] or p['bot']) and self.can_fund(conn, table, p, clock))
        self._countdown(table, clock)

    def _save(self, conn, table, clock):
        table['revision'] += 1
        conn.execute('UPDATE gc_tables SET phase=?,revision=?,state_json=?,updated_at=? WHERE id=?',
                     (table['phase'], table['revision'], encode(table), clock, table['id']))
        for hand in (table['hand'], table['last_hand']):
            if hand:
                conn.execute('INSERT INTO gc_hands VALUES(?,?,?,?,?) ON CONFLICT(table_id,hand_no) '
                             'DO UPDATE SET state_json=excluded.state_json,settled_at=excluded.settled_at '
                             'WHERE gc_hands.settled_at IS NULL AND gc_hands.state_json<>excluded.state_json',
                             (table['id'], hand['number'], encode(hand), hand['created_at'], hand['settled_at']))
        payload = encode(self._card(table))
        desired = 'delete' if table['hand_no'] or table['phase'] == 'closed' else 'live'
        conn.execute('UPDATE gc_outbox SET payload_json=?,revision=?,desired=? WHERE table_id=? '
                     "AND state NOT IN ('deleted','unknown') AND (payload_json<>? OR desired<>?)",
                     (payload, table['revision'], desired, table['id'], payload, desired))

    def _card(self, table):
        s = self._summary(table)
        names = {'zjh': '炸金花', 'holdem': '德州扑克', 'xiangqi': '象棋', 'gomoku': '五子棋'}
        unit = '欢乐豆' if table['currency'] == 'beans' else '积分'
        stake = (f"每人押 {table['rules']['stake']} {unit}" if table['game'] in BOARD_GAMES else
                 f"底注 {table['rules']['ante']}" if table['game'] == 'zjh' else
                 f"盲注 {table['rules']['small_blind']}/{table['rules']['big_blind']}")
        return {'text': (f"🎮 <b>{names[table['game']]} · {escape(table['tier_name'])}</b>\n"
                         f"{stake} · 房主 {escape(s['host_name'])}\n"
                         f"{s['players']}/{s['max_players']} 人 · 等你上桌"),
                'reply_markup': {'inline_keyboard': [[{'text': '上桌', 'url': self.link(table['id'])}]]}}

    def _new_cards(self, conn, table):
        cfg, bot = self.cfg(), self.bot()
        if not cfg['broadcast_enabled'] or not bot or not self.link(table['id']):
            return
        bot_id = str(bot._token()).split(':')[0]
        if not bot_id.isdigit():
            return
        username_allowlist = any(str(item).startswith('@') for item in getattr(bot, '_group_allowlist', list)())
        for group in cfg['broadcast_groups'].split():
            # Username allowlists require async getChat in the delivery worker; no
            # network I/O may occur while holding the economy transaction.
            if bot._group_chat_allowed({'id': group, 'type': 'supergroup'}) or username_allowlist:
                conn.execute('INSERT INTO gc_outbox(table_id,chat_id,bot_id,payload_json) VALUES(?,?,?,?)',
                             (table['id'], group, bot_id, encode(self._card(table))))

    def mutate(self, uid, name, op, body, table_id=None, now=None):
        uid = str(uid)
        clock = self._clock(now)
        if table_id:
            with self.db.snapshot():
                self._seen(self._load(self.db._conn, table_id, uid), uid)
        if not isinstance(body, dict):
            raise GameError('invalid', '请求须为 JSON 对象')
        key = body.get('request_id')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', key):
            raise GameError('invalid', '缺少有效 request_id')
        intent = encode([op, table_id, body])
        # Deadline processing commits independently, even if the incoming stale action fails.
        self.tick(now=clock, table_id=table_id)
        with economy_write(self.db) as conn:
            old = conn.execute('SELECT * FROM gc_requests WHERE user_id=? AND request_id=?', (uid, key)).fetchone()
            if old:
                if old['request_json'] != intent:
                    raise GameError('idempotency_conflict', '请求ID已用于其他操作', 409)
                outcome = json.loads(old['result_json'])
                table = self._load(conn, outcome['id'], uid)
            else:
                outcome = {}
                if op in ('create', 'quick'):
                    if body.get('game') in BOARD_GAMES:
                        level = body.get('robot_level', 'normal')
                        if (level not in ('easy', 'normal', 'hard')
                                or body.get('tier') != 'practice' and 'robot_level' in body):
                            raise GameError('invalid', '练习场机器人难度须为 easy、normal 或 hard')
                    table = None
                    if op == 'quick':
                        self._gate(body.get('game'))
                        for prior in self.active_tables(uid):
                            if prior['game'] == body.get('game') and prior['tier'] == body.get('tier'):
                                table = self._load(conn, prior['table_id'], uid)
                                break
                        if table is None and body.get('tier') != 'practice':
                            for row in conn.execute("SELECT id FROM gc_tables WHERE game=? AND tier=? AND "
                                                    "(phase='waiting' OR game='holdem' AND phase IN ('between','playing')) "
                                                    "ORDER BY (phase<>'waiting'),created_at,id", (body.get('game'), body.get('tier'))):
                                candidate = self._load(conn, row['id'])
                                if len(candidate['seats']) < candidate['max_players']:
                                    table = candidate
                                    self._join(conn, table, uid, name, body, clock)
                                    break
                    outcome['created'] = table is None
                    if table is None:
                        table = self._create(conn, uid, name, body, clock)
                else:
                    table = self._load(conn, table_id, uid)
                    self._operation(conn, table, uid, name, op, body, clock)
                table['last_activity'] = clock
                if table['phase'] == 'waiting':
                    table['idle_deadline'] = clock + 600
                player = self.seat(table, uid)
                if player and player['player'] == table['host']:
                    table['host_active'] = clock
                if table['game'] == 'holdem':
                    self.games['holdem'].schedule(self, conn, table, clock)
                self._save(conn, table, clock)
                outcome['id'] = table['id']
                conn.execute('INSERT INTO gc_requests VALUES(?,?,?,?,?)', (uid, key, intent, encode(outcome), clock))
            response = {'table': self._view(conn, table, uid, clock)}
            if 'created' in outcome:
                response['created'] = outcome['created']
        if table['game'] in BOARD_GAMES:
            self.board_robots.schedule(self.games[table['game']], table)
        self.notify(table['id'])
        return response

    def _operation(self, conn, table, uid, name, op, body, clock):
        if op == 'seat':
            self._join(conn, table, uid, name, body, clock)
            return
        p = self.seat(table, uid)
        if not p:
            raise GameError('forbidden', '请先入座', 403)
        if op == 'leave':
            if not self.games[table['game']].leave(self, conn, table, p, clock):
                self._remove(conn, table, p, clock)
                self._countdown(table, clock)
            elif p['leave_after'] and table['host'] == p['player']:
                self._transfer_host(table, clock)
        elif op == 'ready':
            if table['game'] == 'holdem':
                raise GameError('invalid', '德州无需准备，带入筹码后按开局规则参与')
            if table['phase'] != 'waiting':
                raise GameError('wrong_phase', '本手结束后才能准备', 409)
            ready = body.get('ready')
            if type(ready) is not bool:
                raise GameError('invalid', 'ready 须为布尔值')
            if ready and not self.can_fund(conn, table, p, clock):
                raise GameError('insufficient_funds', self.games[table['game']].funding_message(table))
            p['ready'] = ready
            if ready:
                p['notice'] = ''
            self._countdown(table, clock)
        elif op == 'start':
            if p['player'] != table['host']:
                raise GameError('forbidden', '只有房主可以开始', 403)
            if table['phase'] != 'waiting':
                raise GameError('wrong_phase', '牌局已经开始', 409)
            self.games[table['game']].start(self, conn, table, clock, manual=True)
        elif op == 'auto':
            if table['game'] in BOARD_GAMES:
                raise GameError('invalid', '棋类不提供托管，超时将判负')
            enabled = body.get('enabled')
            if type(enabled) is not bool:
                raise GameError('invalid', 'enabled 须为布尔值')
            if table['game'] == 'holdem' and p['leave_after']:
                raise GameError('wrong_phase', '已申请离桌，等待本手结算', 409)
            if p['auto'] != enabled:
                p['auto'] = enabled
                if table['game'] == 'holdem':
                    p['auto_since'] = clock if enabled else None
                if not enabled:
                    p.update(auto_hands=0, notice='')
                if table['game'] == 'zjh' and table['phase'] == 'waiting' and enabled:
                    p['ready'] = self.can_fund(conn, table, p, clock)
                    self._countdown(table, clock)
                if table['hand'] and self.games[table['game']].engine.current_player(table['hand']['engine']) == p['player']:
                    self.arm_auto(table, clock)
        elif op == 'actions':
            hand = table['hand']
            if (not hand or type(body.get('hand_no')) is not int or type(body.get('turn')) is not int
                    or body['hand_no'] != hand['number'] or body['turn'] != hand['turn']):
                raise GameError('stale_turn', '牌局已变化，请刷新后操作', 409)
            self.games[table['game']].action(self, conn, table, p, body, clock)
        elif op in ('away', 'top-up'):
            if table['game'] != 'holdem':
                raise GameError('invalid', '此操作仅支持德州扑克')
            self.games['holdem'].operation(self, conn, table, p, op, body, clock)
        else:
            raise GameError('not_found', '操作不存在', 404)

    def _seen(self, table, uid):
        if self.seat(table, uid):
            with self._listener_lock:
                self._presence.setdefault(table['id'], {})[uid] = monotonic()

    def heartbeat(self, table_id, uid):
        with self._listener_lock:
            seen = self._presence.get(table_id, {})
            if uid in seen:
                seen[uid] = monotonic()

    def board_online(self, table, player):
        if player['bot']:
            return True
        with self._listener_lock:
            return bool(self._online.get((table['id'], player['uid'])) or
                        monotonic() - self._presence.get(table['id'], {}).get(
                            player['uid'], self._started_at) < 30)

    def _remove_offline(self, conn, table, clock):
        # The economy lock serializes HTTP activity; the listener lock covers SSE admission.
        with self._listener_lock:
            seated = {p['uid'] for p in table['seats'] if not p['bot']}
            seen = self._presence.get(table['id'], {})
            for uid in list(seen):
                if uid not in seated:
                    del seen[uid]
            if table['phase'] != 'waiting':
                return False
            now = monotonic()
            offline = [p for p in table['seats'] if not p['bot']
                       and not self._online.get((table['id'], p['uid']))
                       and now - seen.get(p['uid'], self._started_at) >= self.OFFLINE_SECONDS]
            if not offline:
                return False
            for p in offline:
                self._operation(conn, table, p['uid'], p['name'], 'leave', {}, clock)
            table['last_activity'] = clock
            if table['phase'] == 'waiting':
                table['idle_deadline'] = clock + 600
            if table['game'] == 'holdem':
                self.games['holdem'].schedule(self, conn, table, clock)
            self._save(conn, table, clock)
            return True

    def tick(self, now=None, table_id=None):
        clock = self._clock(now)
        query = "SELECT id FROM gc_tables WHERE phase<>'closed'"
        with self.db.snapshot():
            rows = self.db.query(query + (' AND id=?' if table_id else ''), (table_id,) if table_id else ())
            live = {row['id'] for row in rows}
            with self._listener_lock:
                for tid in list(self._presence):
                    if (table_id is None or tid == table_id) and tid not in live:
                        del self._presence[tid]
        changed = []
        for row in rows:
            with economy_write(self.db) as conn:
                table = self._load(conn, row['id'])
                dirty = self._remove_offline(conn, table, clock)
                # Catch up from original deadlines, not restart time. Bounded per table for fairness.
                for _ in range(256):
                    game = self.games[table['game']]
                    if table['game'] in BOARD_GAMES and table['phase'] == 'playing':
                        if game.tick(self, conn, table, clock):
                            dirty = True
                            self._save(conn, table, clock)
                        break
                    candidates = []
                    if table['phase'] == 'playing':
                        candidates.append((table['auto_at'] if table['auto_at'] is not None else table['turn_deadline'], 'turn'))
                    elif table['phase'] in ('waiting', 'between'):
                        if table['idle_deadline'] is not None:
                            candidates.append((table['idle_deadline'], 'idle'))
                        if table['countdown_at'] is not None:
                            candidates.append((table['countdown_at'], 'start'))
                        if table['phase'] == 'waiting' and sum(not p['bot'] and not p['leave_after'] for p in table['seats']) > 1:
                            candidates.append((table['host_active'] + 180, 'host'))
                    else:
                        break
                    if table['game'] == 'holdem':
                        candidates.extend(game.deadlines(table))
                    if not candidates:
                        break
                    due, event = min(candidates)
                    if due > clock:
                        break
                    if event == 'turn':
                        current = game.engine.current_player(table['hand']['engine'])
                        p = next(p for p in table['seats'] if p['player'] == current)
                        if not p['auto']:
                            p.update(auto=True, notice='已超时，进入托管；点「取消托管」接管')
                            if table['game'] == 'holdem':
                                p['auto_since'] = due
                        body = (game.auto_body(table, p) if table['game'] == 'holdem' else
                                {'action': game.engine.auto_action(table['hand']['engine'], current)})
                        game.action(self, conn, table, p, body, due, automatic=True)
                    elif event == 'idle':
                        if table['game'] == 'holdem':
                            for p in list(table['seats']):
                                if not p['bot']:
                                    self._remove(conn, table, p, due)
                        else:
                            conn.execute('DELETE FROM gc_seats WHERE table_id=?', (table['id'],))
                            table.update(phase='closed', seats=[], host=None, countdown_at=None, idle_deadline=None)
                    elif event == 'host':
                        self._transfer_host(table, due)
                    elif event == 'start':
                        game.start(self, conn, table, due)
                    else:
                        game.timer(self, conn, table, event, due)
                    if table['game'] == 'holdem':
                        game.schedule(self, conn, table, due)
                    dirty = True
                    self._save(conn, table, due)
                if dirty:
                    changed.append(table['id'])
            if table['game'] in BOARD_GAMES:
                self.board_robots.schedule(self.games[table['game']], table)
        for tid in changed:
            self.notify(tid)
        return changed

    def _zjh_start_status(self, conn, table, viewer, clock):
        seats = [p for p in table['seats'] if not p['leave_after']]
        eligible = []
        if table['phase'] == 'waiting':
            eligible = self.games[table['game']].eligible(self, conn, table, clock, manual=True)
        if table['phase'] == 'closed':
            reason, message = 'closed', '本桌已结束，未使用的冻结额已退回'
        elif table['phase'] == 'playing':
            reason, message = 'playing', '本手进行中，新入座玩家从下一手参与'
        elif len(seats) < 2:
            reason, message = 'waiting_players', f'还差 {2 - len(seats)} 人入座才能开局'
        elif len(eligible) < 2:
            if sum(p['ready'] or p['player'] == table['host'] for p in seats) < 2:
                reason, message = 'waiting_ready', '等待其他玩家点准备；房主点开始时自动算准备'
            else:
                reason, message = 'ineligible_players', '已准备且可用余额够本场的玩家至少需要 2 人'
        elif not viewer or viewer['player'] != table['host']:
            reason, message = 'waiting_host', '人数够了，等待房主开始'
        else:
            reason, message = 'ready', '人数够了，点「开始」就能开局；未准备的人本手旁观'
        if table['game'] in BOARD_GAMES:
            host = bool(viewer) and viewer['player'] == table['host']
            message = {
                'waiting_players': '等待对手入座，可以把桌链接分享给朋友',
                'waiting_ready': '等待对手点准备' if host or not viewer else '点「准备」后，房主就能开局',
                'ineligible_players': '双方的可用余额都要够本场押注才能开局',
                'waiting_host': '你已准备，等待房主开始' if viewer else '双方已准备，等待房主开始',
                'ready': '对手已准备，点「开始」即可开局',
                'playing': '对局进行中，超时判负，不提供托管',
            }.get(reason, message)
        return {'min_players': 2, 'seated_players': len(seats), 'eligible_players': len(eligible),
                'can_start': reason == 'ready', 'reason': reason, 'message': message}

    def _view(self, conn, table, uid, clock):
        from app.modules.miniapp import avatar_url
        self._seen(table, uid)
        users = [p['uid'] for p in table['seats'] if not p['bot']]
        avatars = {}
        if users:
            marks = ','.join('?' for _ in users)
            avatars = {row['emby_user_id']: avatar_url(row['tg_user_id']) for row in conn.execute(
                f'SELECT emby_user_id,tg_user_id FROM members WHERE emby_user_id IN ({marks})', users)}
        p = self.seat(table, uid)
        out = {k: table[k] for k in ('id', 'game', 'tier', 'tier_name', 'currency', 'phase', 'revision',
               'hand_no', 'host', 'max_players', 'created_at', 'last_activity', 'countdown_at',
               'turn_deadline', 'auto_at', 'idle_deadline', 'rules')}
        out.update(server_time=clock, deep_link=self.link(table['id']),
                   seats=[{**{k: seat[k] for k in ('seat', 'player', 'name', 'bot', 'ready', 'auto', 'auto_hands',
                                                 'away', 'leave_after')},
                           'avatar': avatars.get(seat['uid'], '')} for seat in table['seats']],
                   me={'player': p['player'] if p else None, 'seat': p['seat'] if p else None,
                       'is_host': bool(p and table['host'] == p['player']), 'notice': p['notice'] if p else '',
                       'wallet': self._wallet(conn, uid, clock)})
        if table['game'] == 'holdem':
            out['button'] = table.get('button')
            out['start_status'] = self.games['holdem'].start_status(table)
            for public, seat in zip(out['seats'], table['seats']):
                public.update({k: seat[k] for k in ('stack', 'away_since', 'auto_since')})
        else:
            out['start_status'] = self._zjh_start_status(conn, table, p, clock)
        if table['game'] in BOARD_GAMES:
            for public, seat in zip(out['seats'], table['seats']):
                public['color'] = seat.get('color')
        for key in ('hand', 'last_hand'):
            hand = table[key]
            # A re-seated/departed member keeps their own per-hand identity, not a new seat occupant's.
            player = next((pid for pid, who in hand['users'].items() if who['uid'] == uid), None) if hand else None
            if table['game'] in BOARD_GAMES:
                out[key] = self.games[table['game']].view(hand, player, clock) if hand else None
            else:
                out[key] = self.games[table['game']].view(hand, player) if hand else None
        return out

    def snapshot(self, table_id, uid, now=None):
        clock = self._clock(now)
        with economy_write(self.db) as conn:
            table = self._load(conn, table_id, uid)
            return {'table': self._view(conn, table, uid, clock)}

    def proof(self, table_id, number):
        # Administrator-only: the revealed deck lets anyone rebuild folded and compared hands.
        with self.db.snapshot():
            table = self._load(self.db._conn, table_id)
            if table['game'] in BOARD_GAMES:
                raise GameError('invalid', '棋类没有洗牌复核信息')
            row = self.db.one('SELECT state_json FROM gc_hands WHERE table_id=? AND hand_no=?', (table_id, number))
        if not row:
            raise GameError('not_found', '手牌不存在', 404)
        hand = json.loads(row['state_json'])
        out = {'table_id': table_id, 'hand_no': number, 'commitment': hand['commitment'],
               'settled': hand['settled_at'] is not None}
        if out['settled']:
            out.update({k: hand[k] for k in ('deck', 'deck_codes', 'salt', 'deal_order')})
            out['result'] = hand['engine']['result']
        return out

    def cash_detail(self, table_id='', user_id='', limit=100):
        integer(limit, '条数', 1, 500)
        if not table_id and not user_id:
            raise GameError('invalid', '至少提供牌桌或用户条件')
        sql = "SELECT * FROM play_cash_events WHERE (scope IN ('gc_zjh','gc_holdem','gc_xiangqi','gc_gomoku') OR scope='poker' AND ref IN (SELECT table_id||':'||hand_no FROM gc_hands))"
        args = []
        if table_id:
            sql += " AND (ref=? OR substr(ref,1,?)=?)"
            args += [table_id, len(table_id) + 1, table_id + ':']
        if user_id:
            sql += ' AND user_id=?'
            args += [user_id]
        return {'events': self.db.query(sql + ' ORDER BY id DESC LIMIT ?', (*args, limit))}

    def subscribe(self, table_id, uid=None):
        event = asyncio.Event()
        subscriber = (asyncio.get_running_loop(), event)
        with self._listener_lock:
            self._listeners.setdefault(table_id, set()).add(subscriber)
            if uid is not None:
                self._online.setdefault((table_id, uid), set()).add(subscriber)
        return subscriber

    def unsubscribe(self, table_id, subscriber, uid=None):
        with self._listener_lock:
            listeners = self._listeners.get(table_id, set())
            listeners.discard(subscriber)
            if not listeners:
                self._listeners.pop(table_id, None)
            key = (table_id, uid)
            online = self._online.get(key, set())
            if subscriber in online:
                online.remove(subscriber)
                seen = self._presence.get(table_id, {})
                if uid in seen:
                    seen[uid] = monotonic()
                if not online:
                    del self._online[key]

    def notify(self, table_id):
        with self._listener_lock:
            listeners = list(self._listeners.get(table_id, ()))
        for loop, event in listeners:
            if not loop.is_closed():
                loop.call_soon_threadsafe(event.set)

    async def run(self):
        while True:
            try:
                await run_usage_io(self.tick)
            except Exception:
                logging.getLogger(__name__).exception('Game-center timer interrupted')
            await asyncio.sleep(0.5)
