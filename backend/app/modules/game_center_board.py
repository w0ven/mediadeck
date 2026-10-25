"""Two-player board adapter: durable clocks, public positions and atomic escrow."""
from __future__ import annotations

import copy
import importlib
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.modules import gomoku_engine, gomoku_robot
from app.modules.board_engine_pool import job_budget
from app.modules.game_center_zjh import ZjhGame

BOARD_GAMES = ('xiangqi', 'gomoku')


class BoardRobots:
    """No engine wait under a DB lock; both engines share a global process cap.

    Executor + process-slot queueing share a 1.5-second absolute job budget.
    If workers are busy or late, tick uses a local move at 1.5 seconds. Position
    versions (not action versions) prevent stale results after undo/new games.
    """
    MAX_WAIT = 1.5

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='board-robot')
        self.lock = threading.Lock()
        self.jobs = {}

    @staticmethod
    def key(table):
        hand = table['hand']
        return table['id'], hand['number'], hand['position_no']

    @staticmethod
    def _run(game, hand, level, deadline):
        with job_budget(deadline):
            if time.monotonic() >= deadline:
                return game.fallback_move(hand, level)
            return game.robot_move(hand, level)

    def schedule(self, game, table):
        hand = table['hand']
        if not hand or not hand['users'][hand['current_player']]['bot']:
            return
        key = self.key(table)
        with self.lock:
            for old, (future, started) in list(self.jobs.items()):
                if old != key and future.done() and (old[0] == key[0] or time.monotonic()-started > 5):
                    del self.jobs[old]
            if key not in self.jobs:
                started = time.monotonic()
                self.jobs[key] = (self.pool.submit(self._run, game, copy.deepcopy(hand),
                    table['rules']['robot_level'], started + self.MAX_WAIT), started)

    def move(self, game, table, clock):
        hand = table['hand']
        elapsed = clock - hand['turn_started_at']
        if elapsed < .5:
            return None
        with self.lock:
            job = self.jobs.get(self.key(table))
        if job and job[0].done():
            try:
                return job[0].result()
            except Exception:
                logging.getLogger(__name__).exception('Board robot failed; using local move')
        if elapsed >= self.MAX_WAIT or job and job[0].done():
            if job:
                job[0].cancel()  # Expired executor-queued work must never spawn later.
            return game.fallback_move(hand, table['rules']['robot_level'])
        return None


