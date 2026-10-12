"""Pinned drand mainnet, chained BLS signatures (G1 keys / G2 signatures)."""
from __future__ import annotations

import hashlib
import json
import math

import httpx
from py_ecc.bls import G2Basic

CHAIN = '8990e7a9aaed2ffed73dbd7092123d6f289930540d7651336225dc172e51b2ce'
PUBLIC_KEY = '868f005eb8e6e4ca0a47c8a77ceaa5309a47978a7c71bc5cce96366b5d7a569937c529eeda66c7293784a9402801af31'
GENESIS = 1595431050
PERIOD = 30
SCHEME = 'pedersen-bls-chained'
MIRRORS = ('https://api.drand.sh', 'https://api2.drand.sh', 'https://api3.drand.sh')
VERSION = 'dice-pools-v2-drand-v1'


def future_round(deadline):
    return max(1, math.floor((deadline - GENESIS) / PERIOD) + 2)


def round_time(round_number):
    return GENESIS + (round_number - 1) * PERIOD


def verify(data, expected_round):
    if type(data.get('round')) is not int or data['round'] != expected_round or not 1 <= expected_round < 2**64:
        raise ValueError('wrong beacon round')
    try:
        sig = bytes.fromhex(data['signature'])
        previous = bytes.fromhex(data['previous_signature'])
        randomness = bytes.fromhex(data['randomness'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid beacon encoding') from None
    if len(sig) != 96 or len(previous) != 96 or len(randomness) != 32:
        raise ValueError('invalid beacon size')
    digest = hashlib.sha256(previous + expected_round.to_bytes(8, 'big')).digest()
    if not G2Basic.Verify(bytes.fromhex(PUBLIC_KEY), digest, sig):
        raise ValueError('invalid beacon signature')
    if hashlib.sha256(sig).digest() != randomness:
        raise ValueError('invalid beacon randomness')
    return {'round': expected_round, 'signature': sig.hex(), 'previous_signature': previous.hex(), 'randomness': randomness.hex()}


async def fetch(round_number):
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        for mirror in MIRRORS:
            try:
                response = await client.get(f'{mirror}/{CHAIN}/public/{round_number}')
                response.raise_for_status()
                if len(response.content) > 8192:
                    continue
                # CPU verification is off the Telegram event loop.
                import asyncio
                return await asyncio.to_thread(verify, response.json(), round_number)
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                continue
    raise ValueError('fixed beacon unavailable or invalid')


def derive(secret, nonce, beacon):
    seed = hashlib.sha256(json.dumps([VERSION, CHAIN, beacon['round'], secret, nonce, beacon['randomness']], separators=(',', ':'), ensure_ascii=True).encode('ascii')).digest()
    dice = []
    counter = 0
    while len(dice) < 3:
        for value in hashlib.sha256(seed + counter.to_bytes(8, 'big')).digest():
            if value < 252:
                dice.append(value % 6 + 1)
                if len(dice) == 3:
                    break
        counter += 1
    return seed.hex(), dice


def for_row(row):
    """Immutable version dispatch: old rules never acquire new draw semantics."""
    from app.modules import dice_quicknet
    from app.modules.dice_banker import QUICK_BANKER_VERSION
    rules = json.loads(row['rules'])
    if rules.get('version') == QUICK_BANKER_VERSION:
        if (rules.get('draw_version') != dice_quicknet.VERSION or rules.get('chain') != dice_quicknet.CHAIN
                or rules.get('selection') != dice_quicknet.SELECTION):
            raise ValueError('invalid immutable quicknet rules')
        return dice_quicknet
    import sys
    return sys.modules[__name__]
