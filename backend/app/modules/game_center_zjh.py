"""Three-card adapter: the table shell owns membership/timers; this owns hand money."""
from __future__ import annotations

import hashlib
import secrets

from app.modules import zjh_engine as engine

_ZJH_RANKS = {11: 'J', 12: 'Q', 13: 'K', 14: 'A'}


def card_code(card):
    return '23456789TJQKA'[card % 13] + 'shcd'[card // 13]


def zjh_hand_name(cards, allow_235=False, has_trips=False):
    rank = engine.strength(cards)
    top = _ZJH_RANKS.get(rank[1], str(rank[1]))
    if allow_235 and engine._special(cards):
        return '特殊 235 · 吃豹子' if has_trips else '特殊 235 · 单张'
    if rank[0] == 5:
        return f'豹子 · {top}'
    if rank[0] == 4:
        return f'顺金 · {top} 高'
    if rank[0] == 3:
        return f'金花 · {top} 高'
    if rank[0] == 2:
        return f'顺子 · {top} 高'
    if rank[0] == 1:
        return f'对子 · {top}'
    return f'高牌 · {top}'


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
        hand['revealed'] = list(result['winners']) if hand['engine']['reason'] != 'last_player' else []
        hand['settled_at'] = clock
        service.finish_hand(conn, table, clock)

    @staticmethod
    def hand_label_for(hand, player):
        raw = hand['engine']
        target = next((p for p in raw['players'] if p['player'] == player), None)
        if not target:
            return ''
        live = [p for p in raw['players'] if not p['folded']]
        has_trips = any(engine.strength(p['cards'])[0] == 5 for p in live)
        return zjh_hand_name(target['cards'], raw.get('allow_235', False), has_trips)

    @staticmethod
    def view(hand, player):
        raw = hand['engine']
        out = engine.view(raw, player)
        live = [p for p in raw['players'] if not p['folded']]
        has_trips = any(engine.strength(p['cards'])[0] == 5 for p in live)
        by_id = {p['player']: p for p in raw['players']}
        if raw['finished']:
            revealed = set(hand['revealed']) if 'revealed' in hand else set(raw['showdown'])
            for pub in out['players']:
                pid = pub['player']
                orig = by_id[pid]
                public_shown = pid in revealed
                visible = public_shown or (pid == player and orig['seen'])
                pub['shown'] = public_shown
                pub['cards'] = list(orig['cards']) if visible else None
                pub['hand_name'] = zjh_hand_name(orig['cards'], raw.get('allow_235', False), has_trips) if visible else None
            out['revealed'] = sorted(revealed)
            out['can_reveal'] = bool(player and player in hand['users'] and player not in revealed)
        else:
            for pub in out['players']:
                pid = pub['player']
                orig = by_id[pid]
                visible = pid == player and orig['seen']
                pub['shown'] = False
                pub['cards'] = list(orig['cards']) if visible else None
                pub['hand_name'] = zjh_hand_name(orig['cards'], raw.get('allow_235', False), False) if visible else None
            out['revealed'] = []
            out['can_reveal'] = False
        return {**out, 'number': hand['number'], 'commitment': hand['commitment']}
