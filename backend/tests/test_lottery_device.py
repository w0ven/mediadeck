"""Consented device check: real SQLite/services/APIs, synthetic device values, mock Telegram only."""
import json
import secrets
import time

import pytest

from app.core.db import Database
from app.modules import lottery_device
from app.modules.lottery_device import CONSENT_VERSION, FAILED, NEED_CONSENT
from tests.lottery_device_helpers import blob, device_ctx, traits
from tests.test_lottery import BASE, GUEST, MEMBER, browser_id, config, create, draw, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import headers

OTHER, THIRD, FOURTH, FIFTH, SIXTH = 888000444, 888000555, 888000666, 888000777, 888000888
UNSET = object()
CONSENT = {'version': 1, 'title': '设备校验',
           'body': ('参加本活动需完成设备校验：报名时将读取本设备的屏幕、显卡、系统等信息，生成不可逆的摘要，'
                    '仅用于本期活动的参与资格校验，不作其他用途。不同意将无法参加本活动。'),
           'accept': '同意并报名', 'decline': '不同意'}
FORBIDDEN = {'risk', 'qualification', 'excluded', 'excluded_count', 'candidate_count', 'clusters', 'signals',
             'digest', 'anti_multi', 'device_check', 'admin_rules', 'verdict', 'tg_id', 'hardware'}


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def strict(**kw):
    """A new strict activity as the current admin form submits it (device_check only when given)."""
    raw = config(**kw)
    if 'device_check' not in kw:
        raw.pop('device_check')
    return raw


def enter(e, lid, tg, *, ip=None, seed=None, k=None, s=None, consent=CONSENT_VERSION, ctx=UNSET, extra=None):
    body = {'browser_id': browser_id(tg), **(extra or {})}
    if consent is not UNSET:
        body['consent_version'] = consent
    body['ctx'] = device_ctx(tg, seed, k=k, s=s) if ctx is UNSET else ctx
    if body['ctx'] is None:
        body.pop('ctx')
    return e.client.post(BASE + f'/{lid}/join', json=body,
                         headers={**headers(tg), 'CF-Connecting-IP': ip or e.ip_for(tg)})


def device_rows(e, lid):
    return e.service.db.query('SELECT * FROM lottery_device_entries WHERE lottery_id=? ORDER BY tg_id', (lid,))


def assert_public(value):
    if isinstance(value, dict):
        assert not FORBIDDEN.intersection(value), FORBIDDEN.intersection(value)
        for child in value.values():
            assert_public(child)
    elif isinstance(value, list):
        for child in value:
            assert_public(child)


def test_strict_default_consent_text_and_off_switches(e):
    lid = create(e, strict(), publish=False)
    assert e.service.row(lid)['config']['device_check'] is True
    admin = req(e, 'GET', '/' + lid).json()
    assert admin['consent'] == CONSENT and admin['admin_rules'].endswith(lottery_device.RULE)
    assert lottery_device.RULE not in admin['rules']  # the admin's participant view shows public rules only
    assert all(word not in CONSENT['body'] for word in ('多账号', '小号', '重复'))
    assert req(e, 'POST', '/admin/drafts', json=strict(device_check='true')).status_code == 400
    loose = create(e, strict(anti_multi=False, device_check=True), publish=False)
    assert e.service.row(loose)['config']['device_check'] is False  # only exists inside strict mode
    assert 'consent' not in req(e, 'GET', '/' + loose).json()
    opted_out = create(e, strict(device_check=False), publish=False)
    assert e.service.row(opted_out)['config']['device_check'] is False
    req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
    assert req(e, 'PUT', '/' + lid, json={**strict(device_check=False), 'revision': 1}).status_code == 400
    public = req(e, 'GET', '/' + lid, GUEST).json()
    assert public['consent'] == CONSENT and 'device_check' not in public['config']
    assert lottery_device.RULE not in public['rules']


def test_missing_or_stale_consent_rejected_without_charge(e):
    lid = create(e, strict(entry_fee=40))
    e.service.points.add('u2', 100, 'admin.mint', 'test-mint')
    for consent in (UNSET, None, 0, 2, '1', True, 1.0):
        r = enter(e, lid, MEMBER, consent=consent, extra={'expected_entry_fee': 40})
        assert r.status_code == 400 and r.json()['detail'] == NEED_CONSENT
    assert e.service.points.balance('u2') == 100
    assert not e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.entry'")
    for table in ('lottery_entries', 'lottery_signals', 'lottery_device_entries', 'lottery_identity_key'):
        assert not e.service.db.query(f'SELECT * FROM {table}')


