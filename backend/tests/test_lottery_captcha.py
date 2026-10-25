"""Authenticated challenge -> real Siteverify HTTP client -> atomic entry/debit.

Synthetic credentials, MockTransport and isolated SQLite only. No network calls
are sent to Cloudflare/Telegram and no production data is read or written.
"""
import asyncio
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.core.config import settings
from app.modules import lottery_captcha
from tests.lottery_captcha_helpers import HOST, SECRET, SITE, answer
from tests.test_lottery import ADMIN, BASE, GUEST, MEMBER, browser_id, config, create, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_lottery_count import worker
from tests.test_miniapp import headers

MANUAL = [{'kind': 'manual', 'name': '隔离奖品', 'quantity': 2, 'instructions': '测试领取'}]


@pytest.fixture
def e(request):
    e = request.getfixturevalue('lottery_env')
    e.auto_captcha = False
    for uid in ('u1', 'u2'):
        e.service.points.add(uid, 100, 'admin.mint')
    return e


def activity(e, **kw):
    return create(e, config(**{'draw_mode': 'manual', 'prizes': MANUAL, **kw}))


def challenge(e, lid, tg=MEMBER):
    r = req(e, 'POST', f'/{lid}/captcha', tg)
    assert r.status_code == 200, r.text
    return r.json()


def credentials(e, lid, tg=MEMBER):
    return answer(e.turnstile, challenge(e, lid, tg))


def join(e, lid, tg=MEMBER, **kw):
    return req(e, 'POST', f'/{lid}/join', tg, json={'browser_id': browser_id(tg), **kw})


def snapshot(e):
    return {t: e.service.db.query(f'SELECT * FROM {t} ORDER BY rowid') for t in
            ('lottery_entries', 'lottery_signals', 'lottery_awards', 'lottery_notices', 'lottery_dms', 'points_ledger')}


def change_config(e, lid, **kw):
    c = e.service.row(lid)['config']
    c.update(kw)
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))


def clear_env(monkeypatch, name, value=''):
    monkeypatch.setenv('MEDIADECK_TURNSTILE_' + name, value)
    settings.cache_clear()


@pytest.mark.parametrize(('tg', 'fee'), [(GUEST, 0), (MEMBER, 0), (MEMBER, 40), (ADMIN, 40)])
def test_real_challenge_verify_join_is_private_bound_and_idempotent(e, tg, fee):
    lid = activity(e, audience='member' if fee else 'all', entry_fee=fee)
    view = req(e, 'GET', '/' + lid, tg).json()
    assert view['captcha'] == {'required': True, 'available': True, 'site_key': SITE, 'action': 'lottery_join'}
    c = challenge(e, lid, tg)
    assert set(c) == {'challenge_id', 'cdata', 'site_key', 'action', 'expires_at'}
    assert 298 <= c['expires_at']-time.time() <= 300 and c['challenge_id'] != c['cdata']
    # Opaque random values may contain identity-like substrings by chance.
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,255}', c['cdata'])
    assert c['cdata'] not in {str(tg), lid, 'u1', 'u2'}
    proof = answer(e.turnstile, c)
    result = join(e, lid, tg, expected_entry_fee=fee, **proof)
    assert result.status_code == 200 and result.json()['joined'] and not result.json()['captcha']['required']
    assert len(e.turnstile.calls) == 1
    assert e.turnstile.calls[0] == {'secret': SECRET, 'response': proof['captcha_token'], 'remoteip': e.ip_for(tg)}
    assert e.service.db.one('SELECT state,cdata,lottery_id,tg_id FROM lottery_captcha_challenges WHERE id=?',
                            (c['challenge_id'],)) == {
        'state': 'consumed', 'cdata': c['cdata'], 'lottery_id': lid, 'tg_id': str(tg)}
    before = snapshot(e)
    assert join(e, lid, tg, captcha_token='expired', captcha_challenge_id='bad').status_code == 200
    assert snapshot(e) == before and len(e.turnstile.calls) == 1
    if fee:
        assert e.service.points.balance('u1' if tg == ADMIN else 'u2') == 100-fee


