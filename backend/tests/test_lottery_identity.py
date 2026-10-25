"""Strict lottery association exclusion; all data/transport are local mocks."""
import asyncio
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.requests import Request

from app.core.config import settings
from app.core.db import Database
from app.modules.lottery_identity import public_ip, trusted_ip
from tests.lottery_captcha_helpers import captcha_args
from tests.test_lottery import (
    BASE,
    GUEST,
    MEMBER,
    browser_id,
    draw,
    join,
    req,
    synthetic_public_ip,
)
from tests.test_lottery import config as base_config
from tests.test_lottery import create as base_create
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import headers

OTHER, UNIQUE = 888000444, 888000555


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def config(**kw):
    # New activities use a universal prize for identity-independent risk tests.
    return base_config(**{'prizes': [{'kind': 'manual', 'name': '人工奖', 'quantity': 3,
                                     'instructions': '私密领奖'}], **kw})


def create(e, payload=None, publish=True):
    return base_create(e, payload or config(), publish)


def signup(e, lid, tg, ip=None, device=None, extra=None):
    return e.client.post(BASE + f'/{lid}/join', headers={**headers(tg), **({'CF-Connecting-IP': ip} if ip else {}),
                                                       **(extra or {})},
                         json={'browser_id': browser_id(tg) if device is None else device,
                               **captcha_args(e.service, lid, tg)})


@pytest.mark.parametrize('reason', ['ip', 'device', 'both'])
def test_all_linked_accounts_excluded_including_first_and_no_award_bypass(e, reason):
    prizes = [{'kind': 'registration', 'name': '本人资格', 'quantity': 2, 'group_id': 'standard', 'days': 30},
              {'kind': 'points', 'name': '积分', 'quantity': 2, 'amount': 100}]
    lid = create(e, config(prizes=prizes))
    first_ip = e.ip_for(GUEST)
    assert signup(e, lid, GUEST, first_ip).json()['joined'] is True
    second_ip = first_ip if reason in ('ip', 'both') else e.ip_for(OTHER)
    second_device = browser_id(GUEST) if reason in ('device', 'both') else browser_id(OTHER)
    second = signup(e, lid, OTHER, second_ip, second_device)
    assert second.status_code == 200 and second.json()['joined']
    assert 'qualification' not in second.json()
    first = req(e, 'GET', '/' + lid, GUEST).json()
    assert first['count'] == 2 and first == second.json()
    assert e.service.view(lid, str(GUEST), admin=True)['qualification']['excluded'] is True
    join(e, lid, MEMBER)
    join(e, lid, UNIQUE)
    records = req(e, 'GET', f'/{lid}/records').json()
    assert records['total'] == 4 and records['excluded'] == 2
    assert len(records['clusters']) == 1
    cluster = records['clusters'][0]
    assert set(cluster['reasons']) == ({'ip', 'device'} if reason == 'both' else {reason})
    assert {a['tg_id'] for a in cluster['accounts']} == {str(GUEST), str(OTHER)}
    assert all(a['excluded'] for a in cluster['accounts'])
    d = draw(e, lid)
    assert len(d['winners']) == 2
    ids = {r['tg_id'] for r in e.service.db.query('SELECT tg_id FROM lottery_awards WHERE lottery_id=?', (lid,))}
    assert ids == {str(MEMBER), str(UNIQUE)}
    assert e.service.db.one('SELECT tg_id FROM lottery_admissions')['tg_id'] == str(UNIQUE)
    for tg in (GUEST, OTHER):
        assert req(e, 'POST', f'/{lid}/claim', tg).status_code == 404
        assert req(e, 'GET', f'/{lid}/records', tg).status_code == 403
        public = req(e, 'GET', '/' + lid, tg).json()
        assert 'qualification' not in public
        assert 'clusters' not in public and 'entries' not in public and 'excluded_count' not in public
        assert str(OTHER) not in json.dumps(public) and str(GUEST) not in json.dumps(public)
    assert req(e, 'GET', f'/{lid}/records', MEMBER).status_code == 403
    assert e.client.get(BASE + f'/{lid}/records').status_code == 401
    raw_values = [first_ip, second_ip, browser_id(GUEST), second_device]
    for payload in (json.dumps(records), json.dumps(d), str(e.calls)):
        assert all(value not in payload for value in raw_values)
    final = req(e, 'GET', f'/{lid}/records').json()
    assert {r['outcome'] for r in final['entries'] if r['excluded']} == {'excluded'}
    assert {r['outcome'] for r in final['entries'] if not r['excluded']} == {'winner'}