@pytest.mark.parametrize('ctx', [
    None, '', 'not base64 !', 'x' * 9000, blob(['v', 1]), blob({'v': 2, 's': traits('a')}),
    blob({'v': 1, 'k': browser_id(MEMBER)}),
    blob({'v': 1, 's': {k: v for k, v in traits('a').items() if k in 'fghij'}}),  # no rendering component
    blob({'v': 1, 's': {k: v for k, v in traits('a').items() if k in 'abce'}}),  # fewer than five values
    blob({'v': 1, 's': {**{k: 'not-a-digest' for k in 'acen'}, 'f': 'wide', 'g': True}}),
])
def test_failed_collection_rejected_without_charge(e, ctx):
    lid = create(e, strict(entry_fee=40))
    e.service.points.add('u2', 100, 'admin.mint', 'test-mint')
    r = enter(e, lid, MEMBER, ctx=ctx, extra={'expected_entry_fee': 40})
    assert r.status_code == 400 and r.json()['detail'] == FAILED
    assert e.service.points.balance('u2') == 100
    assert not e.service.db.query('SELECT * FROM lottery_entries') and not device_rows(e, lid)


def test_consented_entry_records_time_version_and_charges_once(e):
    lid = create(e, strict(entry_fee=40))
    e.service.points.add('u2', 100, 'admin.mint', 'test-mint')
    before = int(time.time())
    r = enter(e, lid, MEMBER, extra={'expected_entry_fee': 40})
    after = int(time.time())
    assert r.status_code == 200 and r.json()['joined'] and r.json()['consent'] == CONSENT
    [row] = device_rows(e, lid)
    assert row['consent_version'] == CONSENT_VERSION and before <= row['consented_at'] <= after
    assert row['tg_id'] == str(MEMBER) and row['verdict'] == ''
    assert e.service.points.balance('u2') == 60
    # Retried request (even without consent) is the idempotent receipt, not a second entry.
    assert enter(e, lid, MEMBER, consent=UNSET, extra={'expected_entry_fee': 40}).status_code == 200
    assert e.service.points.balance('u2') == 60 and len(device_rows(e, lid)) == 1


def test_same_device_links_accounts_across_ips_and_clients(e):
    lid = create(e, strict(prizes=[{'kind': 'points', 'name': '积分', 'quantity': 3, 'amount': 5}]))
    # Same device, different networks, different installation ids (another Telegram client).
    assert enter(e, lid, GUEST, seed='phone').status_code == 200
    assert enter(e, lid, OTHER, seed='phone').status_code == 200
    assert enter(e, lid, THIRD, seed='laptop').status_code == 200
    assert len({e.ip_for(t) for t in (GUEST, OTHER, THIRD)}) == 3
    records = req(e, 'GET', f'/{lid}/records').json()
    assert records['total'] == 3 and records['excluded'] == 2 and records['device_check'] is True
    [cluster] = records['clusters']
    assert cluster['reasons'] == ['hardware'] and cluster['labels'] == ['相同设备']
    assert {a['tg_id'] for a in cluster['accounts']} == {str(GUEST), str(OTHER)}
    assert all(a['excluded'] for a in cluster['accounts'])  # the first entrant too
    [link] = cluster['links']
    assert link['label'] == '相同设备' and link['status'] == 'same'
    assert {r['tg_id']: r['outcome'] for r in records['entries']} == {
        str(GUEST): 'excluded', str(OTHER): 'excluded', str(THIRD): 'eligible'}
    admin = req(e, 'GET', '/' + lid).json()
    assert admin['excluded_count'] == 2 and admin['candidate_count'] == 1
    assert e.service.view(lid, str(GUEST), admin=True)['qualification']['excluded'] is True
    # Ordinary participants: identical responses, no verdict anywhere.
    views = [req(e, 'GET', '/' + lid, t).json() for t in (GUEST, OTHER, THIRD)]
    assert views[0] == views[1] == views[2]
    for value in views + [enter(e, lid, t).json() for t in (GUEST, OTHER, THIRD)]:
        assert_public(value)
    assert req(e, 'GET', f'/{lid}/records', GUEST).status_code == 403
    result = draw(e, lid)
    assert [w['label'] for w in result['winners']] == ['Viewer']
    assert {r['tg_id'] for r in e.service.db.query('SELECT tg_id FROM lottery_awards WHERE lottery_id=?', (lid,))} == {
        str(THIRD)}
    assert {r['tg_id'] for r in e.service.db.query(
        'SELECT tg_id FROM lottery_entries WHERE lottery_id=? AND excluded=1', (lid,))} == {str(GUEST), str(OTHER)}
    assert {r['verdict'] for r in device_rows(e, lid)} == {'same', ''}
    settled = [req(e, 'GET', '/' + lid, t).json() for t in (GUEST, OTHER, THIRD)]
    assert settled[0] == settled[1] and settled[0]['my_award'] is None and settled[2]['my_award']
    for value in settled:
        assert_public(value)
    for tg in (GUEST, OTHER):
        assert req(e, 'POST', f'/{lid}/claim', tg).status_code == 404
    sent = json.dumps(e.calls, ensure_ascii=False)
    assert all(word not in sent for word in ('设备', '异常', '排除', 'ctx'))


