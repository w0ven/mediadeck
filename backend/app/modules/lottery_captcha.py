"""Lottery-only Turnstile: opaque, bound, single-use challenges; no credential logs.

Siteverify happens outside the SQLite writer. A reservation prevents concurrent
verification/replay; consume() must share the final admission/debit transaction.
Failed business checks require a fresh challenge, never a second token charge.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.core.config import settings
from app.modules.economy_rules import economy_write
from app.modules.lottery_identity import public_ip
from app.modules.telegram_private import identity

ACTION = 'lottery_join'
TTL = 300
VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
MAX_CHALLENGES = 20000
MAX_RATE_KEYS = 50000
WINDOW = 60
LIMITS = {'challenge': (6, 300), 'join': (12, 300)}  # TG/activity, wider shared-IP budget
INVALID = '验证已失效或不匹配，请重新完成验证后报名'
UNAVAILABLE = '报名验证暂不可用，请联系管理员或稍后重试（尚未报名）'

SCHEMA = """
CREATE TABLE IF NOT EXISTS lottery_captcha_challenges (
 id TEXT PRIMARY KEY, cdata TEXT NOT NULL, lottery_id TEXT NOT NULL, tg_id TEXT NOT NULL,
 policy TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'issued', reservation TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lottery_captcha_expiry ON lottery_captcha_challenges(expires_at);
CREATE TABLE IF NOT EXISTS lottery_captcha_rates (
 scope TEXT PRIMARY KEY, hits INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lottery_captcha_rate_expiry ON lottery_captcha_rates(expires_at);
"""


class CaptchaError(ValueError):
    def __init__(self, message=INVALID, *, status=400, retry_after=None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


@dataclass(frozen=True)
class Policy:
    site_key: str
    secret: str = field(repr=False)
    hosts: tuple[str, ...]
    fingerprint: str = field(repr=False)

    @property
    def available(self):
        return bool(self.fingerprint)


@dataclass(frozen=True)
class Proof:
    challenge_id: str
    reservation: str
    policy: str


def policy(bot):
    cfg = settings()
    site = cfg.mediadeck_turnstile_site_key.strip()
    secret = cfg.mediadeck_turnstile_secret_key.get_secret_value().strip()
    hosts = tuple(sorted({h.strip().lower() for h in cfg.mediadeck_turnstile_hostnames.split(',')}))
    valid_hosts = all(len(h) <= 253 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p)
                                          for p in h.split('.')) for h in hosts)
    valid = (identity(bot) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', site)
             and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret) and valid_hosts)
    fingerprint = hashlib.sha256(json.dumps([site, secret, hosts, bot._token(), ACTION]).encode()).hexdigest() if valid else ''
    return Policy(site if valid else '', secret, hosts, fingerprint)


def public(bot, required):
    cfg = policy(bot)
    return {'required': bool(required), 'available': cfg.available, 'site_key': cfg.site_key, 'action': ACTION}


def cleanup(conn, now):
    conn.execute('DELETE FROM lottery_captcha_challenges WHERE expires_at<=?', (now,))
    conn.execute('DELETE FROM lottery_captcha_rates WHERE expires_at<=?', (now,))


def prune(db):
    with economy_write(db) as conn:
        cleanup(conn, int(time.time()))


def _limit(conn, kind, lottery_id, tg_id, source_ip, now):
    scopes = [(f'{kind}:tg:{lottery_id}:{tg_id}', LIMITS[kind][0])]
    address = public_ip(source_ip)
    if address:
        scopes.append((f'{kind}:ip:{address}', LIMITS[kind][1]))
    expiry = (now // WINDOW + 1) * WINDOW
    for name, limit in scopes:
        key = hashlib.sha256(name.encode()).hexdigest()
        row = conn.execute('SELECT hits FROM lottery_captcha_rates WHERE scope=?', (key,)).fetchone()
        if row and row['hits'] >= limit:
            raise CaptchaError('操作过于频繁，请稍后重试（尚未报名）', status=429, retry_after=max(1, expiry-now))
        if not row and conn.execute('SELECT COUNT(*) FROM lottery_captcha_rates').fetchone()[0] >= MAX_RATE_KEYS:
            raise CaptchaError('验证请求较多，请稍后重试', status=429, retry_after=60)
        conn.execute('INSERT INTO lottery_captcha_rates(scope,hits,expires_at) VALUES(?,1,?) '
                     'ON CONFLICT(scope) DO UPDATE SET hits=hits+1', (key, expiry))


def throttle(db, kind, lottery_id, tg_id, source_ip):
    now = int(time.time())
    with economy_write(db) as conn:
        cleanup(conn, now)
        _limit(conn, kind, lottery_id, tg_id, source_ip, now)


def issue(db, bot, lottery_id, tg_id, source_ip):
    cfg = policy(bot)
    if not cfg.available:
        raise CaptchaError(UNAVAILABLE, status=503)
    now = int(time.time())
    challenge_id, cdata = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with economy_write(db) as conn:
        cleanup(conn, now)
        if conn.execute('SELECT COUNT(*) FROM lottery_captcha_challenges').fetchone()[0] >= MAX_CHALLENGES:
            raise CaptchaError('验证请求较多，请稍后重试', status=429, retry_after=60)
        conn.execute('INSERT INTO lottery_captcha_challenges(id,cdata,lottery_id,tg_id,policy,created_at,expires_at) '
                     'VALUES(?,?,?,?,?,?,?)', (challenge_id, cdata, lottery_id, tg_id, cfg.fingerprint, now, now+TTL))
    return {'challenge_id': challenge_id, 'cdata': cdata, 'site_key': cfg.site_key, 'action': ACTION, 'expires_at': now+TTL}


def _client():
    return httpx.AsyncClient(timeout=8.0, follow_redirects=False, trust_env=False)


async def verify(db, bot, lottery_id, tg_id, challenge_id, token, source_ip):
    cfg = policy(bot)
    if not cfg.available:
        raise CaptchaError(UNAVAILABLE, status=503)
    if (not isinstance(token, str) or not 1 <= len(token) <= 2048 or not token.strip()
            or not isinstance(challenge_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', challenge_id)):
        raise CaptchaError('请先完成人机验证后报名')
    reservation = secrets.token_urlsafe(24)
    with economy_write(db) as conn:
        row = conn.execute("SELECT * FROM lottery_captcha_challenges WHERE id=? AND lottery_id=? AND tg_id=? "
                           "AND policy=? AND expires_at>? AND state='issued'",
                           (challenge_id, lottery_id, tg_id, cfg.fingerprint, int(time.time()))).fetchone()
        if not row:
            raise CaptchaError()
        conn.execute("UPDATE lottery_captcha_challenges SET state='verifying',reservation=? WHERE id=?",
                     (reservation, challenge_id))
        challenge = dict(row)
    # Do not persist/send TG identity, account identity, raw token, secret, or a
    # provider error body anywhere except the token/secret in this HTTPS POST.
    payload = {'secret': cfg.secret, 'response': token}
    address = public_ip(source_ip)
    if address:
        payload['remoteip'] = address
    try:
        async with _client() as client:
            response = await client.post(VERIFY_URL, json=payload)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError):
        raise CaptchaError(UNAVAILABLE, status=503) from None
    if not isinstance(result, dict) or result.get('success') is not True:
        raise CaptchaError()
    if (result.get('hostname') not in cfg.hosts or result.get('action') != ACTION
            or result.get('cdata') != challenge['cdata']):
        raise CaptchaError()
    try:
        stamp = datetime.fromisoformat(result['challenge_ts'])
        issued = stamp.timestamp() if stamp.tzinfo is not None else 0
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise CaptchaError() from None
    now = time.time()
    if not challenge['created_at']-30 <= issued <= now+30 or now-issued > TTL or now >= challenge['expires_at']:
        raise CaptchaError()
    return Proof(challenge_id, reservation, cfg.fingerprint)


def consume(conn, bot, proof, lottery_id, tg_id, now):
    """Only called after verify(), in the same transaction as the final entry."""
    cfg = policy(bot)
    if not cfg.available:
        raise CaptchaError(UNAVAILABLE, status=503)
    if not isinstance(proof, Proof) or proof.policy != cfg.fingerprint:
        raise CaptchaError()
    changed = conn.execute("UPDATE lottery_captcha_challenges SET state='consumed' WHERE id=? AND lottery_id=? "
                           "AND tg_id=? AND policy=? AND reservation=? AND expires_at>? AND state='verifying'",
                           (proof.challenge_id, lottery_id, tg_id, cfg.fingerprint, proof.reservation, now)).rowcount
    if changed != 1:
        raise CaptchaError()