@pytest.mark.parametrize(('name', 'value'), [('SITE_KEY', ''), ('SECRET_KEY', ''), ('HOSTNAMES', ''),
    ('HOSTNAMES', '*.example.test'), ('HOSTNAMES', 'https://app.example.test'), ('HOSTNAMES', 'app.example.test:443'),
    ('HOSTNAMES', 'app.example.test,'), ('SITE_KEY', '<script>')])
def test_missing_or_invalid_configuration_fails_closed_without_leaking_secret(e, monkeypatch, name, value):
    lid = activity(e, audience='member', entry_fee=40)
    clear_env(monkeypatch, name, value)
    before = snapshot(e)
    v = req(e, 'GET', '/' + lid).json()
    assert v['captcha']['required'] and not v['captcha']['available'] and v['captcha']['site_key'] == ''
    for path in ('captcha', 'join'):
        result = req(e, 'POST', f'/{lid}/{path}', json={'expected_entry_fee': 40})
        assert result.status_code == 503 and '暂不可用' in result.text and SECRET not in result.text
    assert snapshot(e) == before and not e.turnstile.calls


@pytest.mark.parametrize('token', [None, '', ' ', True, [], {}, 'x'*2049])
def test_missing_invalid_or_oversized_token_cannot_be_client_bypassed(e, token):
    lid = activity(e, audience='member', entry_fee=40)
    c = challenge(e, lid)
    before = snapshot(e)
    r = join(e, lid, expected_entry_fee=40, captcha_token=token, captcha_challenge_id=c['challenge_id'],
             ready=True, hostname=HOST, user=ADMIN, require_captcha=False)
    assert r.status_code == 400 and not e.turnstile.calls and snapshot(e) == before


@pytest.mark.parametrize(('field', 'value'), [('success', False), ('success', 1), ('hostname', 'evil.test'),
    ('hostname', 'sub.'+HOST), ('hostname', HOST.upper()), ('hostname', None), ('action', 'login'),
    ('action', None), ('cdata', 'forged'), ('cdata', None), ('challenge_ts', 'invalid'),
    ('challenge_ts', '2001-01-01T00:00:00Z'), ('challenge_ts', '2099-01-01T00:00:00Z'),
    ('challenge_ts', '2026-10-25T00:00:00'), ('challenge_ts', None)])
def test_provider_success_requires_exact_all_fields_and_fresh_timestamp(e, field, value):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    before = snapshot(e)
    e.turnstile.override[field] = value
    assert join(e, lid, expected_entry_fee=40, **proof).status_code == 400
    assert snapshot(e) == before and len(e.turnstile.calls) == 1
    e.turnstile.override.clear()
    assert join(e, lid, expected_entry_fee=40, **proof).status_code == 400
    assert len(e.turnstile.calls) == 1


@pytest.mark.parametrize('failure', ['timeout', 'connection', 'http', 'bad_json', 'bad_shape'])
def test_provider_failure_has_no_business_side_effect_or_sensitive_error(e, monkeypatch, caplog, failure):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    before = snapshot(e)
    if failure in ('timeout', 'connection'):
        kind = httpx.ReadTimeout if failure == 'timeout' else httpx.ConnectError
        e.turnstile.error = kind(SECRET + proof['captcha_token'])
    elif failure == 'http':
        e.turnstile.status = 500
    else:
        def reply(request):
            return httpx.Response(200, content=b'{' if failure == 'bad_json' else b'[]')
        monkeypatch.setattr(lottery_captcha, '_client', lambda: httpx.AsyncClient(transport=httpx.MockTransport(reply)))
    r = join(e, lid, expected_entry_fee=40, **proof)
    assert r.status_code == (400 if failure == 'bad_shape' else 503)
    assert snapshot(e) == before
    assert all(s not in r.text+caplog.text for s in (SECRET, proof['captcha_token']))


