"""Count-triggered settlement: isolated real SQLite/APIs and mocked Telegram."""
import asyncio
import copy
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from app.core.db import Database
from app.modules.points import PointsService
from tests.lottery_legacy_helpers import legacy_create
from tests.lottery_private_helpers import private_start
from tests.test_lottery import ADMIN, GUEST, MEMBER, browser_id, config, create, draw, join, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_lottery_device import assert_public
from tests.test_lottery_eligibility import MANUAL, POINTS, REG, bind, shop_prize

UNSET = object()


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def counted(**kw):
    return config(**{'draw_mode': 'count', 'max_entries': 3, 'prizes': [MANUAL], **kw})


def enter(e, lid, tg=MEMBER, **kw):
    return req(e, 'POST', f'/{lid}/join', tg, json={'browser_id': browser_id(tg), **kw})


def rows(e, lid, table):
    return e.service.db.query(f'SELECT * FROM {table} WHERE lottery_id=? ORDER BY rowid', (lid,))


def snapshot(e, lid):
    return {t: rows(e, lid, t) for t in ('lottery_entries', 'lottery_signals', 'lottery_awards', 'lottery_notices', 'lottery_dms')}


def saved(e, lid):
    return e.service.db.one('SELECT * FROM lotteries WHERE id=?', (lid,))


def fees(e, lid):
    return e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.entry' AND ref LIKE ? ORDER BY id",
                              (f'lottery.entry:{lid}:%',))


def funded(e, uid='u2', amount=100):
    e.service.points.add(uid, amount, 'admin.mint', 'count-test-' + uid)


@contextmanager
def worker(e):
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    other.points = PointsService(other.db)
    other.groups = copy.copy(e.service.groups)
    other.groups._db = other.db
    other.registration = copy.copy(e.service.registration)
    cfg = e.service.registration._cfg()
    other.registration._config = lambda: cfg
    other.registration._db = other.db
    try:
        yield other
    finally:
        other.db.close()


def service_join(e, service, lid, tg, fee=0):
    asyncio.run(service.join(lid, {'id': tg}, expected_entry_fee=fee,
                             source_ip=e.ip_for(tg), browser_id=browser_id(tg)))


@pytest.mark.parametrize('maximum', [UNSET, None, False, True, 0, -1, 100001, 1.0, '5', [], {}])
def test_count_requires_bounded_integer(e, maximum):
    raw = counted()
    if maximum is UNSET:
        raw.pop('max_entries')
    else:
        raw['max_entries'] = maximum
    response = req(e, 'POST', '/admin/drafts', json=raw)
    assert response.status_code == 400 and '开奖人数须为 1–100000 的整数' in response.json()['detail']
    assert not e.service.db.query('SELECT * FROM lotteries')


@pytest.mark.parametrize('maximum', [1, 2, 100000])
def test_count_freezes_without_time_or_reminder(e, maximum):
    lid = create(e, counted(max_entries=maximum, deadline='stale hidden time', reminder=True), publish=False)
    c = req(e, 'GET', '/' + lid).json()['config']
    assert c['max_entries'] == maximum and c['deadline'] is None and c['reminder'] is False
    assert c['device_check'] is False and c['eligibility_version'] == 2
    preview = req(e, 'POST', f'/{lid}/preview').json()
    for text in (preview['text'], preview['rules']):
        assert f'满{maximum}人自动开奖' in text
        assert all(w not in text for w in ('None', '截止', '提醒', '防小号', '多账号', 'IP', '浏览器', '设备', '关联'))
    assert '通用人工奖' in preview['text'] and '免费参与' in preview['rules']
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1}).status_code == 200
    frozen = saved(e, lid)
    assert req(e, 'PUT', '/' + lid, json={**counted(max_entries=maximum + 1), 'revision': 1}).status_code == 400
    req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
    assert saved(e, lid) == frozen
    launch = json.loads(rows(e, lid, 'lottery_notices')[0]['payload_json'])
    assert launch['text'] == preview['text'] and launch['reply_markup']['inline_keyboard'][0][0]['icon_custom_emoji_id']
    assert req(e, 'GET', '/' + lid).json()['operations']['reminder']['state'] == 'off'


