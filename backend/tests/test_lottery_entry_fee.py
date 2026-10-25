"""Atomic lottery entry fees; local database and mocked Telegram only."""
import asyncio
import copy
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.db import Database
from app.modules import lottery_identity
from app.modules.lottery import entry_terms
from app.modules.points import PointsService
from tests.lottery_captcha_helpers import captcha_args
from tests.lottery_legacy_helpers import legacy_create
from tests.test_lottery import ADMIN, BASE, GUEST, MEMBER, browser_id, create, draw, req
from tests.test_lottery import config as base_config
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import headers


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def config(**kw):
    # Only member activities can have fees under the new public participation rules.
    return base_config(**{'audience': 'member', **kw})


def fund(e, uid='u2', amount=100):
    e.service.points.add(uid, amount, 'admin.mint', 'mock-entry-fee-test')


def fees(e):
    return e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.entry' ORDER BY id")


def enter(e, lid, user=MEMBER, fee=40, **extra):
    return req(e, 'POST', f'/{lid}/join', user,
               json={'browser_id': browser_id(user), 'expected_entry_fee': fee, **extra})


def assert_empty(e, lid, balance=100):
    assert e.service.points.balance('u2') == balance
    assert fees(e) == []
    assert e.service.db.query('SELECT * FROM lottery_entries WHERE lottery_id=?', (lid,)) == []
    assert e.service.db.query('SELECT * FROM lottery_signals WHERE lottery_id=?', (lid,)) == []


@pytest.mark.parametrize('fee', [True, False, -1, 100001, 1.5, '40', None, [], {}])
def test_fee_config_rejects_invalid_types_and_bounds(e, fee):
    result = req(e, 'POST', '/admin/drafts', json=config(entry_fee=fee))
    assert result.status_code == 400 and '报名积分' in result.json()['detail']
    assert not e.service.db.query('SELECT * FROM lotteries')


@pytest.mark.parametrize('fee', [0, 1, 100000])
def test_valid_fee_config_saved_and_frozen(e, fee):
    lid = create(e, config(entry_fee=fee), publish=False)
    assert e.service.row(lid)['config']['entry_fee'] == fee
    changed = 1 if fee == 0 else 0
    update = req(e, 'PUT', '/' + lid, json={**config(entry_fee=changed), 'revision': 1})
    assert update.status_code == 200 and update.json()['config']['entry_fee'] == changed
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1}).status_code == 400
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 2}).status_code == 200
    assert req(e, 'PUT', '/' + lid, json={**config(entry_fee=fee), 'revision': 2}).status_code == 400
    assert req(e, 'GET', '/' + lid, MEMBER).json()['config']['entry_fee'] == changed


@pytest.mark.parametrize('audience', ['all', 'member'])
def test_legacy_paid_all_and_new_member_supported_without_rewriting_audience(e, audience):
    fund(e)
    maker = legacy_create if audience == 'all' else create
    lid = maker(e, config(entry_fee=40, audience=audience))
    assert enter(e, lid).json()['joined']
    assert e.service.row(lid)['config']['audience'] == audience
    result = enter(e, lid, GUEST)
    assert result.status_code == 400
    assert len(fees(e)) == 1 and e.service.view(lid, str(GUEST))['count'] == 1


def test_fee_guest_and_registration_prize_incompatible_but_free_unchanged(e):
    result = req(e, 'POST', '/admin/drafts', json=config(entry_fee=40, audience='guest'))
    assert result.status_code == 400 and '仅未注册' in result.json()['detail']
    registration = [{'kind': 'registration', 'name': '本人资格', 'quantity': 2, 'group_id': 'standard', 'days': 10}]
    result = req(e, 'POST', '/admin/drafts', json=config(entry_fee=40, prizes=registration))
    assert result.status_code == 400 and '注册资格' in result.json()['detail']
    lid = create(e, config(entry_fee=0, audience='all', prizes=registration))
    assert enter(e, lid, MEMBER, fee=0).status_code == 400
    assert enter(e, lid, GUEST, fee=0).status_code == 200
    draw(e, lid)
    assert {r['tg_id'] for r in e.service.db.query('SELECT tg_id FROM lottery_awards')} == {str(GUEST)}
    assert fees(e) == []


