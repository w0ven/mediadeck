"""Pure JSON three-card engine. No money, clocks, I/O or other players' views."""
from __future__ import annotations

from copy import deepcopy

from app.modules.play_rounds import fair_split
from app.modules.poker import CATEGORIES, strength


class EngineError(ValueError):
    pass


def new_hand(seats, ante, deck, *, allow_235=False, secret):
    if (not 2 <= len(seats) <= 5 or type(ante) is not int or ante < 1
            or len(deck) != 52 or set(deck) != set(range(52))
            or any(type(c) is not int for c in deck)
            or len({p['player'] for p in seats}) != len(seats)):
        raise EngineError('牌局参数无效')
    players = []
    for i, p in enumerate(sorted(seats, key=lambda p: p['seat'])):
        if type(p['stack']) is not int or p['stack'] < ante:
            raise EngineError('预算不足底注')
        players.append({'player': p['player'], 'seat': p['seat'], 'budget': p['stack'],
                        'cards': deck[i * 3:i * 3 + 3], 'seen': False, 'folded': False,
                        'invested': ante})
    return {'players': players, 'ante': ante, 'bet': ante, 'round': 1, 'turn': 1,
            'current_player': players[0]['player'], 'acted': [], 'comparisons': [],
            'allow_235': bool(allow_235), 'secret': secret, 'finished': False,
            'reason': None, 'result': None, 'showdown': []}


def current_player(state):
    return None if state['finished'] else state['current_player']


def _player(state, player):
    return next((p for p in state['players'] if p['player'] == player), None)


def _special(cards):
    return strength(cards)[0] == 0 and sorted(c % 13 + 2 for c in cards) == [2, 3, 5]


def compare(a, b, allow_235=False):
    sa, sb = strength(a), strength(b)
    if allow_235:
        if _special(a) and sb[0] == 5:
            return 1
        if _special(b) and sa[0] == 5:
            return -1
    return (sa > sb) - (sa < sb)


def legal_actions(state, player):
    p = _player(state, player)
    if not p or p['folded'] or state['finished']:
        return {}
    actions = {} if p['seen'] else {'look': {}}
    if current_player(state) != player:
        return actions
    left = p['budget'] - p['invested']
    due = state['bet'] * (2 if p['seen'] else 1)
    actions['fold'] = {}
    if left < due:
        actions['all_in'] = {'amount': left}
    else:
        actions['call'] = {'amount': due}
    raised = (state['bet'] + state['ante']) * (2 if p['seen'] else 1)
    if state['bet'] < 5 * state['ante'] and raised <= left:
        actions['raise'] = {'amount': raised}
    if p['seen'] and state['round'] >= 2 and 2 * due <= left:
        actions['compare'] = {'amount': 2 * due, 'targets': [q['player'] for q in state['players']
                                                         if not q['folded'] and q != p]}
    return actions


def _settle(s, reason):
    live = [p for p in s['players'] if not p['folded']]
    has_trips = any(strength(p['cards'])[0] == 5 for p in live)
    def value(p):
        if s['allow_235'] and has_trips and _special(p['cards']):
            return (6,)
        return strength(p['cards'])
    best = max(value(p) for p in live)
    winners = [p['player'] for p in live if value(p) == best]
    awards = dict.fromkeys((p['player'] for p in s['players']), 0)
    awards.update(fair_split(sum(p['invested'] for p in s['players']), winners, s['secret']))
    s.update(finished=True, current_player=None, reason=reason,
             showdown=[p['player'] for p in live] if len(live) > 1 else [],
             result={'winners': winners, 'awards': awards,
                     'net': {p['player']: awards[p['player']] - p['invested'] for p in s['players']}})


def _advance(s, actor):
    live = [p for p in s['players'] if not p['folded']]
    if len(live) == 1:
        _settle(s, 'last_player')
        return
    s['acted'] = list(set(s['acted']) | {actor})
    if all(p['player'] in s['acted'] for p in live):
        if s['round'] >= 10:
            _settle(s, 'round_limit')
            return
        s['round'] += 1
        s['acted'] = []
    pos = next(i for i, p in enumerate(s['players']) if p['player'] == actor)
    for step in range(1, len(s['players']) + 1):
        q = s['players'][(pos + step) % len(s['players'])]
        if not q['folded']:
            s['current_player'] = q['player']
            break
    s['turn'] += 1


def apply_action(state, player, action, amount=None, target=None):
    actions = legal_actions(state, player)
    p = _player(state, player)
    if action == 'look' and p and not p['folded'] and not state['finished'] and amount is None:
        s = deepcopy(state)
        _player(s, player)['seen'] = True
        return s
    if not isinstance(action, str) or action not in actions or amount is not None:
        raise EngineError('当前不能执行这个动作')
    s = deepcopy(state)
    p = _player(s, player)
    if action == 'fold':
        p['folded'] = True
    else:
        if action == 'compare' and target not in actions[action]['targets']:
            raise EngineError('比牌对象无效')
        p['invested'] += actions[action]['amount']
        if action == 'raise':
            s['bet'] += s['ante']
        if action == 'all_in':
            _settle(s, 'all_in')
            return s
        if action == 'compare':
            other = _player(s, target)
            loser = other if compare(p['cards'], other['cards'], s['allow_235']) > 0 else p
            loser['folded'] = True
            s['comparisons'].append({'turn': s['turn'], 'actor': player, 'target': target,
                                    'loser': loser['player'],
                                    'cards': {player: p['cards'], target: other['cards']}})
    _advance(s, player)
    return s


def force_fold(state, player):
    """Explicit leave may fold out of turn; never grant a fresh action deadline."""
    p = _player(state, player)
    if state['finished'] or not p or p['folded']:
        return deepcopy(state)
    if current_player(state) == player:
        return apply_action(state, player, 'fold')
    s = deepcopy(state)
    _player(s, player)['folded'] = True
    if sum(not q['folded'] for q in s['players']) == 1:
        _settle(s, 'last_player')
    return s


def auto_action(state, player):
    actions = legal_actions(state, player)
    if not actions or current_player(state) != player:
        raise EngineError('尚未轮到你')
    if 'look' in actions:
        return 'look'
    if strength(_player(state, player)['cards'])[0] >= 1:
        return 'call' if 'call' in actions else 'all_in'
    return 'fold'


def is_finished(state):
    return state['finished']


def results(state):
    if not is_finished(state):
        raise EngineError('牌局尚未结算')
    return deepcopy(state['result'])


def view(state, viewer):
    players = []
    for p in state['players']:
        visible = p['player'] in state['showdown'] or p['player'] == viewer and p['seen']
        players.append({k: p[k] for k in ('player', 'seat', 'seen', 'folded', 'invested')})
        players[-1].update(remaining=p['budget'] - p['invested'],
                           cards=list(p['cards']) if visible else None,
                           hand_name=CATEGORIES[strength(p['cards'])[0]] if visible else None)
    return {'turn': state['turn'], 'round': state['round'], 'bet': state['bet'],
            'current_player': current_player(state), 'pot': sum(p['invested'] for p in state['players']),
            'players': players, 'legal_actions': legal_actions(state, viewer),
            'finished': state['finished'], 'reason': state['reason'],
            'comparisons': deepcopy([c for c in state['comparisons'] if viewer in (c['actor'], c['target'])]),
            'result': deepcopy(state['result'])}