@pytest.mark.parametrize('mode', ['manual', 'timed'])
@pytest.mark.parametrize('hidden', [None, 8, True, -5, 'stale'])
def test_other_modes_clear_hidden_maximum(e, mode, hidden):
    lid = create(e, config(draw_mode=mode, max_entries=hidden), publish=False)
    c = e.service.row(lid)['config']
    assert c['max_entries'] == 0 and (c['deadline'] is None) is (mode == 'manual')
    assert c['reminder'] is (mode == 'timed')


def test_last_entry_participates_atomically_and_does_not_send(e, monkeypatch):
    lid = create(e, counted())
    assert join(e, lid, GUEST)['count'] == 1
    assert join(e, lid, GUEST)['count'] == 1
    join(e, lid, MEMBER)
    far_future = int(time.time()) + 400 * 86400
    e.service.draw(lid, timed=True, now=far_future)
    e.service.draw(lid, counted=True, now=far_future)
    with monkeypatch.context() as m:
        m.setattr(time, 'time', lambda: far_future)
        asyncio.run(e.service.tick())
    assert e.service.row(lid)['state'] == 'published' and not rows(e, lid, 'lottery_awards')
    assert not any(r['kind'] == 'reminder' for r in rows(e, lid, 'lottery_notices'))
    calls = len(e.calls)
    last = join(e, lid, ADMIN)
    assert last['state'] == 'settled' and last['count'] == 3 and last['joined']
    awards = rows(e, lid, 'lottery_awards')
    assert {a['tg_id'] for a in awards} == {str(u) for u in (GUEST, MEMBER, ADMIN)}
    assert {a['state'] for a in awards} == {'pending'} and len(rows(e, lid, 'lottery_dms')) == 3
    assert [m for m, _ in e.calls[calls:]] == ['getChatMember']
    frozen = snapshot(e, lid)
    for user in (GUEST, MEMBER, ADMIN):
        assert join(e, lid, user)['state'] == 'settled'
    e.service.draw(lid)
    assert snapshot(e, lid) == frozen
    assert enter(e, lid, 999111222).status_code == 400 and snapshot(e, lid) == frozen


def test_lists_expose_only_public_progress(e):
    lid = create(e, counted(max_entries=2))
    join(e, lid)
    for path in ('/ongoing', '/admin/list'):
        result = req(e, 'GET', path, GUEST if path == '/ongoing' else ADMIN).json()
        item = next(r for r in result['items'] if r['id'] == lid)
        assert (item['draw_mode'], item['max_entries'], item['count'], item['deadline']) == ('count', 2, 1, None)
        assert_public(item)
    assert_public(req(e, 'GET', '/' + lid, GUEST).json())
    join(e, lid, MEMBER)
    assert lid not in {i['id'] for i in req(e, 'GET', '/ongoing', MEMBER).json()['items']}


@pytest.mark.parametrize('reason', ['ip', 'browser'])
def test_total_includes_excluded_paid_entries_without_disclosure_or_refund(e, reason):
    bind(e, GUEST, 'u3')
    funded(e)
    funded(e, 'u3')
    lid = create(e, counted(max_entries=2, audience='member', entry_fee=40))
    preview = req(e, 'POST', f'/{lid}/preview').json()
    assert '报名 40 积分 · 成功报名不退，取消活动全额退还' in preview['text']
    assert enter(e, lid, MEMBER, expected_entry_fee=40).status_code == 200
    if reason == 'ip':
        e.ips[str(GUEST)] = e.ip_for(MEMBER)
    last = enter(e, lid, GUEST, expected_entry_fee=40,
                 **({'browser_id': browser_id(MEMBER)} if reason == 'browser' else {})).json()
    assert last['count'] == 2 and last['state'] == 'settled' and last['joined'] and last['winners'] == []
    assert_public(last)
    assert last['prize_eligible'] and not last['prize_eligibility_reason']
    admin = req(e, 'GET', '/' + lid).json()
    assert admin['excluded_count'] == 2 and admin['candidate_count'] == 0 and admin['admin_rules']
    assert len(fees(e, lid)) == 2 and e.service.points.balance('u2') == e.service.points.balance('u3') == 60
    assert not e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.refund'")