def test_publish_revalidates_fee_restrictions(e):
    lid = legacy_create(e, config(audience='guest'), publish=False)
    c = e.service.row(lid)['config']
    c['entry_fee'] = 40
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))
    result = req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
    assert result.status_code == 400 and e.service.row(lid)['state'] == 'draft'
    assert not e.service.db.query('SELECT * FROM lottery_notices')


def test_old_free_configuration_join_and_notices_left_intact(e):
    lid = legacy_create(e)
    c = e.service.row(lid)['config']
    c.pop('entry_fee')
    saved = json.dumps(c)
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (saved, lid))
    before = e.service.db.query('SELECT * FROM lottery_notices')
    for user in (MEMBER, GUEST, ADMIN):
        view = req(e, 'GET', '/' + lid, user).json()
        assert view['config']['entry_fee'] == 0 and '免费参与' in view['rules']
        assert req(e, 'POST', f'/{lid}/join', user).json()['joined']
    assert fees(e) == [] and e.service.points.balance('u2') == 0
    assert e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'] == saved
    assert e.service.db.query('SELECT * FROM lottery_notices') == before


def test_context_returns_only_own_nonnegative_balance_and_guest_null(e):
    fund(e, amount=87)
    fund(e, uid='u1', amount=1234)
    for user, balance in ((MEMBER, 87), (ADMIN, 1234), (GUEST, None)):
        result = req(e, 'GET', '/context', user).json()
        assert result['points_balance'] == balance
        assert set(result) == {'points_balance', 'is_admin', 'registered', 'name', 'bot_username'}
    assert e.client.get(BASE + '/context').status_code == 401
    assert e.client.get('/api/app/me', headers=headers(GUEST)).status_code == 403
    e.service.points.add('u2', -90, 'checkin')
    assert req(e, 'GET', '/context', MEMBER).json()['points_balance'] == 0
    lid = create(e, config(entry_fee=1))
    assert enter(e, lid, fee=1).status_code == 400
    assert not fees(e)


@pytest.mark.parametrize('expected', [None, 0, 39, 41, True, False, '40', 40.0, -1, [], {}])
def test_expected_fee_must_be_explicit_exact_integer(e, expected):
    fund(e)
    lid = create(e, config(entry_fee=40))
    assert enter(e, lid, fee=expected).status_code == 400
    assert_empty(e, lid)


def test_missing_expected_fee_paid_rejected_and_free_old_client_allowed(e):
    fund(e)
    lid = create(e, config(entry_fee=40))
    assert req(e, 'POST', f'/{lid}/join', MEMBER).status_code == 400
    assert_empty(e, lid)
    free = create(e, config(entry_fee=0))
    assert req(e, 'POST', f'/{free}/join', MEMBER).status_code == 200
    assert fees(e) == []


def test_debit_traceability_and_retries_even_without_balance_or_confirmation(e):
    fund(e, amount=40)
    lid = create(e, config(entry_fee=40))
    assert enter(e, lid, points_balance=99999, is_admin=True).json()['joined']
    assert e.service.points.balance('u2') == 0
    f = fees(e)
    assert len(f) == 1 and f[0]['delta'] == -40 and f[0]['balance_after'] == 0
    assert f[0]['emby_user_id'] == 'u2' and f[0]['actor'] == str(MEMBER)
    assert f[0]['ref'] == f'lottery.entry:{lid}:{MEMBER}'
    assert req(e, 'GET', '/context', MEMBER).json()['points_balance'] == 0
    for body in ({}, {'expected_entry_fee': 999}, {'expected_entry_fee': 40}):
        assert req(e, 'POST', f'/{lid}/join', MEMBER, json=body).json()['joined']
    assert fees(e) == f and e.service.view(lid, str(MEMBER))['count'] == 1
    # Idempotency belongs to TG+lottery, not the current bound account.
    e.service.db.execute("UPDATE members SET tg_user_id='' WHERE emby_user_id='u2'")
    assert req(e, 'POST', f'/{lid}/join', MEMBER, json={}).json()['joined']
    assert fees(e) == f


def test_insufficient_balance_no_entry_or_ledger_even_with_spoofed_balance(e):
    fund(e, amount=39)
    lid = create(e, config(entry_fee=40))
    result = enter(e, lid, points_balance=100000, role='admin', emby_user_id='u1')
    assert result.status_code == 400 and '积分不足' in result.json()['detail']
    assert_empty(e, lid, balance=39)


