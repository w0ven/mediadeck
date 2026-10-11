"""Seven disjoint pools; aggregate users before largest-remainder rounding."""
import json
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import combinations

from app.modules.play_money import PlayError, integer

MAX_INT = 2**63 - 1
POOLS = {
    'size': ('大小', ('大', '小')),
    'parity': ('单双', ('单', '双')),
    'single': ('单点', tuple(str(i) for i in range(1, 7))),
    'double': ('对子', tuple(str(i)*2 for i in range(1, 7))),
    'combo': ('组合', tuple(f'{a}+{b}' for a, b in combinations(range(1, 7), 2))),
    'triple': ('豹子', ('无豹子', '任意豹子', *(str(i)*3 for i in range(1, 7)))),
    'total': ('总点数', tuple(str(i) for i in range(3, 19))),
}


def normalize(pool, option):
    if pool == 'combo' and isinstance(option, str):
        parts = option.split('+')
        if len(parts) == 2 and all(p in '123456' and len(p) == 1 for p in parts):
            option = '+'.join(sorted(parts))
    if pool not in POOLS or option not in POOLS[pool][1]:
        raise PlayError('投注选项无效')
    return option


def validate(items):
    if not isinstance(items, list) or not items:
        raise PlayError('请先加入草稿')
    merged = defaultdict(int)
    for item in items:
        if not isinstance(item, dict):
            raise PlayError('投注明细无效')
        p = item.get('pool')
        o = normalize(p, item.get('option'))
        merged[p, o] += integer(item.get('amount'), '积分')
    integer(sum(merged.values()), '总投入')
    return [{'pool': p, 'option': o, 'amount': a} for (p, o), a in sorted(merged.items())]


def hits(dice):
    if len(dice) != 3 or any(type(d) is not int or not 1 <= d <= 6 for d in dice):
        raise ValueError('invalid dice')
    count, total = Counter(dice), sum(dice)
    triple = len(count) == 1
    return {
        'size': {} if triple else {'大' if total >= 11 else '小': 1},
        'parity': {} if triple else {'单' if total % 2 else '双': 1},
        'single': {str(k): v for k, v in count.items()},
        'double': {str(k)*2: 1 for k, v in count.items() if v >= 2},
        'combo': {f'{a}+{b}': 1 for a, b in combinations(sorted(count), 2)},
        'triple': {'任意豹子': 1, str(dice[0])*3: 1} if triple else {'无豹子': 1},
        'total': {str(total): 1},
    }


def settle_pool(pool, bets, dice, seed, *, force_refund=False):
    winning = hits(dice)[pool] if dice else {}
    users = {}
    for bet in bets:
        u = users.setdefault(str(bet['user_id']), {'invested': 0, 'winning': 0, 'weight': 0, 'bonus': 0, 'extra': 0})
        amount = integer(bet['amount'])
        normalize(pool, bet['option'])
        u['invested'] += amount
        weight = winning.get(bet['option'], 0)
        if weight:
            u['winning'] += amount
            u['weight'] += amount * weight
    total = sum(u['invested'] for u in users.values())
    integer(total, low=0)
    win = sum(u['winning'] for u in users.values())
    lost = total - win
    reason = ('empty' if not total else 'beacon_timeout' if force_refund else 'no_hit' if not win else 'no_loser' if not lost else '')
    if dice and len(set(dice)) == 1 and pool in ('size', 'parity'):
        reason = 'triple_refund' if total else 'empty'
    if reason:
        for u in users.values():
            u.update(returned=u['invested'], net=0, refund=u['invested'])
    else:
        weight = sum(u['weight'] for u in users.values())
        winners = [uid for uid, u in users.items() if u['weight']]
        for uid in winners:
            users[uid]['bonus'] = lost * users[uid]['weight'] // weight
        remainder = lost - sum(u['bonus'] for u in users.values())
        def order(uid):
            tie = sha256(json.dumps([seed, pool, uid], ensure_ascii=True, separators=(',', ':')).encode('ascii')).digest()
            return (-(lost * users[uid]['weight'] % weight), tie, uid)
        for uid in sorted(winners, key=order)[:remainder]:
            users[uid]['bonus'] += 1
            users[uid]['extra'] = 1
        for u in users.values():
            returned = u['winning'] + u['bonus']
            u.update(returned=returned, net=returned-u['invested'], refund=0)
    assert sum(u['returned'] for u in users.values()) == total
    return {'total': total, 'reason': reason, 'users': users}