@pytest.mark.parametrize('kind', ['points', 'invite', 'shop', 'custom'])
def test_settlement_uses_current_identity_not_join_snapshot(e, kind):
    prize = (shop_prize(e, custom=kind == 'custom') if kind in ('shop', 'custom') else
             {'kind': kind, 'name': '账户奖', 'quantity': 4, 'amount': 10})
    lid = create(e, counted(prizes=[prize, REG]))
    join(e, lid, MEMBER)
    join(e, lid, GUEST)
    e.service.db.execute("UPDATE members SET tg_user_id='' WHERE emby_user_id='u2'")
    bind(e, GUEST, 'u3')
    join(e, lid, ADMIN)
    awards = rows(e, lid, 'lottery_awards')
    assert len(awards) == len({a['tg_id'] for a in awards}) == 3
    assert {a['tg_id']: json.loads(a['prize_json'])['kind'] for a in awards} == {
        str(MEMBER): 'registration', str(GUEST): prize['kind'], str(ADMIN): prize['kind']}


@pytest.mark.parametrize('stage', ['awards', 'result', 'dms', 'audit'])
def test_settlement_failure_rolls_back_fee_entry_signals_and_jobs(e, stage):
    funded(e)
    funded(e, 'u1')
    lid = create(e, counted(max_entries=2, audience='member', entry_fee=40))
    assert enter(e, lid, MEMBER, expected_entry_fee=40).status_code == 200
    before = snapshot(e, lid)
    ledger = e.service.db.query('SELECT * FROM points_ledger')
    table, condition = {'awards': ('lottery_awards', '1'), 'result': ('lottery_notices', "NEW.kind='result'"),
                        'dms': ('lottery_dms', '1'), 'audit': ('audit_log', "NEW.action='lottery.draw'")}[stage]
    e.service.db.execute(f"CREATE TRIGGER abort_count BEFORE INSERT ON {table} WHEN {condition} "
                         "BEGIN SELECT RAISE(ABORT, 'isolated settlement failure'); END")
    with pytest.raises(sqlite3.DatabaseError):
        service_join(e, e.service, lid, ADMIN, fee=40)
    assert snapshot(e, lid) == before and e.service.row(lid)['state'] == 'published'
    assert e.service.db.query('SELECT * FROM points_ledger') == ledger
    e.service.db.execute('DROP TRIGGER abort_count')
    assert enter(e, lid, ADMIN, expected_entry_fee=40).json()['state'] == 'settled'
    assert len(fees(e, lid)) == 2 and len(rows(e, lid, 'lottery_awards')) == 2


@pytest.mark.parametrize('duplicate', [False, True])
def test_cross_connection_cap_and_duplicate_last_entry(e, monkeypatch, duplicate):
    users = [MEMBER] * 8 if duplicate else [888100000 + i for i in range(8)]
    for tg in set(users):
        if tg != MEMBER:
            bind(e, tg, str(tg))
        private_start(e.service, tg)
        funded(e, 'u2' if tg == MEMBER else str(tg), 40)
    maximum = 1 if duplicate else 3
    lid = create(e, counted(max_entries=maximum, audience='member', entry_fee=40, prizes=[POINTS]))
    barrier = threading.Barrier(len(users))
    original = e.service.bot._call

    async def lookup(method, payload=None, **kw):
        result = await original(method, payload, **kw)
        if method == 'getChatMember':
            barrier.wait(timeout=15)
        return result
    monkeypatch.setattr(e.service.bot, '_call', lookup)
    with worker(e) as other:
        def attempt(pair):
            service, tg = pair
            try:
                service_join(e, service, lid, tg, 40)
                return True
            except ValueError as exc:
                assert '截止' in str(exc) or '人数已满' in str(exc)
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, [(other if i % 2 else e.service, tg) for i, tg in enumerate(users)]))
    assert results.count(True) == (8 if duplicate else maximum)
    entrants = {r['tg_id'] for r in rows(e, lid, 'lottery_entries')}
    assert len(entrants) == len(fees(e, lid)) == len(rows(e, lid, 'lottery_awards')) == maximum
    assert {r['tg_id'] for r in rows(e, lid, 'lottery_awards')} == entrants
    for tg in set(users):
        assert e.service.points.balance('u2' if tg == MEMBER else str(tg)) == (0 if str(tg) in entrants else 40)
    before = snapshot(e, lid)
    for tg in entrants:
        assert req(e, 'POST', f'/{lid}/join', int(tg), json={}).json()['joined']
    assert snapshot(e, lid) == before and len(fees(e, lid)) == maximum