def test_admin_participates_and_pays_without_exemption(e):
    lid = create(e, config(entry_fee=40))
    assert enter(e, lid, ADMIN).status_code == 400
    fund(e, uid='u1', amount=40)
    assert enter(e, lid, ADMIN).json()['joined']
    assert e.service.points.balance('u1') == 0 and fees(e)[0]['emby_user_id'] == 'u1'
    assert enter(e, lid, ADMIN).status_code == 200 and len(fees(e)) == 1


@pytest.mark.parametrize('fault', ['group_unknown', 'not_member', 'expired', 'audience', 'device', 'ip'])
def test_failed_admission_never_charges(e, fault):
    fund(e)
    lid = create(e, config(entry_fee=40)) if fault != 'audience' else create(e, config(audience='guest', prizes=[
        {'kind': 'registration', 'name': '本人资格', 'quantity': 1, 'group_id': 'standard', 'days': 10}]))
    if fault == 'group_unknown':
        e.ctl.membership = None
    elif fault == 'not_member':
        e.ctl.membership = {'status': 'left'}
    elif fault == 'expired':
        c = e.service.row(lid)['config']
        c['deadline'] = int(time.time())
        e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))
    if fault == 'ip':
        response = e.client.post(BASE + f'/{lid}/join', headers=headers(MEMBER),
                                 json={'expected_entry_fee': 40, 'browser_id': browser_id(MEMBER)})
    else:
        response = enter(e, lid, **({'browser_id': ''} if fault == 'device' else {}))
    assert response.status_code == 400
    assert_empty(e, lid)


@pytest.mark.parametrize('stage', ['ledger', 'signals', 'insert'])
def test_failure_after_debit_rolls_back_entire_transaction(e, monkeypatch, stage):
    fund(e)
    lid = create(e, config(entry_fee=40))
    if stage == 'insert':
        e.service.db.execute("CREATE TRIGGER test_entry_failure BEFORE INSERT ON lottery_entries "
                             "BEGIN SELECT RAISE(ABORT, 'mock insert failure'); END")
    else:
        owner, attr = (e.service.points, '_apply') if stage == 'ledger' else (lottery_identity, 'record')
        original = getattr(owner, attr)

        def broken(*args, **kw):
            original(*args, **kw)
            raise sqlite3.OperationalError('mock failure after write')
        monkeypatch.setattr(owner, attr, broken)
    with pytest.raises(sqlite3.DatabaseError):
        asyncio.run(e.service.join(lid, {'id': MEMBER}, expected_entry_fee=40, **captcha_args(e.service, lid, MEMBER),
                                  source_ip=e.ip_for(MEMBER), browser_id=browser_id(MEMBER)))
    assert_empty(e, lid)


def test_deadline_rechecked_inside_transaction_after_group_lookup(e, monkeypatch):
    fund(e)
    lid = create(e, config(entry_fee=40))
    original = e.service.bot._call

    async def after_lookup(method, payload=None, **kw):
        result = await original(method, payload, **kw)
        if method == 'getChatMember':
            c = e.service.row(lid)['config']
            c['deadline'] = int(time.time())
            e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))
        return result
    monkeypatch.setattr(e.service.bot, '_call', after_lookup)
    assert enter(e, lid).status_code == 400
    assert_empty(e, lid)


def test_concurrent_duplicate_across_connections_and_response_lost_retry(e):
    fund(e, amount=40)
    lid = create(e, config(entry_fee=40))
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    gate = threading.Barrier(8)

    def join(service):
        gate.wait()
        asyncio.run(service.join(lid, {'id': MEMBER}, expected_entry_fee=40, **captcha_args(service, lid, MEMBER),
                                 source_ip=e.ip_for(MEMBER), browser_id=browser_id(MEMBER)))
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(join, [other, e.service] * 4))
        assert len(fees(e)) == 1 and e.service.points.balance('u2') == 0
        assert e.service.records(lid)['total'] == 1 and e.service.records(lid)['excluded'] == 0
        assert enter(e, lid).status_code == 200  # lost HTTP response retry
        assert len(fees(e)) == 1
    finally:
        other.db.close()


