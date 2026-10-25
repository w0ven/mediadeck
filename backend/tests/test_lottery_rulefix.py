"""Retire environment checks for new lotteries without rewriting legacy policies.

Real isolated SQLite/services/API, synthetic environment data and mock Telegram.
"""
import copy
import json
import time

import pytest

from app.core.db import Database
from app.modules import lottery_device
from tests.test_lottery import ADMIN, BASE, GUEST, MEMBER, browser_id, create, draw, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_lottery_device import (
    CONSENT,
    OTHER,
    THIRD,
    UNSET,
    assert_public,
    device_rows,
    enter,
    legacy_create,
)
from tests.test_lottery_device import strict as legacy_strict
from tests.test_miniapp import headers


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def strict(**kw):
    # Neutral prize so both identities can exercise NEW environment-retirement rules.
    return legacy_strict(**{'prizes': [{'kind': 'manual', 'name': '人工奖', 'quantity': 3,
                                       'instructions': '仅中奖可见'}], **kw})


def stored_config(e, lid):
    return json.loads(e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'])


@pytest.mark.parametrize('submitted', [UNSET, False, True])
@pytest.mark.parametrize('anti_multi', [False, True])
def test_new_create_edit_and_publish_cannot_enable_retired_check(e, submitted, anti_multi):
    raw = strict(anti_multi=anti_multi)
    if submitted is not UNSET:
        raw['device_check'] = submitted
    # Client-declared versions/legacy markers and nested configs are not authority.
    raw.update(legacy=True, legacy_device_check=True, config_version='v0.52.0',
               stored={'device_check': True}, config={'device_check': True})
    assert e.service.validate(raw)['device_check'] is False
    lid = create(e, raw, publish=False)
    assert stored_config(e, lid)['device_check'] is False
    assert 'consent' not in req(e, 'GET', '/' + lid).json()
    for revision, switch in enumerate((False, True), start=1):
        edited = req(e, 'PUT', '/' + lid, json={**raw, 'revision': revision, 'device_check': True,
                                               'anti_multi': switch})
        assert edited.status_code == 200 and edited.json()['config']['device_check'] is False
        assert stored_config(e, lid)['device_check'] is False
    result = req(e, 'POST', f'/{lid}/publish', json={'revision': 3, 'device_check': True, 'legacy': True})
    assert result.status_code == 200 and result.json()['config']['device_check'] is False
    assert 'consent' not in result.json()
    assert req(e, 'POST', f'/{lid}/join', GUEST).json()['joined']
    assert not device_rows(e, lid)


@pytest.mark.parametrize('previous', [UNSET, False, True])
@pytest.mark.parametrize('submitted', [UNSET, False, True])
def test_existing_draft_policy_comes_only_from_stored_record(e, previous, submitted):
    lid = legacy_create(e, publish=False)
    old = stored_config(e, lid)
    if previous is UNSET:
        old.pop('device_check')  # actual pre-feature draft, no device-check field
    else:
        old['device_check'] = previous
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(old), lid))
    raw = strict(title='仅改标题', revision=1, legacy=True, stored={'device_check': True})
    if submitted is not UNSET:
        raw['device_check'] = submitted
    edited = req(e, 'PUT', '/' + lid, json=raw)
    assert edited.status_code == 200
    expected = previous is True
    assert stored_config(e, lid)['device_check'] is expected
    assert ('consent' in edited.json()) is expected
    # A stale request must not alter either the policy or other saved fields.
    before = stored_config(e, lid)
    assert req(e, 'PUT', '/' + lid, json={**raw, 'device_check': not expected}).status_code == 400
    assert stored_config(e, lid) == before
    published = req(e, 'POST', f'/{lid}/publish', json={'revision': 2, 'device_check': not expected})
    assert published.status_code == 200
    assert stored_config(e, lid)['device_check'] is expected
    response = req(e, 'POST', f'/{lid}/join', GUEST)
    if expected:
        assert response.status_code == 400 and response.json()['detail'] == lottery_device.NEED_CONSENT
        assert req(e, 'GET', '/' + lid, GUEST).json()['consent'] == CONSENT
        assert enter(e, lid, GUEST).status_code == 200 and len(device_rows(e, lid)) == 1
    else:
        assert response.status_code == 200 and response.json()['joined']
        assert not device_rows(e, lid)


