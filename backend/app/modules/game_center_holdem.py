"""Hold'em table adapter: continuous hands, persistent stacks and escrow-only results."""
from __future__ import annotations

import hashlib
import secrets

from app.modules import holdem_engine as engine
from app.modules.game_center_zjh import card_code
from app.modules.play_money import integer


def robot_action(state, player, rng=None):
    """Practice AI sees only its own hole + board; every choice uses engine limits."""
    rng = rng or secrets.SystemRandom()
    options = engine.legal_actions(state, player)
    actor = next(p for p in state['seats'] if p['player'] == player)
    hole, board = actor['hole'], state['board']
    if not board:
        a, b = [c % 13 + 2 for c in hole]
        equity = (.57 + a / 50 if a == b else .17 + (a + b) / 65
                  + .08 * (hole[0] // 13 == hole[1] // 13) + .07 * (abs(a - b) == 1))
    else:
        strength, _, _ = engine.best_hand(hole + board)
        equity = (.17, .43, .61, .72, .80, .86, .93, .97, .99)[strength[0]] + strength[1] / 14 * .06
        if len(board) < 5 and max(sum(c // 13 == suit for c in hole + board) for suit in range(4)) == 4:
            equity += .1
    pot = sum(p['committed'] for p in state['seats'])
    odds = options['call'] / max(1, pot + options['call'])
    bluff = rng.random() < .07
    if (options['min_raise_to'] is not None and (equity > .72 or bluff)
            and rng.random() < (.55 if equity > .9 else .24)):
        target = min(options['max_raise_to'], max(options['min_raise_to'],
                     state['current_bet'] + round((pot + options['call']) * (.45 + rng.random() * .3))))
        return ('bet' if state['current_bet'] == 0 else 'raise'), target
    if options['check']:
        return 'check', None
    if ((equity < odds + .14 and not bluff)
            or (equity < .45 and options['call'] > state['big_blind'] * 5 and rng.random() < .65)):
        return 'fold', None
    return 'call', None


class HoldemGame:
    max_players = 6
    engine = engine

    @staticmethod
    def rules(tier, cfg):
        return {**tier, 'step_seconds': cfg['step_seconds'], 'auto_hands': cfg['holdem_auto_hands'],
                'auto_seconds': cfg['holdem_auto_seconds'], 'away_seconds': cfg['holdem_away_seconds']}

    def buy_in(self, service, conn, table, player, body, clock):
        from app.modules.game_center import GameError
        rules = table['rules']
        amount = body.get('buy_in', 2000 if table['currency'] == 'beans' else None)
        integer(amount, '带入', rules['buy_min'], rules['buy_max'] or 2**63 - 1)
        if amount > service.available(conn, table, player, clock):
            raise GameError('insufficient_funds', '可用余额不足带入额')
        if table['currency'] == 'points':
            row = conn.execute('SELECT amount FROM play_escrows WHERE scope=? AND ref=? AND user_id=?',
                               ('gc_holdem', table['id'], player['uid'])).fetchone()
            if row:
                if row['amount']:
                    raise GameError('wrong_phase', '上次离桌尚未结算', 409)
                # One durable escrow per table/member, including every later visit.
                service.cash.top_up(conn, 'gc_holdem', table['id'], player['uid'], amount,
                                    'buy:' + player['player'], now=clock, reason='game.buy_in')
            else:
                service.cash.reserve(conn, 'gc_holdem', table['id'], player['uid'], amount,
                                     now=clock, reason='game.buy_in')
        else:
            conn.execute('UPDATE gc_beans SET balance=balance-? WHERE user_id=?', (amount, player['uid']))
        player.update(stack=amount, auto_since=None, away_since=None)

    @staticmethod
    def release(service, conn, table, player, clock):
        if player['bot']:
            return
        if table['currency'] == 'points':
            if service.cash.held(conn, 'gc_holdem', table['id'], player['uid']) != player['stack']:
                raise RuntimeError('holdem cash-out stack mismatch')
            service.cash.release(conn, 'gc_holdem', table['id'], player['uid'], now=clock, reason='game.cash_out')
        else:
            conn.execute('UPDATE gc_beans SET balance=balance+? WHERE user_id=?', (player['stack'], player['uid']))

    @staticmethod
    def eligible(table):
        players = [p for p in table['seats'] if p['stack'] > 0 and not p['away'] and not p['leave_after']]
        # A private practice table never burns through robot-only hands while its owner is away/broke.
        return players if table['currency'] != 'beans' or any(not p['bot'] for p in players) else []

    def schedule(self, service, conn, table, clock):
        if table['phase'] not in ('waiting', 'between'):
            return
        if len(self.eligible(table)) < 2:
            table.update(phase='waiting', countdown_at=None, idle_deadline=table['idle_deadline'] or clock + 600)
        elif table['hand_no']:
            table.update(phase='between', countdown_at=table['countdown_at'] or clock + 4, idle_deadline=None)
        elif len(table['seats']) == self.max_players:
            self.start(service, conn, table, clock)

    def start(self, service, conn, table, clock, manual=False):
        from app.modules.game_center import GameError
        players = self.eligible(table)
        if manual and table['hand_no']:
            raise GameError('wrong_phase', '德州开始后自动连续发牌，无需再次开始', 409)
        if len(players) < 2:
            table.update(phase='waiting', countdown_at=None, idle_deadline=table['idle_deadline'] or clock + 600)
            if manual:
                raise GameError('not_ready', '至少需要2名有筹码且未暂离的玩家', 409)
            return False
        previous = table.get('button')
        if previous is None:
            button = next((p['seat'] for p in players if p['player'] == table['host']), players[0]['seat'])
        else:
            button = min(players, key=lambda p: (p['seat'] - previous - 1) % 6)['seat']
        deck = list(range(52))
        secrets.SystemRandom().shuffle(deck)
        salt = secrets.token_hex(32)
        codes = ','.join(map(card_code, deck))
        state = engine.new_hand(seats=[{k: p[k] for k in ('seat', 'player', 'stack')} for p in players],
                                button=button, small_blind=table['rules']['small_blind'],
                                big_blind=table['rules']['big_blind'], deck=deck)
        table['hand_no'] += 1
        hand = {'number': table['hand_no'], 'turn': 1, 'deck': deck, 'deck_codes': codes, 'salt': salt,
                'commitment': hashlib.sha256((codes + '|' + salt).encode('ascii')).hexdigest(),
                'deal_order': [p['player'] for p in sorted(players, key=lambda p: (p['seat'] - button - 1) % 6)],
                'users': {p['player']: {'uid': p['uid'], 'bot': p['bot'], 'budget': p['stack']} for p in players},
                'created_at': clock, 'settled_at': None, 'engine': state}
        table.update(hand=hand, button=button, phase='playing', countdown_at=None, idle_deadline=None)
        self._sync_stacks(table)
        if engine.is_finished(state):
            self.settle(service, conn, table, clock)
        else:
            service.arm_turn(table, clock)
        return True

    @staticmethod
    def _sync_stacks(table):
        stacks = {p['player']: p['stack'] for p in table['hand']['engine']['seats']}
        for p in table['seats']:
            if p['player'] in stacks:
                p['stack'] = stacks[p['player']]

    @staticmethod
    def auto_body(table, player):
        if player['leave_after']:
            action, amount = 'fold', None
        elif player['bot']:
            action, amount = robot_action(table['hand']['engine'], player['player'])
        else:
            action, amount = engine.auto_action(table['hand']['engine'], player['player'])
        return {'action': action, 'amount': amount}

    def action(self, service, conn, table, player, body, clock, *, automatic=False):
        from app.modules.game_center import GameError
        if player['leave_after'] and not automatic:
            raise GameError('wrong_phase', '已申请离桌，等待本手结算', 409)
        try:
            state = engine.apply_action(table['hand']['engine'], player['player'], body.get('action'), body.get('amount'))
        except engine.EngineError as exc:
            raise GameError('illegal_action', str(exc)) from None
        table['hand']['engine'] = state
        table['hand']['turn'] += 1
        self._sync_stacks(table)
        if engine.is_finished(state):
            self.settle(service, conn, table, clock)
        else:
            service.arm_turn(table, clock)

    def leave(self, service, conn, table, player, clock):
        if not table['hand'] or player['player'] not in table['hand']['users']:
            return False
        player.update(leave_after=True, auto=True, auto_since=player['auto_since'] or clock,
                      notice='已申请离桌，轮到时自动弃牌；本手结束后退回剩余筹码')
        if player['player'] == table['host']:
            service._transfer_host(table, clock)
        if engine.current_player(table['hand']['engine']) == player['player']:
            service.arm_auto(table, clock)
        return True

    @staticmethod
    def _mark_away(table, player, clock, notice=''):
        player['away'] = True
        in_hand = table['hand'] and player['player'] in table['hand']['users']
        player['away_since'] = None if in_hand else clock
        if notice:
            player['notice'] = notice

    def operation(self, service, conn, table, player, op, body, clock):
        from app.modules.game_center import GameError
        if player['leave_after']:
            raise GameError('wrong_phase', '已申请离桌，等待本手结算', 409)
        if op == 'away':
            enabled = body.get('enabled')
            if type(enabled) is not bool:
                raise GameError('invalid', 'enabled 须为布尔值')
            if enabled != player['away']:
                if enabled:
                    self._mark_away(table, player, clock)
                else:
                    player.update(away=False, away_since=None, auto_hands=0,
                                  auto_since=clock if player['auto'] else None, notice='')
        elif op == 'top-up':
            if table['hand']:
                raise GameError('wrong_phase', '只能在两手之间补码', 409)
            service._member(player['uid'])
            amount = integer(body.get('amount'), '补码')
            maximum = table['rules']['buy_max']
            if maximum is not None and player['stack'] + amount > maximum:
                raise GameError('invalid', '补码后不能超过最高带入额')
            if amount > service.available(conn, table, player, clock):
                raise GameError('insufficient_funds', '可用余额不足补码')
            if table['currency'] == 'points':
                service.cash.top_up(conn, 'gc_holdem', table['id'], player['uid'], amount,
                                    player['player'] + ':' + body['request_id'], now=clock)
            else:
                conn.execute('UPDATE gc_beans SET balance=balance-? WHERE user_id=?', (amount, player['uid']))
            player['stack'] += amount
            player['notice'] = ''

    @staticmethod
    def deadlines(table):
        events = []
        for p in table['seats']:
            if p['bot'] or p['leave_after']:
                continue
            if p['away_since'] is not None:
                events.append((p['away_since'] + table['rules']['away_seconds'], 'away:' + p['player']))
            elif p['auto'] and not p['away'] and p['auto_since'] is not None:
                events.append((p['auto_since'] + table['rules']['auto_seconds'], 'auto-away:' + p['player']))
        return events

    def timer(self, service, conn, table, event, clock):
        kind, pid = event.split(':', 1)
        player = next(p for p in table['seats'] if p['player'] == pid)
        if kind == 'away':
            service._remove(conn, table, player, clock)
        else:
            self._mark_away(table, player, clock, '连续托管已达上限，已转为暂离；恢复后可参与下一手')

    def settle(self, service, conn, table, clock):
        hand = table['hand']
        if hand['settled_at'] is not None:
            return
        result = engine.results(hand['engine'])
        if sum(p['net'] for p in result['players'].values()) != 0:
            raise RuntimeError('holdem engine results do not conserve chips')
        if table['currency'] == 'points':
            for who in hand['users'].values():
                if service.cash.held(conn, 'gc_holdem', table['id'], who['uid']) != who['budget']:
                    raise RuntimeError('holdem hand escrow mismatch')
            debt = [[key, -p['net']] for key, p in result['players'].items() if p['net'] < 0]
            credit = [[key, p['net']] for key, p in result['players'].items() if p['net'] > 0]
            index = 0
            for loser in debt:
                for winner in credit:
                    amount = min(loser[1], winner[1])
                    if amount:
                        service.cash.transfer(conn, 'gc_holdem', table['id'], hand['users'][loser[0]]['uid'],
                                              hand['users'][winner[0]]['uid'], amount,
                                              f"hand:{hand['number']}:{index}", now=clock)
                        loser[1] -= amount
                        winner[1] -= amount
                        index += 1
            for pid, value in result['players'].items():
                if service.cash.held(conn, 'gc_holdem', table['id'], hand['users'][pid]['uid']) != value['stack']:
                    raise RuntimeError('holdem settlement escrow mismatch')
        self._sync_stacks(table)
        hand['settled_at'] = clock
        table.update(phase='waiting', last_hand=hand, hand=None, turn_deadline=None, auto_at=None,
                     countdown_at=None, idle_deadline=clock + 600, last_activity=clock, host_active=clock)
        for p in list(table['seats']):
            if p['player'] in hand['users'] and p['auto'] and not p['bot']:
                p['auto_hands'] += 1
            if p['leave_after']:
                service._remove(conn, table, p, clock)
                continue
            if p['bot'] and p['stack'] == 0:
                p['stack'] = 2000  # Clearly-labelled practice robots, never point-table members.
            if not p['bot']:
                if p['auto'] and not p['away'] and p['auto_hands'] >= table['rules']['auto_hands']:
                    self._mark_away(table, p, clock, '连续托管已达上限，已转为暂离；恢复后可参与下一手')
                elif p['away'] and p['away_since'] is None:
                    p['away_since'] = clock
                if p['stack'] == 0:
                    p['notice'] = '筹码已归零，请补码或离桌；未补码不会扣盲注'
        self.schedule(service, conn, table, clock)

    @staticmethod
    def view(hand, player):
        public = engine.view(hand['engine'], player)
        return {'number': hand['number'], 'turn': hand['turn'], 'commitment': hand['commitment'],
                'current_player': public['current_player'], 'finished': public['finished'], 'engine': public,
                'legal_actions': public['legal_actions'], 'result': public['results']}