def test_fee_and_other_points_spender_cannot_overspend(e):
    fund(e)
    lid = create(e, config(entry_fee=80))
    other_db = Database(e.service.db.path)
    other_points = PointsService(other_db)
    gate = threading.Barrier(2)

    def attempt(action):
        gate.wait()
        try:
            action()
            return True
        except ValueError as exc:
            assert '积分不足' in str(exc)
            return False

    def join():
        asyncio.run(e.service.join(lid, {'id': MEMBER}, expected_entry_fee=80, **captcha_args(e.service, lid, MEMBER),
                                 source_ip=e.ip_for(MEMBER), browser_id=browser_id(MEMBER)))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [join, lambda: other_points.transfer('u2', 'u1', 80)]))
        assert sorted(results) == [False, True] and e.service.points.balance('u2') == 20
        assert len(fees(e)) == int(results[0])
        assert e.service.view(lid, str(MEMBER))['joined'] == results[0]
        assert e.service.points.balance('u1') == (80 if results[1] else 0)
    finally:
        other_db.close()


@pytest.mark.parametrize('reason', ['ip', 'device'])
def test_linked_entries_charge_both_and_never_refund_or_expose_risk(e, reason):
    fund(e)
    fund(e, 'u3')
    e.service.members.upsert('u3', 'mock-member', {'group_id': 'standard'})
    e.service.db.execute('UPDATE members SET tg_user_id=? WHERE emby_user_id=?', (str(GUEST), 'u3'))
    lid = create(e, config(entry_fee=40))
    assert enter(e, lid).status_code == 200
    assert e.service.points.balance('u2') == 60
    if reason == 'ip':
        e.ips[str(GUEST)] = e.ip_for(MEMBER)
    second = enter(e, lid, GUEST, **({'browser_id': browser_id(MEMBER)} if reason == 'device' else {}))
    assert second.status_code == 200 and second.json()['joined']
    first = req(e, 'GET', '/' + lid, MEMBER).json()
    assert first == second.json()
    assert all(key not in first for key in ('qualification', 'excluded_count', 'candidate_count', 'clusters'))
    assert e.service.records(lid)['excluded'] == 2
    assert len(fees(e)) == 2 and e.service.points.balance('u2') == e.service.points.balance('u3') == 60
    draw(e, lid)
    assert e.service.view(lid, str(MEMBER))['winners'] == []
    for tg in (MEMBER, GUEST):
        assert req(e, 'POST', f'/{lid}/claim', tg).status_code == 404
    assert e.service.points.balance('u2') == e.service.points.balance('u3') == 60
    assert len(e.service.db.query("SELECT * FROM points_ledger WHERE delta>0")) == 2


def test_admin_not_exempt_from_strict_association(e):
    fund(e)
    fund(e, 'u1')
    lid = create(e, config(entry_fee=40))
    assert enter(e, lid).status_code == 200
    e.ips[str(ADMIN)] = e.ip_for(MEMBER)
    assert enter(e, lid, ADMIN).status_code == 200
    assert e.service.records(lid)['excluded'] == 2
    draw(e, lid)
    assert e.service.points.balance('u1') == e.service.points.balance('u2') == 60
    assert not e.service.db.query('SELECT * FROM lottery_awards')


def test_winner_and_loser_both_pay_no_refund_on_settlement_or_retries(e):
    fund(e)
    fund(e, 'u1')
    lid = create(e, config(entry_fee=40, prizes=[{'kind': 'invite', 'name': '邀请名额', 'quantity': 1, 'amount': 1}]))
    assert enter(e, lid).status_code == 200 and enter(e, lid, ADMIN).status_code == 200
    before = e.service.db.query('SELECT * FROM points_ledger ORDER BY id')
    result = draw(e, lid)
    assert len(result['winners']) == 1 and result['awards'][0]['state'] == 'delivered'
    draw(e, lid)
    e.service.deliver_awards(lid)
    assert e.service.points.balance('u1') == e.service.points.balance('u2') == 60
    assert e.service.db.query('SELECT * FROM points_ledger ORDER BY id') == before


