"""Pinned drand quicknet, RFC9380 signatures on G1 / public key on G2.

Official info: https://api.drand.sh/v2/beacons/quicknet/info
Scheme: drand/crypto/schemes.go NewPedersenBLSUnchainedG1.
No fallback to another chain or to locally generated randomness.
"""
from __future__ import annotations

import hashlib
import json
import math

import httpx
from py_ecc.bls.g2_primitives import pubkey_to_G1, signature_to_G2, subgroup_check
from py_ecc.bls.hash_to_curve import hash_to_G1
from py_ecc.optimized_bls12_381 import G2, final_exponentiate, is_inf, neg, pairing

CHAIN = '52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971'
PUBLIC_KEY = '83cf0f2896adee7eb8b5f01fcad3912212c437e0073e911fb90022d3e760183c8c4b450b6a0a6c3ac6a5776a2d1064510d1fec758c921cc22b0e17e63aaf4bcb5ed66304de9cf809bd274ca73bab4af5a6e9c76a4bc09e76eae8991ef5ece45a'
GENESIS = 1692803367
PERIOD = 3
SCHEME = 'bls-unchained-g1-rfc9380'
DST = b'BLS_SIG_BLS12381G1_XMD:SHA-256_SSWU_RO_NUL_'
VERSION = 'dice-draw-quicknet-v1'
SAFETY_SECONDS = 2
SELECTION = 'first-round-strictly-after-closed_at-plus-2s'
MIRRORS = ('https://api.drand.sh', 'https://api2.drand.sh', 'https://api3.drand.sh')


def future_round(closed_at):
    # With a synchronized host clock this is always unpublished at selection.
    # Fixed 2s margin covers small clock skew; never choose the latest known round.
    return max(1, math.floor((closed_at + SAFETY_SECONDS - GENESIS) / PERIOD) + 2)


def round_time(round_number):
    return GENESIS + (round_number - 1) * PERIOD


def verify(data, expected_round):
    if type(expected_round) is not int or not 1 <= expected_round < 2**64:
        raise ValueError('invalid expected round')
    if not isinstance(data, dict):
        raise ValueError('invalid beacon object')  # noqa: TRY004 - malformed external data is a verification refusal
    if type(data.get('round')) is not int or data['round'] != expected_round:
        raise ValueError('wrong beacon round')
    if data.get('chain_hash', CHAIN) != CHAIN:
        raise ValueError('wrong beacon chain')
    try:
        sig = bytes.fromhex(data['signature'])
        randomness = bytes.fromhex(data['randomness'])
        if len(sig) != 48 or len(randomness) != 32:
            raise ValueError('invalid beacon size')
        # py-ecc's helper names describe its default G2-signature scheme;
        # here the 48-byte value is a signature and the 96-byte value is a key.
        signature = pubkey_to_G1(sig)
        public_key = signature_to_G2(bytes.fromhex(PUBLIC_KEY))
        if is_inf(signature) or is_inf(public_key) or not subgroup_check(signature) or not subgroup_check(public_key):
            raise ValueError('invalid beacon subgroup')
        digest = hashlib.sha256(expected_round.to_bytes(8, 'big')).digest()
        point = hash_to_G1(digest, DST, hashlib.sha256)
        product = pairing(public_key, point, final_exponentiate=False) * pairing(G2, neg(signature), final_exponentiate=False)
        if final_exponentiate(product) != product.one():
            raise ValueError('invalid beacon signature')
        if hashlib.sha256(sig).digest() != randomness:
            raise ValueError('invalid beacon randomness')
    except (KeyError, TypeError, ValueError, OverflowError, AssertionError) as exc:
        raise ValueError('invalid quicknet beacon') from exc
    return {'round': expected_round, 'signature': sig.hex(), 'randomness': randomness.hex()}


class Unavailable(ValueError):
    def __init__(self, retry_after=0):
        super().__init__('fixed quicknet beacon unavailable')
        self.retry_after = retry_after


async def fetch(round_number):
    retry_after = 0
    async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
        for mirror in MIRRORS:
            try:
                response = await client.get(f'{mirror}/{CHAIN}/public/{round_number}')
                if response.status_code == 429:
                    value = response.headers.get('Retry-After', '')
                    retry_after = max(retry_after, int(value) if value.isdecimal() and len(value) < 10 else 30)
                response.raise_for_status()
                if len(response.content) > 8192:
                    continue
                import asyncio
                return await asyncio.to_thread(verify, response.json(), round_number)
            except (httpx.HTTPError, ValueError, TypeError):
                continue
    raise Unavailable(retry_after)


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
