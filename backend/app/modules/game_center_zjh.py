"""Three-card adapter: the table shell owns membership/timers; this owns hand money."""
from __future__ import annotations

import hashlib
import secrets

from app.modules import zjh_engine as engine


def card_code(card):
    return '23456789TJQKA'[card % 13] + 'shcd'[card // 13]


class ZjhGame:
    max_players = 5
    engine = engine

    @staticmethod
    def rules(tier, cfg):
        return {**tier, 'step_seconds': cfg['step_seconds'], 'allow_235': cfg['allow_235'],
                'auto_hands': cfg['zjh_auto_hands']}

    @staticmethod
    def eligible(service, conn, table, clock, manual=False):
        return [p for p in table['seats'] if not p['leave_after']
                and (p['ready'] or manual and p['player'] == table['host'])
                and service.can_fund(conn, table, p, clock)]

    @staticmethod
    def funding_message(table):
        rules = table['rules']
        if rules['cap'] is None:
            return f"可用余额不足：本场至少需要 {rules['min_balance']} 积分，每手冻结全部可用积分"
        unit = '欢乐豆' if table['currency'] == 'beans' else '积分'
        return f"可用余额不足：本场每手需 {rules['cap']} {unit}作为封顶预算，请补足后准备"

    def start(self, service, conn, table, clock, manual=False):
        from app.modules.game_center import GameError
        players = self.eligible(service, conn, table, clock, manual)
        for p in table['seats']:
            if p in players:
                if '余额不足' in p['notice']:
                    p['notice'] = ''
            elif p['ready'] or manual and p['player'] == table['host']:
                p.update(ready=False, notice=self.funding_message(table))
        if len(players) < 2:
            table['countdown_at'] = None
            if manual:
                raise GameError('not_ready', '已准备且余额足够的玩家至少需要2人', 409)
            return False
        table['hand_no'] += 1
        ref = f"{table['id']}:{table['hand_no']}"
        seats = []
        users = {}
        for p in players:
            budget = table['rules']['cap'] or service.available(conn, table, p, clock)
            if table['currency'] == 'points':
                service.cash.reserve(conn, 'gc_zjh', ref, p['uid'], budget, now=clock)
            elif not p['bot']:
                conn.execute('UPDATE gc_beans SET balance=balance-? WHERE user_id=?', (budget, p['uid']))
            users[p['player']] = {'uid': p['uid'], 'bot': p['bot'], 'budget': budget}
            seats.append({'seat': p['seat'], 'player': p['player'], 'stack': budget})
        deck = list(range(52))
        secrets.SystemRandom().shuffle(deck)
        salt = secrets.token_hex(32)
        codes = ','.join(map(card_code, deck))
        hand = {'number': table['hand_no'], 'turn': 1, 'deck': deck, 'deck_codes': codes, 'salt': salt,
                'commitment': hashlib.sha256((codes + '|' + salt).encode('ascii')).hexdigest(),
                'deal_order': [p['player'] for p in sorted(seats, key=lambda p: p['seat'])],
                'users': users, 'created_at': clock, 'settled_at': None,
                'engine': engine.new_hand(seats, table['rules']['ante'], deck,
                                          allow_235=table['rules']['allow_235'], secret=salt)}
        table.update(hand=hand, phase='playing', countdown_at=None, idle_deadline=None)
        for p in table['seats']:
            p['ready'] = False
        self._consume(service, conn, table, None, hand['engine'], clock)
        service.arm_turn(table, clock)
        return True

    @staticmethod
    def _consume(service, conn, table, before, after, clock):
        if table['currency'] != 'points':
            return
        ref = f"{table['id']}:{table['hand_no']}"
        old = {p['player']: p['invested'] for p in before['players']} if before else {}
        for p in after['players']:
            amount = p['invested'] - old.get(p['player'], 0)
            if amount:
                uid = table['hand']['users'][p['player']]['uid']
                service.cash.consume(conn, 'gc_zjh', ref, uid, amount, 'poker', ref, now=clock)

    def action(self, service, conn, table, player, body, clock, *, automatic=False):
        from app.modules.game_center import GameError
        hand = table['hand']
        before = hand['engine']
        try:
            after = engine.apply_action(before, player['player'], body.get('action'),
                                        body.get('amount'), body.get('target'))
        except engine.EngineError as exc:
            raise GameError('illegal_action', str(exc)) from None
        self._consume(service, conn, table, before, after, clock)
        hand['engine'] = after
        hand['turn'] = after['turn']
        if engine.is_finished(after):
            self.settle(service, conn, table, clock)
        elif after['turn'] != before['turn']:
            service.arm_turn(table, clock)
        elif automatic:
            service.arm_auto(table, clock)

    def leave(self, service, conn, table, player, clock):
        if not table['hand'] or player['player'] not in table['hand']['users']:
            return False
        before = table['hand']['engine']
        player['leave_after'] = True
        after = engine.force_fold(before, player['player'])
        table['hand']['engine'] = after
        table['hand']['turn'] = after['turn']
        if engine.is_finished(after):
            self.settle(service, conn, table, clock)
        elif after['turn'] != before['turn']:
            service.arm_turn(table, clock)
        return True

    def settle(self, service, conn, table, clock):
        hand = table['hand']
        if hand['settled_at'] is not None:
            return
        ref = f"{table['id']}:{hand['number']}"
        result = engine.results(hand['engine'])
        for p in hand['engine']['players']:
            who = hand['users'][p['player']]
            award = result['awards'][p['player']]
            if table['currency'] == 'points':
                service.cash.release(conn, 'gc_zjh', ref, who['uid'], now=clock)
                if award:
                    service.cash.payout(conn, 'poker', ref, who['uid'], award, now=clock)
            elif not who['bot']:
                conn.execute('UPDATE gc_beans SET balance=balance+? WHERE user_id=?',
                             (who['budget'] - p['invested'] + award, who['uid']))
        hand['settled_at'] = clock
        service.finish_hand(conn, table, clock)

    @staticmethod
    def view(hand, player):
        return {**engine.view(hand['engine'], player), 'number': hand['number'],
                'commitment': hand['commitment']}