def test_repeat_same_tg_never_replaces_evidence_or_clears_exclusion(e):
    lid = create(e)
    join(e, lid, GUEST)
    before = e.service.db.query('SELECT * FROM lottery_signals WHERE lottery_id=?', (lid,))
    for _ in range(3):
        assert signup(e, lid, GUEST, synthetic_public_ip(), browser_id('changed')).status_code == 200
    assert e.service.db.query('SELECT * FROM lottery_signals WHERE lottery_id=?', (lid,)) == before
    assert e.service.view(lid, str(GUEST), admin=True)['qualification']['excluded'] is False
    assert signup(e, lid, OTHER, e.ip_for(GUEST)).status_code == 200
    assert signup(e, lid, GUEST, e.ip_for(UNIQUE), browser_id(UNIQUE)).json()['joined']
    assert e.service.view(lid, str(GUEST), admin=True)['qualification']['excluded']
    assert e.service.view(lid, str(GUEST))['count'] == 2
    assert e.service.records(lid)['excluded'] == 2


def test_transitive_cluster_explains_each_reason_and_keeps_normal_user(e):
    lid = create(e)
    join(e, lid, GUEST)
    signup(e, lid, OTHER, e.ip_for(GUEST))
    signup(e, lid, UNIQUE, e.ip_for(UNIQUE), browser_id(OTHER))
    join(e, lid, MEMBER)
    result = e.service.records(lid)
    assert result['excluded'] == 3 and len(result['clusters']) == 1
    c = result['clusters'][0]
    assert len(c['accounts']) == 3 and c['reasons'] == ['device', 'ip']
    assert {tuple(link['tg_ids']) for link in c['links']} == {(str(GUEST), str(OTHER)), (str(OTHER), str(UNIQUE))}
    assert len(draw(e, lid)['winners']) == 1


@pytest.mark.parametrize('reason', ['ip', 'device'])
def test_concurrent_same_signal_different_connections_both_excluded(e, reason):
    lid = create(e)
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    gate = threading.Barrier(2)
    ip = e.ip_for(GUEST)

    def enter(service, tg):
        gate.wait()
        asyncio.run(service.join(lid, {'id': tg, 'first_name': 'Participant'}, **captcha_args(service, lid, tg),
                                 source_ip=ip if reason == 'ip' else e.ip_for(tg),
                                 browser_id=browser_id(GUEST if reason == 'device' else tg)))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f = [pool.submit(enter, e.service, GUEST), pool.submit(enter, other, OTHER)]
            for future in f:
                future.result()
        assert e.service.records(lid)['excluded'] == 2
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda s: s.draw(lid), [e.service, other]))
        assert e.service.view(lid, str(GUEST))['winners'] == []
    finally:
        other.db.close()