@pytest.mark.parametrize('state', ['draft', 'published', 'settled', 'cancelled'])
@pytest.mark.parametrize('previous', [False, True])
def test_copy_is_a_new_policy_even_for_legacy_activity(e, state, previous):
    source = legacy_create(e, strict(device_check=previous), publish=state != 'draft')
    if state == 'settled':
        assert enter(e, source, GUEST).status_code == 200
        draw(e, source)
    elif state == 'cancelled':
        assert req(e, 'POST', f'/{source}/cancel', json={'confirm': True}).status_code == 200
    before = e.service.row(source)
    old_devices = device_rows(e, source)
    response = req(e, 'POST', f'/{source}/copy', json={'device_check': True, 'legacy': True})
    assert response.status_code == 200
    copied = response.json()
    lid = copied['id']
    assert lid != source and copied['state'] == 'draft'
    assert stored_config(e, lid)['device_check'] is False and copied['config']['anti_multi'] is True
    assert 'consent' not in copied and not device_rows(e, lid)
    # An old cached edit form cannot turn the copied legacy feature back on.
    raw = {**stored_config(e, lid), 'deadline': int(time.time()) + 3600, 'device_check': True, 'revision': 1}
    assert req(e, 'PUT', '/' + lid, json=raw).status_code == 200
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 2}).status_code == 200
    assert req(e, 'POST', f'/{lid}/join', OTHER).status_code == 200
    assert stored_config(e, lid)['device_check'] is False and not device_rows(e, lid)
    assert e.service.row(source) == before and device_rows(e, source) == old_devices


def test_new_free_guest_registration_activity_needs_no_environment_consent_or_blob(e):
    lid = create(e, strict(audience='guest', prizes=[{
        'kind': 'registration', 'name': '本人资格', 'quantity': 2, 'group_id': 'standard', 'days': 10,
    }]))
    for tg in (GUEST, OTHER):
        result = req(e, 'POST', f'/{lid}/join', tg)
        assert result.status_code == 200 and result.json()['joined']
        assert 'consent' not in result.json()
        assert_public(result.json())
    assert len(draw(e, lid)['winners']) == 2
    assert {r['tg_id'] for r in e.service.db.query('SELECT tg_id FROM lottery_admissions')} == {str(GUEST), str(OTHER)}
    assert not device_rows(e, lid)
    assert not e.service.db.query('SELECT * FROM points_ledger')


def test_same_environment_is_ignored_without_decoding_and_cannot_exclude_guests(e, monkeypatch):
    lid = create(e, strict(audience='guest'))

    def never_unpack(_raw):
        pytest.fail('New activities must not decode unsolicited environment data')
    monkeypatch.setattr(lottery_device, 'unpack', never_unpack)
    # Equal environment AND ctx.k are ignored: only the separate installation
    # browser_id field and the trusted IP are used for these new activities.
    for tg in (GUEST, OTHER):
        result = enter(e, lid, tg, seed='same-environment', k=browser_id(THIRD))
        assert result.status_code == 200 and result.json()['joined'] and 'consent' not in result.json()
    assert enter(e, lid, THIRD, ctx='not-even-a-blob', consent='not-consent').status_code == 200
    views = [req(e, 'GET', '/' + lid, tg).json() for tg in (GUEST, OTHER, THIRD)]
    assert views[0] == views[1] == views[2]
    for view in views:
        assert_public(view)
    records = req(e, 'GET', f'/{lid}/records').json()
    assert records['total'] == 3 and records['excluded'] == 0 and records['clusters'] == []
    assert 'device_check' not in records and 'device_observations' not in records
    assert not device_rows(e, lid)
    assert {r['kind'] for r in e.service.db.query('SELECT kind FROM lottery_signals WHERE lottery_id=?', (lid,))} == {'ip', 'device'}
    assert len(draw(e, lid)['winners']) == 3
    assert not device_rows(e, lid)