@pytest.mark.parametrize('binding', ['tg', 'activity', 'site', 'secret', 'hosts', 'bot'])
def test_challenges_cannot_cross_telegram_activity_or_policy(e, monkeypatch, binding):
    lid = activity(e)
    proof = credentials(e, lid)
    target, tg = lid, MEMBER
    if binding == 'tg':
        tg = ADMIN
    elif binding == 'activity':
        target = activity(e)
    elif binding == 'bot':
        monkeypatch.setattr(e.service.bot, '_token', lambda: '123456:rotated-bot-test-token')
    else:
        key, value = {'site': ('SITE_KEY', SITE+'-other'), 'secret': ('SECRET_KEY', SECRET+'-other'),
                      'hosts': ('HOSTNAMES', HOST+',another.example.test')}[binding]
        clear_env(monkeypatch, key, value)
    before = snapshot(e)
    if binding == 'bot':
        assert join(e, target, tg, **proof).status_code == 401  # old initData signature also expires
        with pytest.raises(lottery_captcha.CaptchaError):
            asyncio.run(e.service.join(target, {'id': tg}, source_ip=e.ip_for(tg), browser_id=browser_id(tg), **proof))
    else:
        assert join(e, target, tg, **proof).status_code == 400
    assert not e.turnstile.calls and snapshot(e) == before


@pytest.mark.parametrize('when', ['before_verify', 'during_verify'])
def test_expiration_is_enforced_again_at_final_transaction(e, monkeypatch, when):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    clock = [int(time.time())]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    if when == 'before_verify':
        clock[0] += 300
    else:
        e.turnstile.hook = lambda request: clock.__setitem__(0, clock[0]+300)
    before = snapshot(e)
    assert join(e, lid, expected_entry_fee=40, **proof).status_code == 400
    assert snapshot(e) == before


@pytest.mark.parametrize('problem', ['private', 'prize', 'audience', 'points', 'watch', 'fee', 'draft', 'closed'])
def test_known_public_ineligibility_does_not_issue_useless_challenge(e, problem):
    lid = activity(e, audience='member' if problem in ('points', 'watch', 'fee') else 'all')
    if problem == 'private':
        e.service.db.execute("DELETE FROM meta WHERE key LIKE 'telegram.private:%' OR key LIKE 'telegram.panel:%'")
    elif problem == 'prize':
        change_config(e, lid, prizes=[{'kind': 'registration', 'name': 'test', 'quantity': 1, 'days': 10, 'group_id': 'standard'}])
    elif problem == 'audience':
        change_config(e, lid, audience='guest')
    elif problem == 'points':
        change_config(e, lid, min_points=101)
    elif problem == 'watch':
        change_config(e, lid, min_watch_hours=10)
    elif problem == 'fee':
        change_config(e, lid, entry_fee=101)
    elif problem == 'draft':
        e.service.db.execute("UPDATE lotteries SET state='draft' WHERE id=?", (lid,))
    else:
        e.service.cancel(lid, 'test', confirm=True)
    before = snapshot(e)
    assert req(e, 'POST', f'/{lid}/captcha', MEMBER).status_code == 400
    assert not e.service.db.query('SELECT * FROM lottery_captcha_challenges')
    assert snapshot(e) == before and not e.turnstile.calls


def test_challenge_and_join_require_signed_identity_not_spoofed_payload(e):
    lid = activity(e)
    for suffix in ('captcha', 'join'):
        r = e.client.post(BASE+f'/{lid}/{suffix}', json={'tg_id': MEMBER, 'user': {'id': MEMBER}, 'ready': True})
        assert r.status_code == 401
    assert not e.service.db.query('SELECT * FROM lottery_captcha_challenges')