@pytest.mark.parametrize('fee', [0, 40])
def test_api_rules_and_launch_disclose_fee_and_keep_native_icons(e, fee):
    lid = create(e, config(entry_fee=fee), publish=False)
    terms = entry_terms({'entry_fee': fee})
    line = '报名 40 积分 · 成功报名不退，取消活动全额退还'
    preview = req(e, 'POST', f'/{lid}/preview').json()
    assert terms in preview['rules'] and (line in preview['text']) == bool(fee)
    native = 3 + len(config()['prizes']) + (1 if fee else 0)  # title, prizes, timing, eligibility, fee
    assert preview['text'].count('<tg-emoji') == native and '🔡' not in preview['text']
    req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
    result = req(e, 'GET', '/' + lid, MEMBER).json()
    assert terms in result['rules']
    launch = [p for m, p in e.calls if m == 'sendMessage'][-1]
    assert launch['text'] == preview['text'] and launch['reply_markup']['inline_keyboard'][0][0]['icon_custom_emoji_id']
    if fee:
        assert '仅已注册可参与' in launch['text'] and '已注册积分账户' in result['rules']
        assert '免费' not in launch['text'] and '免费' not in result['rules']
    else:
        assert '报名 0 积分' not in launch['text'] and '不退' not in launch['text']
    for word in ('多账号', '防小号', '关联', 'IP', '浏览器', '设备'):
        assert word not in result['rules'] and word not in launch['text']
    assert '排除' not in launch['text']


def test_distinct_activities_charge_separately(e):
    fund(e)
    lids = [create(e, config(entry_fee=40)) for _ in range(2)]
    for lid in lids:
        assert enter(e, lid).status_code == 200
        assert enter(e, lid).status_code == 200
    assert e.service.points.balance('u2') == 20 and len(fees(e)) == 2
    assert {r['ref'] for r in fees(e)} == {f'lottery.entry:{lid}:{MEMBER}' for lid in lids}


@pytest.mark.parametrize('reason', ['ip', 'device'])
def test_concurrent_linked_paid_entries_both_debit_and_exclude(e, reason):
    fund(e)
    fund(e, 'u1')
    lid = create(e, config(entry_fee=40))
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    gate = threading.Barrier(2)
    first_ip, second_ip = e.ip_for(MEMBER), e.ip_for(ADMIN)

    def signup(service, tg, ip):
        gate.wait()
        asyncio.run(service.join(lid, {'id': tg}, expected_entry_fee=40, **captcha_args(service, lid, tg),
                                 source_ip=first_ip if reason == 'ip' else ip,
                                 browser_id=browser_id(MEMBER if reason == 'device' else tg)))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(signup, e.service, MEMBER, first_ip)
            b = pool.submit(signup, other, ADMIN, second_ip)
            a.result()
            b.result()
        assert len(fees(e)) == 2 and e.service.records(lid)['excluded'] == 2
        draw(e, lid)
        assert e.service.points.balance('u1') == e.service.points.balance('u2') == 60
        assert not e.service.db.query('SELECT * FROM lottery_awards')
    finally:
        other.db.close()


def test_failed_linked_entry_rolls_back_prior_accounts_exclusion_and_own_fee(e, monkeypatch):
    fund(e)
    fund(e, 'u1')
    lid = create(e, config(entry_fee=40))
    assert enter(e, lid).status_code == 200
    before = fees(e)
    original = lottery_identity.record

    def broken(*args, **kw):
        original(*args, **kw)
        raise sqlite3.OperationalError('mock failure after associated accounts were updated')
    monkeypatch.setattr(lottery_identity, 'record', broken)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(e.service.join(lid, {'id': ADMIN}, expected_entry_fee=40, **captcha_args(e.service, lid, ADMIN),
                                  source_ip=e.ip_for(MEMBER), browser_id=browser_id(ADMIN)))
    assert fees(e) == before and e.service.points.balance('u1') == 100
    assert e.service.records(lid)['excluded'] == 0 and e.service.records(lid)['total'] == 1
    assert e.service.view(lid, str(ADMIN))['joined'] is False


def test_draw_during_group_lookup_leaves_no_fee_or_late_entry(e, monkeypatch):
    fund(e)
    lid = create(e, config(entry_fee=40))
    original = e.service.bot._call

    async def draw_before_admission(method, payload=None, **kw):
        result = await original(method, payload, **kw)
        if method == 'getChatMember':
            e.service.draw(lid)
        return result
    monkeypatch.setattr(e.service.bot, '_call', draw_before_admission)
    assert enter(e, lid).status_code == 400
    assert_empty(e, lid)
    assert e.service.row(lid)['state'] == 'settled'