@pytest.mark.parametrize('reason', ['ip', 'device'])
def test_new_rule_still_excludes_every_linked_account_silently(e, reason):
    lid = create(e, strict(audience='guest'))
    if reason == 'ip':
        e.ips[str(OTHER)] = e.ip_for(GUEST)
    for tg in (GUEST, OTHER, THIRD):
        token = browser_id(GUEST if reason == 'device' and tg == OTHER else tg)
        response = req(e, 'POST', f'/{lid}/join', tg, json={'browser_id': token})
        assert response.status_code == 200 and response.json()['joined']
        assert_public(response.json())
    first = req(e, 'GET', '/' + lid, GUEST).json()
    assert first == req(e, 'GET', '/' + lid, OTHER).json() == req(e, 'GET', '/' + lid, THIRD).json()
    records = req(e, 'GET', f'/{lid}/records').json()
    assert records['excluded'] == 2 and len(records['clusters']) == 1
    cluster = records['clusters'][0]
    assert cluster['reasons'] == [reason]
    assert {a['tg_id'] for a in cluster['accounts']} == {str(GUEST), str(OTHER)}
    assert all(a['excluded'] for a in cluster['accounts'])
    assert 'device_observations' not in records and not device_rows(e, lid)
    assert req(e, 'GET', f'/{lid}/records', GUEST).status_code == 403
    draw(e, lid)
    assert e.service.db.one('SELECT tg_id FROM lottery_awards WHERE lottery_id=?', (lid,))['tg_id'] == str(THIRD)
    for tg in (GUEST, OTHER):
        assert req(e, 'POST', f'/{lid}/claim', tg).status_code == 404
        view = req(e, 'GET', '/' + lid, tg).json()
        assert view['my_award'] is None
        assert_public(view)


def test_new_strict_still_requires_trusted_ip_and_installation_id(e):
    lid = create(e, strict())
    missing_browser = req(e, 'POST', f'/{lid}/join', GUEST, json={})
    assert missing_browser.status_code == 400
    for extra in ({}, {'X-Forwarded-For': e.ip_for(GUEST), 'X-Real-IP': e.ip_for(GUEST)}):
        missing_ip = e.client.post(BASE + f'/{lid}/join', headers={**headers(GUEST), **extra},
                                   json={'browser_id': browser_id(GUEST)})
        assert missing_ip.status_code == 400
    assert not e.service.db.query('SELECT * FROM lottery_entries WHERE lottery_id=?', (lid,))
    assert req(e, 'POST', f'/{lid}/join', GUEST).status_code == 200
    assert not device_rows(e, lid)


def test_old_published_and_settled_data_never_rewritten_or_refunded(e):
    lid = legacy_create(e, strict(entry_fee=40))
    for tg, uid in ((ADMIN, 'u1'), (MEMBER, 'u2')):
        e.service.points.add(uid, 100, 'admin.mint', 'legacy-test')
        assert enter(e, lid, tg, seed='old-environment', extra={'expected_entry_fee': 40}).status_code == 200
    published_config = stored_config(e, lid)
    assert req(e, 'PUT', '/' + lid, json={**published_config, 'device_check': False, 'revision': 1}).status_code == 400
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1, 'device_check': False}).status_code == 200
    assert stored_config(e, lid) == published_config
    assert draw(e, lid)['winners'] == []
    assert {r['verdict'] for r in device_rows(e, lid)} == {'same'}
    original = copy.deepcopy(e.service.row(lid))
    tables = ('lottery_entries', 'lottery_device_entries', 'lottery_signals', 'lottery_notices', 'lottery_awards')
    snapshots = {table: e.service.db.query(f'SELECT * FROM {table} WHERE lottery_id=?', (lid,)) for table in tables}
    ledger = e.service.db.query('SELECT * FROM points_ledger ORDER BY id')
    # Opening/migrating the same isolated DB, viewing/copying the old activity
    # and retrying draw/join may not recalculate historical qualifications.
    reopened = Database(e.service.db.path)
    reopened.close()
    assert req(e, 'POST', f'/{lid}/copy').status_code == 200
    e.service.draw(lid)
    assert req(e, 'POST', f'/{lid}/join', OTHER).status_code == 400
    records = req(e, 'GET', f'/{lid}/records').json()
    assert records['excluded'] == 2
    assert records['clusters'][0]['labels'] == ['设备环境特征相同']
    link = records['clusters'][0]['links'][0]
    assert link['status'] == 'same' and link['excluded'] is True
    assert link['status_label'] == '设备环境特征相同，按本期原规则全部排除'
    assert e.service.row(lid) == original
    for table, snapshot in snapshots.items():
        assert e.service.db.query(f'SELECT * FROM {table} WHERE lottery_id=?', (lid,)) == snapshot
    assert e.service.db.query('SELECT * FROM points_ledger ORDER BY id') == ledger
    assert e.service.points.balance('u1') == e.service.points.balance('u2') == 60
    assert req(e, 'GET', '/' + lid, MEMBER).json()['my_award'] is None