def test_verified_business_failure_requires_fresh_challenge_no_debit_or_reuse(e):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    before = snapshot(e)
    assert join(e, lid, expected_entry_fee=39, **proof).status_code == 400
    assert snapshot(e) == before
    assert join(e, lid, expected_entry_fee=40, **proof).status_code == 400
    assert len(e.turnstile.calls) == 1
    assert join(e, lid, expected_entry_fee=40, **credentials(e, lid)).status_code == 200
    assert e.service.points.balance('u2') == 60


def test_provider_token_cannot_be_replayed_with_a_new_nonce(e):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    assert join(e, lid, expected_entry_fee=39, **proof).status_code == 400
    proof['captcha_challenge_id'] = challenge(e, lid)['challenge_id']
    assert join(e, lid, expected_entry_fee=40, **proof).status_code == 400
    assert not e.service.db.query('SELECT * FROM lottery_entries') and e.service.points.balance('u2') == 100


def test_sql_failure_rolls_back_consumption_fee_entry_and_associations(e):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    before = snapshot(e)
    e.service.db.execute("CREATE TRIGGER mock_failure AFTER INSERT ON lottery_entries BEGIN SELECT RAISE(ABORT, 'isolated'); END")
    with pytest.raises(sqlite3.DatabaseError):
        asyncio.run(e.service.join(lid, {'id': MEMBER}, source_ip=e.ip_for(MEMBER), browser_id=browser_id(MEMBER),
                                   expected_entry_fee=40, **proof))
    assert snapshot(e) == before
    assert e.service.db.one('SELECT state FROM lottery_captcha_challenges WHERE id=?', (proof['captcha_challenge_id'],))['state'] == 'verifying'
    e.service.db.execute('DROP TRIGGER mock_failure')
    assert join(e, lid, expected_entry_fee=40, **credentials(e, lid)).status_code == 200


def test_provider_network_does_not_hold_writer_and_business_state_rechecked(e):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    with worker(e) as other:
        def during(request):
            other.cancel(lid, 'concurrent-admin', confirm=True)  # cannot run if HTTP holds a writer
        e.turnstile.hook = during
        assert join(e, lid, expected_entry_fee=40, **proof).status_code == 400
    assert e.service.row(lid)['state'] == 'cancelled' and e.service.points.balance('u2') == 100
    assert not e.service.db.query('SELECT * FROM lottery_entries')


def test_policy_enabled_during_unguarded_request_is_rechecked_under_writer(e, monkeypatch):
    lid = activity(e, audience='member', entry_fee=40)
    change_config(e, lid, require_captcha=False)
    original = e.service.bot._call
    async def during(method, payload=None, **kw):
        if method == 'getChatMember':
            change_config(e, lid, require_captcha=True)
        return await original(method, payload, **kw)
    monkeypatch.setattr(e.service.bot, '_call', during)
    before = snapshot(e)
    assert join(e, lid, expected_entry_fee=40).status_code == 400
    assert snapshot(e) == before and not e.turnstile.calls


def test_concurrent_nonce_replay_across_connections_calls_provider_once(e):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    gate = threading.Barrier(6)
    async def slow(request):
        await asyncio.sleep(.05)
    e.turnstile.hook = slow
    with worker(e) as other:
        def submit(service):
            gate.wait()
            try:
                asyncio.run(service.join(lid, {'id': MEMBER}, source_ip=e.ip_for(MEMBER), browser_id=browser_id(MEMBER),
                                         expected_entry_fee=40, **proof))
                return True
            except lottery_captcha.CaptchaError:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, [e.service, other]*3))
    assert any(results) and len(e.turnstile.calls) == 1
    assert e.service.points.balance('u2') == 60 and len(e.service.db.query('SELECT * FROM lottery_entries')) == 1
    assert join(e, lid, **proof).status_code == 200