def test_count_fee_and_other_spender_share_atomic_balance(e):
    funded(e, amount=100)
    lid = create(e, counted(max_entries=1, audience='member', entry_fee=80))
    barrier = threading.Barrier(2)
    with worker(e) as other:
        def attempt(action):
            barrier.wait(timeout=15)
            try:
                action()
                return True
            except ValueError as exc:
                assert '积分不足' in str(exc)
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [lambda: service_join(e, e.service, lid, MEMBER, 80),
                                               lambda: other.points.transfer('u2', 'u1', 80)]))
    assert sorted(results) == [False, True] and e.service.points.balance('u2') == 20
    assert len(fees(e, lid)) == len(rows(e, lid, 'lottery_awards')) == int(results[0])
    assert e.service.row(lid)['state'] == ('settled' if results[0] else 'published')


def test_restart_continues_awards_notices_dms_without_redraw(e):
    funded(e, amount=40)
    lid = create(e, counted(max_entries=1, audience='member', entry_fee=40, prizes=[POINTS]))
    assert enter(e, lid, MEMBER, expected_entry_fee=40).json()['state'] == 'settled'
    assert e.service.points.balance('u2') == 0
    with worker(e) as restarted:
        asyncio.run(restarted.tick())
        after = snapshot(e, lid)
        ledger = e.service.db.query('SELECT * FROM points_ledger')
        calls = len([m for m, _ in e.calls if m == 'sendMessage'])
        asyncio.run(restarted.tick())
        restarted.draw(lid)
    assert snapshot(e, lid) == after and e.service.db.query('SELECT * FROM points_ledger') == ledger
    assert len([m for m, _ in e.calls if m == 'sendMessage']) == calls
    assert e.service.points.balance('u2') == 10
    assert {r['state'] for r in rows(e, lid, 'lottery_awards')} == {'delivered'}
    assert {r['state'] for r in rows(e, lid, 'lottery_dms')} == {'sent'}
    assert {r['state'] for r in rows(e, lid, 'lottery_notices')} == {'sent'}


def test_maintenance_recovers_full_published_row_and_refuses_extra_entry(e, monkeypatch):
    lid = create(e, counted(max_entries=1))
    # Recovery fixture: a fully admitted interrupted row, without settlement.
    with monkeypatch.context() as m:
        m.setattr(e.service, '_settle', lambda *args: None)
        assert join(e, lid, MEMBER)['state'] == 'published'
    before = snapshot(e, lid)
    assert enter(e, lid, GUEST).status_code == 400 and snapshot(e, lid) == before
    with worker(e) as restarted:
        asyncio.run(restarted.tick())
    assert e.service.row(lid)['state'] == 'settled'
    assert {r['tg_id'] for r in rows(e, lid, 'lottery_awards')} == {str(MEMBER)}


@pytest.mark.parametrize('mode', ['manual', 'timed', 'count'])
def test_admin_can_draw_all_published_modes_early(e, mode):
    lid = create(e, counted(draw_mode=mode, max_entries=100))
    assert join(e, lid)['state'] == 'published'
    for user in (GUEST, MEMBER):
        assert req(e, 'POST', f'/{lid}/draw', user).status_code == 403
    assert draw(e, lid)['state'] == 'settled'
    before = snapshot(e, lid)
    draw(e, lid)
    assert snapshot(e, lid) == before and len(rows(e, lid, 'lottery_awards')) == 1
    assert enter(e, lid, MEMBER).status_code == 400


