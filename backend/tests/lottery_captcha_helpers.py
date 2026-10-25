"""Synthetic credentials/HTTP only; never contact Cloudflare with test tokens.

Existing business regressions seed bound challenges to focus on their own
transaction/race. Real verify/consume still run. test_lottery_captcha covers the
actual authenticated issuance endpoint and its rate/capacity limits.
"""
import inspect
import json
import secrets
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from app.core.config import settings
from app.modules import lottery_captcha
from app.modules.economy_rules import economy_write

SITE = 'synthetic-public-site-key'
SECRET = 'synthetic-backend-only-secret-key'
HOST = 'app.example.test'


def install(monkeypatch):
    monkeypatch.setenv('MEDIADECK_TURNSTILE_SITE_KEY', SITE)
    monkeypatch.setenv('MEDIADECK_TURNSTILE_SECRET_KEY', SECRET)
    monkeypatch.setenv('MEDIADECK_TURNSTILE_HOSTNAMES', HOST)
    settings.cache_clear()
    state = SimpleNamespace(tokens={}, used=set(), calls=[], override={}, error=None, hook=None, status=200)
    async def upstream(request):
        assert str(request.url) == lottery_captcha.VERIFY_URL and request.method == 'POST'
        payload = json.loads(request.content)
        state.calls.append(payload)
        if state.hook:
            result = state.hook(request)
            if inspect.isawaitable(result):
                await result
        if state.error:
            raise state.error
        token = payload.get('response')
        challenge = state.tokens.get(token)
        if token in state.used or not challenge or payload['secret'] != SECRET:
            return httpx.Response(200, json={'success': False, 'error-codes': ['invalid-input-response']})
        state.used.add(token)
        body = {'success': True, 'hostname': HOST, 'action': lottery_captcha.ACTION,
                'cdata': challenge['cdata'], 'challenge_ts': datetime.fromtimestamp(time.time(), UTC).isoformat()}
        body.update(state.override)
        return httpx.Response(state.status, json=body)
    monkeypatch.setattr(lottery_captcha, '_client', lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    return state


def answer(state, challenge):
    token = 'synthetic-token-' + secrets.token_urlsafe(24)
    state.tokens[token] = challenge
    return {'captcha_token': token, 'captcha_challenge_id': challenge['challenge_id']}


def captcha_args(service, lid, tg):
    row = service.row(lid)
    if (not row['config'].get('require_captcha') or row['state'] != 'published' or service.db.one(
            'SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lid, str(tg)))):
        return {}
    now = int(time.time())
    cid, cdata = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with economy_write(service.db) as conn:
        conn.execute('INSERT INTO lottery_captcha_challenges(id,cdata,lottery_id,tg_id,policy,created_at,expires_at) '
                     'VALUES(?,?,?,?,?,?,?)', (cid, cdata, lid, str(tg), lottery_captcha.policy(service.bot).fingerprint,
                                              now, now+300))
    return answer(service.bot._test_turnstile, {'challenge_id': cid, 'cdata': cdata})
