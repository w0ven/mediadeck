"""Versioned player-funded banker rules; no issuance or pooled odds."""
from __future__ import annotations

import json
from collections import defaultdict

from app.modules.play_money import integer

BANKER_VERSION = 'dice-size-banker-v1'
QUICK_BANKER_VERSION = 'dice-size-banker-quicknet-v1'
BANKER_VERSIONS = (BANKER_VERSION, QUICK_BANKER_VERSION)


def is_banker(row):
    return json.loads(row['rules']).get('version') in BANKER_VERSIONS


def banker(row):
    return json.loads(row['rules'])['banker']


def settle_banker(bets, dice, bank, *, force_refund=False):
    capital = integer(bank['capital'], '庄本')
    totals = {'大': 0, '小': 0}
    stakes = defaultdict(lambda: {'大': 0, '小': 0})
    for bet in bets:
        if bet['user_id'] == bank['user_id'] or bet['option'] not in totals:
            raise RuntimeError('invalid banker bet')
        amount = integer(bet['amount'], '下注积分')
        totals[bet['option']] += amount
        stakes[bet['user_id']][bet['option']] += amount
    total = sum(totals.values())
    integer(capital+total, '本局托管总额')
    if abs(totals['大']-totals['小']) > capital:
        raise RuntimeError('banker exposure exceeds collateral')
    reason = 'timeout' if force_refund else 'empty' if not total else ''
    if not reason and (len(dice) != 3 or any(type(d) is not int or not 1 <= d <= 6 for d in dice)):
        raise RuntimeError('banker settlement requires three valid dice')
    winner = None if reason or len(set(dice)) == 1 else '大' if sum(dice) >= 11 else '小'
    users = {}
    for uid, sides in stakes.items():
        invested = sum(sides.values())
        returned = invested if reason else 2*sides.get(winner, 0)
        users[uid] = {'invested': invested, 'returned': returned, 'net': returned-invested}
    net = total-sum(outcome['returned'] for outcome in users.values())
    house = {'invested': capital, 'returned': capital+net, 'net': net}
    if house['returned'] < 0 or sum(o['returned'] for o in users.values())+house['returned'] != capital+total:
        raise RuntimeError('banker conservation failed')
    return {'total': total, 'reason': reason, 'users': users, 'banker': house}