def test_count_last_entry_with_captcha_is_in_same_unique_result(e):
    lid = activity(e, draw_mode='count', max_entries=2, audience='member', entry_fee=40)
    for tg in (MEMBER, ADMIN):
        result = join(e, lid, tg, expected_entry_fee=40, **credentials(e, lid, tg))
        assert result.status_code == 200
    assert result.json()['state'] == 'settled' and not result.json()['captcha']['required']
    assert {a['tg_id'] for a in e.service.db.query('SELECT tg_id FROM lottery_awards')} == {str(MEMBER), str(ADMIN)}
    before = snapshot(e)
    assert join(e, lid, ADMIN, captcha_token='expired').status_code == 200 and snapshot(e) == before
    assert e.service.points.balance('u1') == e.service.points.balance('u2') == 60


@pytest.mark.parametrize('mode', ['manual', 'timed', 'count'])
def test_already_joined_receipt_works_after_settlement_and_config_outage(e, monkeypatch, mode):
    lid = activity(e, draw_mode=mode, max_entries=1)
    assert join(e, lid, **credentials(e, lid)).status_code == 200
    e.service.draw(lid)
    before = snapshot(e)
    clear_env(monkeypatch, 'SECRET_KEY')
    assert join(e, lid, captcha_token='expired').status_code == 200
    assert snapshot(e) == before and not req(e, 'GET', '/' + lid).json()['captcha']['required']


def test_new_copy_save_and_old_draft_publish_force_captcha_only(e):
    raw = config(draw_mode='manual', prizes=MANUAL, require_captcha=False)
    lid = create(e, raw, publish=False)
    assert e.service.row(lid)['config']['require_captcha'] is True
    stored = json.loads(e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'])
    for key in ('require_captcha', 'require_private_start', 'eligibility_version', 'public_rules_version'):
        stored.pop(key)
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(stored), lid))
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1}).status_code == 200
    published = json.loads(e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'])
    assert published['require_captcha'] is True
    assert 'require_private_start' not in published
    assert published.get('eligibility_version', 1) == 1 and published.get('public_rules_version', 1) == 1
    assert join(e, lid).status_code == 400
    assert join(e, lid, **credentials(e, lid)).status_code == 200
    clone = e.service.copy(lid, str(ADMIN))
    assert e.service.row(clone)['config']['require_captcha'] is True
    e.service.save(raw, str(ADMIN), clone, 1)
    assert e.service.row(clone)['config']['require_captcha'] is True


def test_existing_published_config_not_rewritten_until_explicit_enable_and_existing_entrant_exempt(e):
    lid = activity(e)
    c = e.service.row(lid)['config']; c.pop('require_captcha')
    original = json.dumps(c)
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (original, lid))
    assert not req(e, 'GET', '/' + lid).json()['captcha']['required']
    assert join(e, lid).status_code == 200 and not e.turnstile.calls
    with worker(e):
        pass
    assert e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'] == original
    change_config(e, lid, require_captcha=True)
    assert join(e, lid).status_code == 200 and join(e, lid, ADMIN).status_code == 400
    assert join(e, lid, ADMIN, **credentials(e, lid, ADMIN)).status_code == 200


@pytest.mark.parametrize('state', ['draft', 'cancelled', 'settled', 'deadline', 'full'])
def test_detail_never_prompts_for_captcha_on_closed_entries(e, state):
    lid = activity(e)
    if state in ('draft', 'cancelled', 'settled'):
        e.service.db.execute('UPDATE lotteries SET state=? WHERE id=?', (state, lid))
    elif state == 'deadline':
        change_config(e, lid, draw_mode='timed', deadline=int(time.time())-1)
    else:
        change_config(e, lid, draw_mode='count', max_entries=1)
        e.service.db.execute('INSERT INTO lottery_entries VALUES(?,?,?,?,?,?)', (lid, str(GUEST), 'test', int(time.time()), 0, 1))
    assert req(e, 'GET', '/' + lid, ADMIN).json()['captcha']['required'] is False
    assert req(e, 'POST', f'/{lid}/captcha').status_code == 400