def test_each_activity_judged_alone_from_its_first_entries(e):
    # The very first device-checked activity already excludes: no warm-up period.
    first = create(e, strict(prizes=[{'kind': 'points', 'name': '积分', 'quantity': 3, 'amount': 5}]))
    for tg in (THIRD, FOURTH):
        assert enter(e, first, tg, seed='phone').status_code == 200
    assert enter(e, first, FIFTH, seed='laptop').status_code == 200
    assert e.service.records(first)['excluded'] == 2
    # The same device in a later activity is not linked to the earlier one.
    lid = create(e, strict())
    assert enter(e, lid, GUEST, seed='phone').status_code == 200
    assert enter(e, lid, OTHER, seed='tablet').status_code == 200
    records = e.service.records(lid)
    assert records['excluded'] == 0 and records['clusters'] == []
    assert len(draw(e, lid)['winners']) == 2
    assert [w['label'] for w in draw(e, first)['winners']] == ['Viewer']


def test_iphone_entries_keep_no_device_digest(e):
    iphone = {k: v for k, v in traits('iphone').items() if k != 'h'} | {'b': 'Apple Inc.|Apple GPU', 'i': '5'}
    lid = create(e, strict(prizes=[{'kind': 'points', 'name': '积分', 'quantity': 4, 'amount': 5}]))
    for tg in (GUEST, OTHER):
        assert enter(e, lid, tg, s=iphone).status_code == 200
    for tg in (THIRD, FOURTH):
        assert enter(e, lid, tg, seed='android').status_code == 200
    assert {r['tg_id'] for r in device_rows(e, lid)} == {str(THIRD), str(FOURTH)}
    records = e.service.records(lid)
    assert records['excluded'] == 2 and [sorted(a['tg_id'] for a in c['accounts']) for c in records['clusters']] == [
        sorted([str(THIRD), str(FOURTH)])]
    assert len(draw(e, lid)['winners']) == 2
    assert lottery_device.apple_mobile(json.dumps({k: v for k, v in iphone.items() if k != 'b'}))
    assert not lottery_device.apple_mobile(json.dumps({**iphone, 'i': '0'}))  # a Mac
    assert not lottery_device.apple_mobile(json.dumps(traits('android')))


def test_draw_transaction_freezes_the_decision(e):
    lid = create(e, strict())
    for tg in (GUEST, OTHER):
        enter(e, lid, tg, seed='phone')
    assert {r['verdict'] for r in device_rows(e, lid)} == {''}
    assert e.service.records(lid)['excluded'] == 2  # live verdict before the draw
    assert draw(e, lid)['winners'] == []
    assert {r['verdict'] for r in device_rows(e, lid)} == {'same'}
    assert {r['tg_id'] for r in e.service.db.query(
        'SELECT tg_id FROM lottery_entries WHERE lottery_id=? AND excluded=1', (lid,))} == {str(GUEST), str(OTHER)}
    records = e.service.records(lid)
    assert records['excluded'] == 2 and records['clusters'][0]['labels'] == ['相同设备']
    assert {r['outcome'] for r in records['entries']} == {'excluded'}


def test_switched_off_activity_is_unchanged_and_collects_nothing(e):
    for payload in (config(), config(anti_multi=False), strict(device_check=False)):
        lid = create(e, payload)
        assert 'consent' not in req(e, 'GET', '/' + lid, GUEST).json()
        r = e.client.post(BASE + f'/{lid}/join', json={'browser_id': browser_id(GUEST)},
                          headers={**headers(GUEST), 'CF-Connecting-IP': e.ip_for(GUEST)})
        assert r.status_code == 200 and 'consent' not in r.json()
        # Even a client that sends consent + device data has it ignored, not stored.
        assert enter(e, lid, OTHER, seed='phone').status_code == 200
        records = e.service.records(lid)
        assert 'device_check' not in records and 'device_observations' not in records
    # Legacy published activity without the field: no consent, no collection.
    legacy = create(e)
    raw = e.service.row(legacy)['config']
    raw.pop('device_check')
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(raw), legacy))
    assert e.client.post(BASE + f'/{legacy}/join', json={'browser_id': browser_id(GUEST)},
                         headers={**headers(GUEST), 'CF-Connecting-IP': e.ip_for(GUEST)}).status_code == 200
    assert not e.service.db.query('SELECT * FROM lottery_device_entries')
    migrated = Database(e.service.db.path)
    try:
        assert 'device_check' not in json.loads(
            migrated.one('SELECT config_json FROM lotteries WHERE id=?', (legacy,))['config_json'])
    finally:
        migrated.close()