class BoardGame:
    max_players = 2
    eligible = staticmethod(ZjhGame.eligible)

    def __init__(self, game):
        self.game = game

    @property
    def engine(self):
        return (gomoku_engine if self.game == 'gomoku' else
                importlib.import_module('app.modules.xiangqi_engine'))

    @property
    def available(self):
        try:
            return self.engine is not None
        except ImportError:
            return False

    def rules(self, tier, cfg):
        return {**tier, 'total_seconds': cfg[self.game + '_total_seconds'],
                'step_seconds': cfg[self.game + '_step_seconds'], 'offline_step_seconds': 120,
                'draw_cooldown_plies': 3, 'auto_play': False}

    @staticmethod
    def funding_message(table):
        unit = '欢乐豆' if table['currency'] == 'beans' else '积分'
        return f"可用余额不足：本场每局需押 {table['rules']['stake']} {unit}，请补足后准备"

    def start(self, service, conn, table, clock, manual=False):
        from app.modules.game_center import GameError
        players = self.eligible(service, conn, table, clock, manual)
        if len(players) != 2:
            table['countdown_at'] = None
            for p in table['seats']:
                if (p['ready'] or manual and p['player'] == table['host']) and p not in players:
                    p.update(ready=False, notice=self.funding_message(table))
            if manual:
                raise GameError('not_ready', '请等待对手准备，并确保双方余额足够本场押注', 409)
            return False
        first_seat = (1-table['first_seat']) if 'first_seat' in table else secrets.randbelow(2)
        table['first_seat'] = first_seat
        players.sort(key=lambda p: p['seat'] != first_seat)
        colors = ('red', 'black') if self.game == 'xiangqi' else ('black', 'white')
        table['hand_no'] += 1
        ref = f"{table['id']}:{table['hand_no']}"
        stake = table['rules']['stake']
        users = {}
        for p, color in zip(players, colors):
            if table['currency'] == 'points':
                service.cash.reserve(conn, 'gc_' + self.game, ref, p['uid'], stake, now=clock,
                                     reason=self.game + '.stake')
            elif not p['bot']:
                conn.execute('UPDATE gc_beans SET balance=balance-? WHERE user_id=?', (stake, p['uid']))
            users[p['player']] = {'uid': p['uid'], 'bot': p['bot'], 'budget': stake}
            p.update(color=color, ready=False, auto=False, notice='')
        hand = {'number': table['hand_no'], 'turn': 1, 'position_no': 0, 'users': users,
                'players': [{k: p[k] for k in ('player', 'seat', 'color')} for p in players],
                'current_player': players[0]['player'], 'created_at': clock, 'settled_at': None,
                'moves': [], 'move_count': 0, 'draw_offer': None, 'draw_last': {},
                'remaining': {p['player']: table['rules']['total_seconds'] for p in players},
                'practice': table['currency'] == 'beans', 'result': None}
        if self.game == 'xiangqi':
            hand['start_fen'] = self.engine.INITIAL_FEN
            hand['fen'] = hand['start_fen']
        table.update(hand=hand, phase='playing', countdown_at=None, idle_deadline=None)
        self.arm_turn(service, table, clock)
        return True

    @staticmethod
    def other(hand, player):
        return next(pid for pid in hand['users'] if pid != player)

    @staticmethod
    def charge(hand, clock):
        current = hand['current_player']
        if current is not None:
            hand['remaining'][current] = max(0, hand['remaining'][current] -
                                             max(0, clock-hand['turn_started_at']))
            hand['turn_started_at'] = clock

    def arm_turn(self, service, table, clock):
        hand = table['hand']
        player = next(p for p in table['seats'] if p['player'] == hand['current_player'])
        offline = not service.board_online(table, player)
        step = table['rules']['offline_step_seconds'] if offline else table['rules']['step_seconds']
        hand.update(turn_started_at=clock, offline_grace=offline,
                    turn_deadline=clock + min(step, hand['remaining'][player['player']]))
        table.update(turn_deadline=hand['turn_deadline'], auto_at=None)

    def tick(self, service, conn, table, clock):
        hand = table['hand']
        if not hand:
            return False
        current = hand['current_player']
        player = next(p for p in table['seats'] if p['player'] == current)
        changed = False
        # Never grant a new grace interval after an already persisted deadline.
        if (not hand['offline_grace'] and clock < table['turn_deadline']
                and not service.board_online(table, player)):
            hand['offline_grace'] = True
            hand['turn_deadline'] = hand['turn_started_at'] + min(
                table['rules']['offline_step_seconds'], hand['remaining'][current])
            table['turn_deadline'] = hand['turn_deadline']
            changed = True
        if clock >= table['turn_deadline']:
            reason = 'aborted' if hand['move_count'] == 0 else 'timeout'
            winner = None if reason == 'aborted' else self.other(hand, current)
            self.settle(service, conn, table, winner, reason, table['turn_deadline'])
            return True
        offer = hand['draw_offer']
        if offer and hand['users'][self.other(hand, offer['player'])]['bot']:
            hand['draw_offer'] = None
            hand['turn'] += 1
            changed = True
        if player['bot']:
            move = service.board_robots.move(self, table, clock)
            if move is not None:
                self.action(service, conn, table, player, {'action': 'move', 'move': move}, clock)
                changed = True
        return changed

    @staticmethod
    def legal_actions(hand, player):
        if hand['result'] or player not in hand['users']:
            return {}
        actions = {'resign': {}}
        offer = hand['draw_offer']
        if not offer and hand['move_count'] - hand['draw_last'].get(player, -3) >= 3:
            actions['offer_draw'] = {}
        elif offer and offer['player'] != player:
            actions.update(accept_draw={}, reject_draw={})
        if player == hand['current_player']:
            actions['move'] = {}
            if (hand['practice'] and not hand['users'][player]['bot'] and len(hand['moves']) >= 2
                    and hand['moves'][-2]['player'] == player):
                actions['undo'] = {}
        return actions

    def action(self, service, conn, table, player, body, clock):
        from app.modules.game_center import GameError
        hand = table['hand']
        pid, action = player['player'], body.get('action')
        if not isinstance(action, str):
            raise GameError('invalid', 'action 须为动作名称')
        if (action == 'offer_draw' and
                hand['move_count'] - hand['draw_last'].get(pid, -3) < 3):
            raise GameError('draw_cooldown', '请在三次落子后再提出和棋', 409)
        if action not in self.legal_actions(hand, pid):
            raise GameError('illegal_action', '当前不能执行此动作')
        if action == 'resign':
            hand['turn'] += 1
            self.settle(service, conn, table, self.other(hand, pid), 'resign', clock)
            return
        if action == 'accept_draw':
            hand['turn'] += 1
            self.settle(service, conn, table, None, 'draw_agreed', clock)
            return
        if action == 'offer_draw':
            hand['draw_offer'] = {'player': pid, 'ply': len(hand['moves'])}
            hand['draw_last'][pid] = hand['move_count']
        elif action == 'reject_draw':
            hand['draw_offer'] = None
        elif action == 'undo':
            self.charge(hand, clock)
            hand['moves'] = hand['moves'][:-2]
            if self.game == 'xiangqi':
                position = self.engine.Position.from_fen(hand['start_fen'])
                for item in hand['moves']:
                    position = position.make(item['move'])
                hand['fen'] = position.fen()
            hand['draw_offer'] = None
            hand['position_no'] += 1
            self.arm_turn(service, table, clock)
        elif action == 'move':
            move = body.get('move')
            if self.game == 'xiangqi':
                if not isinstance(move, str) or not re.fullmatch('[a-i][0-9][a-i][0-9]', move):
                    raise GameError('invalid', '着法须为 h2e2 这样的起终点坐标')
            else:
                try:
                    self.engine.coordinate(move)
                except ValueError as exc:
                    raise GameError('invalid', str(exc)) from None
            try:
                if self.game == 'xiangqi':
                    after = self.engine.Position.from_fen(hand['fen']).make(move)
                    hand['fen'] = after.fen()
                else:
                    self.engine.make([m['move'] for m in hand['moves']], move)
            except ValueError as exc:
                raise GameError('illegal_action', str(exc)) from None
            self.charge(hand, clock)
            color = next(p['color'] for p in hand['players'] if p['player'] == pid)
            hand['moves'].append({'ply': len(hand['moves'])+1, 'player': pid, 'color': color,
                                  'move': copy.deepcopy(move)})
            hand['move_count'] += 1
            hand['position_no'] += 1
            if hand['draw_offer'] and hand['draw_offer']['player'] != pid:
                hand['draw_offer'] = None
            moves = [m['move'] for m in hand['moves']]
            outcome = (self.engine.outcome(hand['start_fen'], moves) if self.game == 'xiangqi'
                       else self.engine.outcome(moves))
            if outcome:
                color = outcome['winner']
                if self.game == 'xiangqi' and color:
                    color = {'w': 'red', 'b': 'black'}[color]
                winner = next((p['player'] for p in hand['players'] if p['color'] == color), None)
                hand['turn'] += 1
                self.settle(service, conn, table, winner, outcome['reason'], clock)
                return
            hand['current_player'] = self.other(hand, pid)
            self.arm_turn(service, table, clock)
        hand['turn'] += 1

    def settle(self, service, conn, table, winner, reason, clock):
        hand = table['hand']
        if hand['settled_at'] is not None:
            return
        self.charge(hand, clock)
        stake, ref = table['rules']['stake'], f"{table['id']}:{hand['number']}"
        scope = 'gc_' + self.game
        net = {pid: (stake if pid == winner else -stake) if winner else 0 for pid in hand['users']}
        if table['currency'] == 'points':
            if winner:
                loser = self.other(hand, winner)
                service.cash.transfer(conn, scope, ref, hand['users'][loser]['uid'],
                                      hand['users'][winner]['uid'], stake, 'settle', now=clock)
            for who in hand['users'].values():
                service.cash.release(conn, scope, ref, who['uid'], now=clock,
                                     reason=self.game + ('.win' if winner else '.refund'))
        else:
            for pid, who in hand['users'].items():
                if not who['bot']:
                    conn.execute('UPDATE gc_beans SET balance=balance+? WHERE user_id=?',
                                 (stake + net[pid], who['uid']))
        hand.update(result={'winner': winner, 'reason': reason, 'net': net}, settled_at=clock,
                    current_player=None, turn_started_at=None, turn_deadline=None, draw_offer=None)
        service.finish_hand(conn, table, clock)

    def leave(self, service, conn, table, player, clock):
        if table['hand']:
            self.action(service, conn, table, player, {'action': 'resign'}, clock)
        return False

    def view(self, hand, player, clock):
        remaining = dict(hand['remaining'])
        current = hand['current_player']
        if current:
            remaining[current] = max(0, remaining[current] - max(0, min(clock, hand['turn_deadline'])
                                                                - hand['turn_started_at']))
        out = {'number': hand['number'], 'turn': hand['turn'], 'current_player': current,
               'finished': hand['result'] is not None, 'players': copy.deepcopy(hand['players']),
               'moves': copy.deepcopy(hand['moves']),
               'last_move': copy.deepcopy(hand['moves'][-1]) if hand['moves'] else None,
               'clocks': {'remaining': remaining, 'running': current,
                          'turn_started_at': hand['turn_started_at'], 'turn_deadline': hand['turn_deadline']},
               'draw_offer': copy.deepcopy(hand['draw_offer']), 'result': copy.deepcopy(hand['result']),
               'legal_actions': self.legal_actions(hand, player), 'in_check': None}
        if self.game == 'gomoku':
            out['board'] = {'size': 15, 'moves': [copy.deepcopy(m['move']) for m in hand['moves']]}
        else:
            position = self.engine.Position.from_fen(hand['fen'])
            out.update(board={'fen': hand['fen']}, in_check=position.in_check())
            if player == current and current is not None:
                out['legal_moves'] = position.legal_moves()
        return out

    def fallback_move(self, hand, level):
        if self.game == 'gomoku':
            return gomoku_robot.fallback_move([m['move'] for m in hand['moves']], level)
        robot = importlib.import_module('app.modules.xiangqi_robot')
        return robot.robot_move(hand['fen'], level, binary=None, nnue=None)

    def robot_move(self, hand, level):
        if self.game == 'gomoku':
            return gomoku_robot.robot_move([m['move'] for m in hand['moves']], level)
        robot = importlib.import_module('app.modules.xiangqi_robot')
        return robot.robot_move(hand['fen'], level, binary=os.environ.get('MEDIADECK_PIKAFISH_BIN', ''),
                                nnue=os.environ.get('MEDIADECK_PIKAFISH_NNUE', ''))