@pytest.mark.parametrize('action', ['cancel', 'manual'])
def test_final_entry_races_admin_action(e, action):
    funded(e, amount=500)
    funded(e, 'u1', 500)
    with worker(e) as other:
        for _ in range(4):
            lid = create(e, counted(max_entries=2, audience='member', entry_fee=40))
            assert enter(e, lid, MEMBER, expected_entry_fee=40).status_code == 200
            barrier = threading.Barrier(2)

            def attempt(fn, gate=barrier):
                gate.wait(timeout=15)
                try:
                    fn()
                    return 'ok'
                except ValueError as exc:
                    return str(exc)
            command = (lambda lid=lid: other.cancel(lid, 'test', confirm=True)) if action == 'cancel' else (lambda lid=lid: other.draw(lid, 'test'))
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, [lambda lid=lid: service_join(e, e.service, lid, ADMIN, 40), command]))
            entries, awards = rows(e, lid, 'lottery_entries'), rows(e, lid, 'lottery_awards')
            refunds = e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.refund' AND ref LIKE ?",
                                          (f'lottery.refund:{lid}:%',))
            if e.service.row(lid)['state'] == 'cancelled':
                assert action == 'cancel' and len(entries) == len(refunds) == 1 and not awards
                assert '已取消' in results[0] and results[1] == 'ok'
            else:
                assert not refunds and len(awards) == len(entries)
                assert len(entries) in ((2,) if action == 'cancel' else (1, 2))
                if action == 'cancel':
                    assert results == ['ok', '已开奖的活动不能取消']
            assert len(fees(e, lid)) == len(entries)


@pytest.mark.parametrize('state', ['draft', 'published', 'settled', 'cancelled'])
def test_copy_count_preserves_limit_order_and_source(e, state):
    source = create(e, counted(prizes=[{**MANUAL, 'name': 'First'}, {**MANUAL, 'name': 'Second'}]), publish=state != 'draft')
    if state == 'settled':
        join(e, source)
        draw(e, source)
    elif state == 'cancelled':
        e.service.cancel(source, 'test', confirm=True)
    old, old_data = saved(e, source), snapshot(e, source)
    copied = req(e, 'POST', f'/{source}/copy').json()
    assert copied['state'] == 'draft' and copied['count'] == 0 and copied['winners'] == []
    assert copied['config']['max_entries'] == 3 and copied['config']['draw_mode'] == 'count'
    assert copied['config']['deadline'] is None and copied['config']['reminder'] is False
    assert [p['name'] for p in copied['config']['prizes']] == ['First', 'Second']
    assert req(e, 'POST', f'/{copied["id"]}/publish', json={'revision': 1}).status_code == 200
    assert saved(e, source) == old and snapshot(e, source) == old_data


@pytest.mark.parametrize('mode', ['manual', 'timed'])
@pytest.mark.parametrize('version', [1, 2])
def test_old_missing_maximum_reads_zero_without_backfill(e, mode, version):
    maker = legacy_create if version == 1 else create
    lid = maker(e, config(draw_mode=mode, prizes=[MANUAL]), publish=False)
    c = json.loads(saved(e, lid)['config_json'])
    c.pop('max_entries', None)  # genuine pre-count draft, not a client switch
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))
    before = saved(e, lid)
    old_rules = req(e, 'GET', '/' + lid).json()['rules']
    assert e.service.row(lid)['config']['max_entries'] == 0
    req(e, 'POST', f'/{lid}/preview')
    assert saved(e, lid) == before
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1, 'max_entries': 1}).status_code == 200
    assert 'max_entries' not in json.loads(saved(e, lid)['config_json'])
    frozen, notices = saved(e, lid), rows(e, lid, 'lottery_notices')
    assert req(e, 'GET', '/' + lid, MEMBER).json()['rules'] == old_rules
    assert req(e, 'GET', '/' + lid, MEMBER).json()['config']['max_entries'] == 0
    assert req(e, 'POST', f'/{lid}/copy').json()['config']['max_entries'] == 0
    assert saved(e, lid) == frozen and rows(e, lid, 'lottery_notices') == notices