def test_coexists_with_ip_and_browser_rules(e):
    lid = create(e, strict(prizes=[{'kind': 'points', 'name': '积分', 'quantity': 3, 'amount': 5}]))
    shared_ip, shared_k = e.ip_for(GUEST), secrets.token_hex(32)
    assert enter(e, lid, GUEST, seed='g').status_code == 200
    assert enter(e, lid, OTHER, ip=shared_ip, seed='o').status_code == 200           # same IP as GUEST
    assert enter(e, lid, THIRD, seed='o').status_code == 200                          # same device as OTHER
    assert enter(e, lid, FOURTH, seed='p', k=shared_k).status_code == 200
    assert enter(e, lid, FIFTH, seed='q', k=shared_k).status_code == 200              # ctx.k: same installation
    assert enter(e, lid, SIXTH, seed='clean').status_code == 200
    records = e.service.records(lid)
    assert records['excluded'] == 5
    clusters = {frozenset(a['tg_id'] for a in c['accounts']): c for c in records['clusters']}
    triple = clusters[frozenset({str(GUEST), str(OTHER), str(THIRD)})]
    assert triple['reasons'] == ['hardware', 'ip'] and triple['labels'] == ['相同设备', '相同可信公网 IP']
    assert {link['label'] for link in triple['links']} == {'相同设备', '相同可信公网 IP'}
    pair = clusters[frozenset({str(FOURTH), str(FIFTH)})]
    assert pair['reasons'] == ['device'] and pair['labels'] == ['相同浏览器安装标识']
    assert all(a['excluded'] for c in records['clusters'] for a in c['accounts'])
    assert len(draw(e, lid)['winners']) == 1
    assert e.service.db.one('SELECT tg_id FROM lottery_awards WHERE lottery_id=?', (lid,))['tg_id'] == str(SIXTH)


def test_raw_device_values_never_persisted_or_returned(e):
    lid = create(e, strict())
    k = secrets.token_hex(32)
    values = traits('raw-check')
    contexts = [device_ctx(GUEST, k=k, s=values), device_ctx(OTHER, s=values)]
    for tg, ctx in zip((GUEST, OTHER), contexts):
        assert enter(e, lid, tg, ctx=ctx).status_code == 200
    responses = [req(e, 'GET', f'/{lid}/records').text, req(e, 'GET', '/' + lid).text]
    draw(e, lid)
    responses += [req(e, 'GET', f'/{lid}/records').text, req(e, 'GET', '/' + lid, GUEST).text]
    dump = '\n'.join(e.service.db._conn.iterdump())
    raw = [*values.values(), k, browser_id(OTHER), *contexts]
    for value in raw:
        if value in ('8', '4', '5'):
            continue  # bare small numbers occur everywhere; covered by the canonical HMAC input
        assert value not in dump, value
        assert all(value not in text for text in responses), value
    stored = [r['digest'] for r in device_rows(e, lid)]
    assert all(digest not in text for digest in stored for text in responses)


def test_lost_key_never_rotates_while_device_digests_exist(e):
    lid = create(e, strict())
    without = create(e, strict(device_check=False))  # strict, but no device check
    assert enter(e, lid, GUEST).status_code == 200
    e.service.db.execute('DELETE FROM lottery_identity_key')
    e.service.db.execute('DELETE FROM lottery_signals')  # only device digests remain
    for target in (lid, without):
        r = enter(e, target, OTHER)
        assert r.status_code == 400 and '报名暂不可用' in r.json()['detail']
    assert e.service.db.one('SELECT * FROM lottery_identity_key') is None
    assert e.service.db.one('SELECT COUNT(*) AS n FROM lottery_entries')['n'] == 1


def test_unpack_contract():
    s = {**traits('x'), 'g': 8, 'h': 0.5, 'l': 'zh-CN', 'ua': 'Mozilla/5.0', 'b': 'x|' + 'y' * 300}
    token, trait = lottery_device.unpack(blob({'v': 1, 'k': 'A' * 64, 's': s}))
    assert token is None
    canonical = json.loads(trait)
    assert set(canonical) == set('acefghijn') and canonical['g'] == '8' and canonical['h'] == '0.5'
    assert lottery_device.unpack(blob({'v': 1, 'k': 'a' * 64, 's': traits('x')}))[0] == 'a' * 64
    assert lottery_device.unpack(blob({'v': 1, 's': traits('x')})) == lottery_device.unpack(
        blob({'v': 1, 's': dict(reversed(list(traits('x').items())))}))