def test_join_draw_race_is_serializable_not_retroactive(e):
    for _ in range(6):
        lid = create(e)
        join(e, lid, GUEST)
        other = copy.copy(e.service)
        other.db = Database(e.service.db.path)
        gate = threading.Barrier(2)

        def enter(gate=gate, other=other, lid=lid):
            gate.wait()
            try:
                asyncio.run(other.join(lid, {'id': OTHER, 'first_name': 'Other'}, **captcha_args(other, lid, OTHER),
                                       source_ip=e.ip_for(GUEST), browser_id=browser_id(OTHER)))
                return True
            except ValueError:
                return False

        def settle(gate=gate, lid=lid):
            gate.wait()
            e.service.draw(lid)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f = pool.submit(enter)
                g = pool.submit(settle)
                accepted = f.result()
                g.result()
            result = e.service.records(lid)
            winners = e.service.view(lid, str(GUEST))['winners']
            if accepted:
                assert result['total'] == result['excluded'] == 2 and not winners
            else:
                assert result['total'] == 1 and result['excluded'] == 0 and len(winners) == 1
            before = e.service.db.query('SELECT * FROM lottery_entries WHERE lottery_id=?', (lid,))
            assert signup(e, lid, OTHER, e.ip_for(GUEST)).status_code == 400
            assert before == e.service.db.query('SELECT * FROM lottery_entries WHERE lottery_id=?', (lid,))
        finally:
            other.db.close()


def test_membership_network_reply_after_deadline_cannot_change_qualification(e, monkeypatch):
    lid = create(e)
    join(e, lid, GUEST)
    deadline = e.service.row(lid)['config']['deadline']
    original = e.service.bot._call

    async def delayed(method, payload=None, **kw):
        result = await original(method, payload, **kw)
        if method == 'getChatMember':
            monkeypatch.setattr(time, 'time', lambda: deadline)
        return result
    monkeypatch.setattr(e.service.bot, '_call', delayed)
    assert signup(e, lid, OTHER, e.ip_for(GUEST)).status_code == 400
    assert e.service.records(lid)['total'] == 1 and e.service.records(lid)['excluded'] == 0
    assert len(draw(e, lid)['winners']) == 1


def test_activity_domain_and_restart_key_persistence(e):
    a, b = create(e), create(e)
    join(e, a, GUEST)
    join(e, b, GUEST)
    a_sig = e.service.db.query('SELECT kind,digest FROM lottery_signals WHERE lottery_id=? ORDER BY kind', (a,))
    b_sig = e.service.db.query('SELECT kind,digest FROM lottery_signals WHERE lottery_id=? ORDER BY kind', (b,))
    assert all(x['digest'] != y['digest'] for x, y in zip(a_sig, b_sig))
    key = e.service.db.one('SELECT secret FROM lottery_identity_key')['secret']
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    try:
        asyncio.run(other.join(a, {'id': OTHER, 'first_name': 'Other'}, **captcha_args(other, a, OTHER), source_ip=e.ip_for(GUEST), browser_id=browser_id(OTHER)))
        assert other.db.one('SELECT secret FROM lottery_identity_key')['secret'] == key
        assert other.records(a)['excluded'] == 2 and other.records(b)['excluded'] == 0
        serialized = str(other.db.query('SELECT * FROM lottery_signals'))
        assert e.ip_for(GUEST) not in serialized and browser_id(GUEST) not in serialized
    finally:
        other.db.close()


def test_missing_or_corrupt_key_never_silently_rotates(e):
    lid = create(e)
    join(e, lid, GUEST)
    key = e.service.db.one('SELECT secret FROM lottery_identity_key')['secret']
    e.service.db.execute('DELETE FROM lottery_identity_key')
    failed = signup(e, lid, OTHER, e.ip_for(GUEST))
    assert failed.status_code == 400 and '报名暂不可用' in failed.text
    assert e.service.records(lid)['total'] == 1
    assert e.service.db.one('SELECT * FROM lottery_identity_key') is None
    e.service.db.execute('INSERT INTO lottery_identity_key VALUES(1,?)', ('invalid',))
    assert signup(e, lid, OTHER, e.ip_for(GUEST)).status_code == 400
    e.service.db.execute('UPDATE lottery_identity_key SET secret=? WHERE id=1', (key,))
    assert signup(e, lid, OTHER, e.ip_for(GUEST)).json()['joined']
    assert e.service.records(lid)['excluded'] == 2