@pytest.mark.parametrize('endpoint,limit', [('captcha', 6), ('join', 12)])
def test_account_activity_limits_return_retry_after_and_recover(e, monkeypatch, endpoint, limit):
    clock = [int(time.time())]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    lid = activity(e)
    for _ in range(limit):
        assert req(e, 'POST', f'/{lid}/{endpoint}').status_code == (200 if endpoint == 'captcha' else 400)
    r = req(e, 'POST', f'/{lid}/{endpoint}')
    assert r.status_code == 429 and int(r.headers['Retry-After']) > 0
    other = activity(e)
    assert req(e, 'POST', f'/{other}/captcha').status_code == 200
    clock[0] += 61
    assert req(e, 'POST', f'/{lid}/captcha').status_code == 200
    assert not e.service.db.query('SELECT * FROM lottery_entries')


def test_shared_ip_limit_is_wider_and_untrusted_headers_do_not_supply_ip(e, monkeypatch):
    assert lottery_captcha.LIMITS['challenge'][1] > lottery_captcha.LIMITS['challenge'][0]*20
    monkeypatch.setattr(lottery_captcha, 'LIMITS', {'challenge': (6, 3), 'join': (12, 300)})
    lid = activity(e)
    shared = e.ip_for(MEMBER)
    for tg in (MEMBER, ADMIN, GUEST):
        e.ips[str(tg)] = shared
        assert req(e, 'POST', f'/{lid}/captcha', tg).status_code == 200
    fourth = 888000444
    e.ips[str(fourth)] = shared
    assert req(e, 'POST', f'/{lid}/captcha', fourth).status_code == 429
    monkeypatch.setenv('MEDIADECK_LOTTERY_IP_MODE', 'disabled'); settings.cache_clear()
    r = e.client.post(BASE+f'/{lid}/captcha', headers={**headers(fourth), 'X-Forwarded-For': shared, 'X-Real-IP': shared})
    assert r.status_code == 200


def test_challenge_capacity_expiry_cleanup_and_restart_persistence(e, monkeypatch):
    monkeypatch.setattr(lottery_captcha, 'MAX_CHALLENGES', 2)
    clock = [int(time.time())]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    lid = activity(e)
    proof = credentials(e, lid)
    challenge(e, lid)
    assert req(e, 'POST', f'/{lid}/captcha').status_code == 429
    with worker(e) as other:
        asyncio.run(other.join(lid, {'id': MEMBER}, source_ip=e.ip_for(MEMBER), browser_id=browser_id(MEMBER), **proof))
        assert other.db.one('SELECT state FROM lottery_captcha_challenges WHERE id=?', (proof['captcha_challenge_id'],))['state'] == 'consumed'
    clock[0] += 301
    asyncio.run(e.service.tick())
    assert not e.service.db.query('SELECT * FROM lottery_captcha_challenges')
    assert not e.service.db.query('SELECT * FROM lottery_captcha_rates')
    assert join(e, lid, captcha_token='expired').status_code == 200


@pytest.mark.parametrize('token_suffix', ['', f'-u2-{MEMBER}'], ids=['random-token', 'identity-like-token'])
def test_no_secret_token_or_identity_in_public_surfaces_persistent_challenge_or_logs(e, caplog, token_suffix):
    lid = activity(e)
    proof = credentials(e, lid)
    if token_suffix:
        # Preserve the random token; deliberately exercise harmless substring collisions.
        original = proof['captcha_token']
        proof['captcha_token'] += token_suffix
        e.turnstile.tokens[proof['captcha_token']] = e.turnstile.tokens.pop(original)
    assert join(e, lid, **proof).status_code == 200
    surfaces = ''.join(req(e, 'GET', path).text for path in ('/'+lid, '/context', '/admin/options', '/ongoing'))
    rows = str(e.service.db.query('SELECT * FROM lottery_captcha_challenges'))
    for secret in (SECRET, proof['captcha_token']):
        assert secret not in surfaces+rows+caplog.text+repr(settings())
    # Exact keys and value sources forbid extra identity fields/content, while
    # treating the provider token as opaque rather than scanning its substrings.
    assert e.turnstile.calls == [{
        'secret': SECRET, 'response': proof['captcha_token'], 'remoteip': e.ip_for(MEMBER)}]