def test_default_strict_freeze_off_mode_no_identifiers_and_legacy_migration(e, monkeypatch):
    lid = create(e, publish=False)
    assert e.service.row(lid)['config']['anti_multi'] is True
    assert req(e, 'PUT', '/' + lid, json={**config(anti_multi='true'), 'revision': 1}).status_code == 400
    assert req(e, 'PUT', '/' + lid, json={**config(anti_multi=False), 'revision': 1}).status_code == 200
    req(e, 'POST', f'/{lid}/publish', json={'revision': 2})
    monkeypatch.setattr(settings(), 'mediadeck_lottery_ip_mode', 'disabled')
    assert signup(e, lid, GUEST, device='').status_code == 200
    assert signup(e, lid, OTHER, device='').status_code == 200
    assert not e.service.db.query('SELECT * FROM lottery_signals WHERE lottery_id=?', (lid,))
    assert e.service.records(lid)['excluded'] == 0
    assert req(e, 'PUT', '/' + lid, json={**config(anti_multi=True), 'revision': 2}).status_code == 400
    strict = create(e)
    assert req(e, 'PUT', '/' + strict, json={**config(anti_multi=False), 'revision': 1}).status_code == 400
    # An already published pre-risk lottery must not silently acquire new rules.
    raw = e.service.row(lid)['config']
    raw.pop('anti_multi')
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(raw), lid))
    migrated = Database(e.service.db.path)
    try:
        assert json.loads(migrated.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'])['anti_multi'] is False
    finally:
        migrated.close()


@pytest.mark.parametrize('raw', [None, '', '127.0.0.1', '10.0.0.1', '192.168.1.1', '169.254.1.1',
                                 '100.64.0.1', '0.0.0.0', '255.255.255.255', '224.0.0.1',
                                 '::1', 'fe80::1', 'ff02::1', '2001:db8::1', '192.0.2.1',
                                 'example.test', '192.0.2.1:80', '192.0.2.1,198.51.100.1',
                                 ' 192.0.2.1', '::ffff:127.0.0.1', 'fe80::1%eth0'])
def test_public_ip_rejects_non_public_malformed_and_multiple(raw):
    assert public_ip(raw) is None


def test_cf_mode_uses_explicit_ingress_assertion_not_rewritten_scope_or_forged_xff():
    ip, forged = synthetic_public_ip(), synthetic_public_ip()

    def request(client, hdrs):
        return Request({'type': 'http', 'method': 'POST', 'path': '/', 'client': (client, 1),
                        'headers': [(k.encode(), v.encode()) for k, v in hdrs]})
    # Uvicorn may have replaced socket peer with XFF. Protected CF mode ignores
    # that value entirely; deployment MUST enforce the promised ingress boundary.
    r = request(forged, [('cf-connecting-ip', ip), ('x-forwarded-for', forged)])
    assert trusted_ip(r, 'cloudflare_protected') == ip
    assert trusted_ip(r, 'disabled') is None
    assert trusted_ip(r, 'unknown') is None
    assert trusted_ip(request('127.0.0.1', [('x-forwarded-for', ip)]), 'cloudflare_protected') is None
    assert trusted_ip(request('127.0.0.1', [('cf-connecting-ip', ip), ('cf-connecting-ip', forged)]), 'cloudflare_protected') is None
    assert public_ip('::ffff:' + ip) == ip


def test_strict_missing_ip_forged_headers_and_missing_browser_fail_closed(e, monkeypatch):
    lid = create(e)
    for mode, extra in [('disabled', {'CF-Connecting-IP': e.ip_for(GUEST)}),
                        ('cloudflare_protected', {'X-Forwarded-For': e.ip_for(GUEST), 'X-Real-IP': e.ip_for(GUEST)}),
                        ('cloudflare_protected', {'CF-Connecting-IP': '127.0.0.1'})]:
        monkeypatch.setattr(settings(), 'mediadeck_lottery_ip_mode', mode)
        r = e.client.post(BASE + f'/{lid}/join?ip_mode=cloudflare_protected',
                          headers={**headers(GUEST), **extra},
                          json={'browser_id': browser_id(GUEST), 'ip': e.ip_for(GUEST), 'is_admin': True,
                                **captcha_args(e.service, lid, GUEST)})
        assert r.status_code == 400 and '报名暂不可用' in r.text
    monkeypatch.setattr(settings(), 'mediadeck_lottery_ip_mode', 'cloudflare_protected')
    for device in ('', 'same-device', 'a' * 65):
        assert signup(e, lid, GUEST, e.ip_for(GUEST), device).status_code == 400
    assert e.service.records(lid)['total'] == 0
    assert not e.service.db.query('SELECT * FROM lottery_signals')
    assert not e.service.db.query('SELECT * FROM lottery_identity_key')
    assert signup(e, lid, GUEST, e.ip_for(GUEST)).status_code == 200


def test_records_pagination_and_no_normal_user_disclosure(e):
    lid = create(e)
    # Fixture rows for roster pagination; no network or real identities.
    with e.service.db.write() as conn:
        for i in range(52):
            conn.execute('INSERT INTO lottery_entries(lottery_id,tg_id,label,joined_at,registered) VALUES(?,?,?,?,?)',
                         (lid, str(900000000 + i), 'Fixture participant', int(time.time()), 0))
    first = req(e, 'GET', f'/{lid}/records').json()
    second = req(e, 'GET', f'/{lid}/records?offset=50').json()
    assert first['total'] == 52 and first['more'] and len(first['entries']) == 50
    assert not second['more'] and len(second['entries']) == 2
    public = req(e, 'GET', '/' + lid, GUEST).json()
    assert 'Fixture participant' not in json.dumps(public)


def test_public_views_identical_for_linked_and_unlinked_entries(e):
    lid = create(e, config(prizes=[{'kind': 'manual', 'name': '人工奖', 'quantity': 1, 'instructions': '私密领奖'}]))
    join(e, lid, GUEST)
    signup(e, lid, OTHER, e.ip_for(GUEST))
    join(e, lid, UNIQUE)
    responses = [req(e, 'GET', '/' + lid, tg).json() for tg in (GUEST, OTHER, UNIQUE)]
    assert responses[0] == responses[1] == responses[2]
    assert responses[0]['count'] == 3 and responses[0]['joined']
    forbidden = {'risk', 'risk_flag', 'qualification', 'excluded', 'excluded_count', 'candidate_count',
                 'eligible_count', 'clusters', 'signals', 'digest', 'anti_multi', 'tg_id'}

    def assert_safe(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                assert_safe(child)
        elif isinstance(value, list):
            for child in value:
                assert_safe(child)
    for tg in (GUEST, OTHER, UNIQUE):
        assert_safe(req(e, 'POST', f'/{lid}/join', tg).json())
    admin = req(e, 'GET', '/' + lid).json()
    assert admin['excluded_count'] == 2 and admin['candidate_count'] == 1
    draw(e, lid)
    for tg in (GUEST, OTHER, UNIQUE):
        assert_safe(req(e, 'GET', '/' + lid, tg).json())
    # Excluded guest registering later cannot acquire any reward via claim.
    e.service.members.upsert('late-bound', 'private-fixture', {'group_id': 'standard'})
    e.service.db.execute('UPDATE members SET tg_user_id=? WHERE emby_user_id=?', (str(GUEST), 'late-bound'))
    assert req(e, 'POST', f'/{lid}/claim', GUEST).status_code == 404
    assert e.service.view(lid, str(GUEST))['my_award'] is None
    sent = json.dumps(e.calls, ensure_ascii=False)
    assert all(word not in sent for word in ('异常', '关联', '排除', 'browser_id', 'digest'))