@pytest.mark.parametrize('change', ['expiry', 'site', 'secret'])
def test_verified_proof_rechecked_at_commit_not_just_when_provider_returns(e, monkeypatch, change):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    original = lottery_captcha.verify
    async def after_verified(*args, **kw):
        valid = await original(*args, **kw)
        if change == 'expiry':
            later = int(time.time())+301
            monkeypatch.setattr(time, 'time', lambda: later)
        else:
            clear_env(monkeypatch, 'SITE_KEY' if change == 'site' else 'SECRET_KEY', 'another-valid-config-key')
        return valid
    monkeypatch.setattr(lottery_captcha, 'verify', after_verified)
    before = snapshot(e)
    assert join(e, lid, expected_entry_fee=40, **proof).status_code == 400
    assert snapshot(e) == before
    assert e.service.db.one('SELECT state FROM lottery_captcha_challenges WHERE id=?', (proof['captcha_challenge_id'],))['state'] == 'verifying'


@pytest.mark.parametrize('membership', [None, {'status': 'left'}, {'status': 'restricted', 'is_member': False}])
def test_live_group_membership_still_required_after_challenge(e, membership):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    e.ctl.membership = membership
    before = snapshot(e)
    assert join(e, lid, expected_entry_fee=40, **proof).status_code == 400
    assert snapshot(e) == before and not e.turnstile.calls


def test_legacy_unprotected_published_still_joins_without_environment(e, monkeypatch):
    lid = activity(e)
    c = e.service.row(lid)['config']; c.pop('require_captcha')
    original = json.dumps(c)
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (original, lid))
    clear_env(monkeypatch, 'SECRET_KEY')
    assert req(e, 'GET', '/' + lid).json()['captcha'] == {
        'required': False, 'available': False, 'site_key': '', 'action': 'lottery_join'}
    assert join(e, lid).status_code == 200 and not e.turnstile.calls
    assert e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'] == original


def test_rate_key_storage_is_bounded(e, monkeypatch):
    monkeypatch.setattr(lottery_captcha, 'MAX_RATE_KEYS', 2)
    lid = activity(e)
    challenge(e, lid, MEMBER)  # one TG/activity and one trusted-IP key
    assert req(e, 'POST', f'/{lid}/captcha', ADMIN).status_code == 429
    assert len(e.service.db.query('SELECT * FROM lottery_captcha_rates')) == 2


def test_interrupted_provider_request_is_not_replayed_after_reopening_database(e):
    lid = activity(e, audience='member', entry_fee=40)
    proof = credentials(e, lid)
    async def interrupted():
        started = asyncio.Event()
        async def block(request):
            started.set()
            await asyncio.Future()
        e.turnstile.hook = block
        task = asyncio.create_task(e.service.join(lid, {'id': MEMBER}, source_ip=e.ip_for(MEMBER),
                                                browser_id=browser_id(MEMBER), expected_entry_fee=40, **proof))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    before = snapshot(e)
    asyncio.run(interrupted())
    e.turnstile.hook = None
    with worker(e) as other, pytest.raises(lottery_captcha.CaptchaError):
        asyncio.run(other.join(lid, {'id': MEMBER}, source_ip=e.ip_for(MEMBER), browser_id=browser_id(MEMBER),
                               expected_entry_fee=40, **proof))
    assert snapshot(e) == before and len(e.turnstile.calls) == 1
    assert join(e, lid, expected_entry_fee=40, **credentials(e, lid)).status_code == 200
