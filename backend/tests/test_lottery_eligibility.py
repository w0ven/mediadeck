"""Eligibility v2: server-owned freeze, current-identity draws, no hidden fees.

All records are isolated fixtures and Telegram is mocked by the lottery env.
"""
import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.db import Database
from app.modules.lottery import ELIGIBILITY_RULES, FAIRNESS, RULES
from tests.lottery_legacy_helpers import legacy_create
from tests.test_lottery import ADMIN, GUEST, MEMBER, browser_id, config, create, draw, join, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_lottery_device import assert_public

MANUAL = {'kind': 'manual', 'name': '通用人工奖', 'quantity': 10, 'instructions': 'PRIVATE-NOTE'}
REG = {'kind': 'registration', 'name': '本人资格', 'quantity': 2, 'group_id': 'standard', 'days': 10}
POINTS = {'kind': 'points', 'name': '积分', 'quantity': 10, 'amount': 10}
INVITE = {'kind': 'invite', 'name': '邀请名额', 'quantity': 10, 'amount': 2}
HIDDEN = ('entry_fee', 'min_level', 'min_points', 'min_watch_hours')


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def snapshot(e, lid):
    return {table: e.service.db.query(f'SELECT * FROM {table} WHERE lottery_id=? ORDER BY rowid', (lid,))
            for table in ('lottery_entries', 'lottery_awards', 'lottery_notices', 'lottery_dms')}


def bind(e, tg, uid):
    e.service.members.upsert(uid, uid, {'group_id': 'standard'})
    e.service.db.execute('UPDATE members SET tg_user_id=? WHERE emby_user_id=?', (str(tg), uid))


def enter(e, lid, tg=MEMBER, **extra):
    return req(e, 'POST', f'/{lid}/join', tg, json={'browser_id': browser_id(tg), **extra})


def shop_prize(e, custom=False):
    item = e.service.shop.items()[0]
    if custom:
        e.service.db.execute("UPDATE shop_items SET kind='custom',amount=1,purchase_notice='private' WHERE id=?", (item['id'],))
    return {'kind': 'shop', 'name': '商城奖', 'quantity': 10, 'item_id': item['id']}


@pytest.mark.parametrize('deadline', [None, 1, True, 'hidden-old-value', 9999999999])
def test_manual_discards_deadline_and_reminder_even_from_old_forms(e, deadline):
    lid = create(e, config(draw_mode='manual', deadline=deadline, reminder=True, prizes=[MANUAL]))
    row = e.service.row(lid)
    assert row['config']['deadline'] is None and row['config']['reminder'] is False
    assert row['config']['eligibility_version'] == 2
    e.service.stage_reminders(now=int(time.time()) + 86400)
    assert not e.service.db.query("SELECT * FROM lottery_notices WHERE kind='reminder'")
    assert join(e, lid)['joined']
    e.service.draw(lid, timed=True, now=int(time.time()) + 86400)
    assert e.service.row(lid)['state'] == 'published'
    launch = e.service.db.one("SELECT payload_json FROM lottery_notices WHERE lottery_id=? AND kind='launch'", (lid,))
    assert '管理员开奖 · 开奖前均可报名' in launch['payload_json'] and '截止' not in launch['payload_json']
    assert len(draw(e, lid)['winners']) == 1


@pytest.mark.parametrize('deadline', [None, 1, True, 'bad'])
def test_timed_still_requires_valid_future_deadline(e, deadline):
    assert req(e, 'POST', '/admin/drafts', json=config(deadline=deadline)).status_code == 400
    assert not e.service.db.query('SELECT * FROM lotteries')


@pytest.mark.parametrize('audience', ['all', 'guest'])
@pytest.mark.parametrize('hidden', [40, 'stale', True, -1])
def test_hidden_member_conditions_zero_and_cannot_charge_or_block(e, audience, hidden):
    raw = config(audience=audience, prizes=[MANUAL], **dict.fromkeys(HIDDEN, hidden))
    lid = create(e, raw)
    c = e.service.row(lid)['config']
    assert all(c[key] == 0 for key in HIDDEN)
    assert enter(e, lid, GUEST).json()['joined']
    assert not e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.entry'")
    view = req(e, 'GET', '/' + lid, GUEST).json()
    assert_public(view)
    assert '免费参与' in view['rules'] and '参与门槛' not in view['rules']
    assert view['prize_eligible'] is True and view['prize_eligibility_reason'] == ''


def test_switching_saved_member_draft_clears_hidden_fields_and_cannot_downgrade(e):
    lid = create(e, config(audience='member', entry_fee=40, min_points=20, min_watch_hours=1,
                           prizes=[MANUAL]), publish=False)
    for revision, audience in enumerate(('all', 'guest', 'member'), start=1):
        raw = req(e, 'GET', '/' + lid).json()['config']
        changed = req(e, 'PUT', '/' + lid, json={**raw, 'audience': audience, 'revision': revision,
                                                'eligibility_version': 1, 'legacy': True})
        assert changed.status_code == 200, changed.text
        assert changed.json()['config']['eligibility_version'] == 2
        assert all(changed.json()['config'][key] == 0 for key in HIDDEN)
    req(e, 'POST', f'/{lid}/publish', json={'revision': 4})
    assert enter(e, lid, MEMBER).json()['joined']
    assert e.service.points.balance('u2') == 0


@pytest.mark.parametrize('kind', ['points', 'invite', 'shop', 'shop-custom'])
def test_guest_audience_rejects_all_account_prizes(e, kind):
    prize = shop_prize(e, custom=kind == 'shop-custom') if kind.startswith('shop') else (POINTS if kind == 'points' else INVITE)
    result = req(e, 'POST', '/admin/drafts', json=config(audience='guest', prizes=[prize]))
    assert result.status_code == 400 and '仅未注册活动不能配置' in result.json()['detail']
    assert not e.service.db.query('SELECT * FROM lotteries')
    # The client's extra prize/audience fields cannot make a shop item universal.
    result = req(e, 'POST', '/admin/drafts', json=config(audience='guest', prizes=[{**prize, 'audience': 'all'}]))
    assert result.status_code == 400


def test_member_audience_rejects_registration_even_when_free(e):
    r = req(e, 'POST', '/admin/drafts', json=config(audience='member', prizes=[REG]))
    assert r.status_code == 400 and '仅已注册活动不能配置注册资格' in r.json()['detail']


@pytest.mark.parametrize('registered', [True, False])
def test_no_eligible_prize_is_public_and_blocks_even_admin_without_any_writes(e, registered):
    lid = create(e, config(prizes=[REG] if registered else [POINTS]))
    users = (MEMBER, ADMIN) if registered else (GUEST,)
    before = snapshot(e, lid)
    for user in users:
        v = req(e, 'GET', '/' + lid, user).json()
        assert v['prize_eligible'] is False and v['prize_eligibility_reason']
        if user != ADMIN:
            assert_public(v)
        r = enter(e, lid, user, registered=not registered, is_admin=True, eligibility_version=1, prize_eligible=True)
        assert r.status_code == 400 and r.json()['detail'] == v['prize_eligibility_reason']
    assert snapshot(e, lid) == before
    for table in ('lottery_signals', 'lottery_device_entries', 'points_ledger'):
        assert not e.service.db.query(f'SELECT * FROM {table}')


@pytest.mark.parametrize('kind', ['points', 'invite', 'shop', 'shop-custom'])
def test_mixed_pool_isolated_by_current_identity_and_global_one_prize(e, kind):
    prize = shop_prize(e, custom=kind == 'shop-custom') if kind.startswith('shop') else (POINTS if kind == 'points' else INVITE)
    lid = create(e, config(prizes=[prize, {**REG, 'quantity': 1}, MANUAL]))
    guests = (GUEST, 888000444, 888000555)
    for tg in (MEMBER, ADMIN, *guests):
        join(e, lid, tg)
    result = draw(e, lid)
    awards = e.service.db.query('SELECT * FROM lottery_awards WHERE lottery_id=?', (lid,))
    assert len(awards) == 5 and len({r['tg_id'] for r in awards}) == 5
    assert sum(json.loads(r['prize_json'])['kind'] == 'registration' for r in awards) == 1
    for a in awards:
        pk = json.loads(a['prize_json'])['kind']
        if a['tg_id'] in {str(t) for t in guests}:
            assert pk in ('registration', 'manual')
        else:
            assert pk != 'registration'
    assert all(a['state'] != 'binding' for a in result['awards'])
    before = snapshot(e, lid)
    ledger = e.service.db.query('SELECT * FROM points_ledger')
    draw(e, lid)
    e.service.deliver_awards(lid)
    assert snapshot(e, lid) == before and e.service.db.query('SELECT * FROM points_ledger') == ledger


@pytest.mark.parametrize('kind', ['points', 'invite', 'shop', 'shop-custom'])
def test_only_members_can_win_account_prize_after_guests_registration_slots_fill(e, kind):
    p = shop_prize(e, custom=kind == 'shop-custom') if kind.startswith('shop') else (POINTS if kind == 'points' else INVITE)
    lid = create(e, config(prizes=[p, {**REG, 'quantity': 1}]))
    for tg in (MEMBER, ADMIN, GUEST, 888000444):
        join(e, lid, tg)
    result = draw(e, lid)
    assert len(result['awards']) == 3  # one guest intentionally gets no award
    awards = e.service.db.query('SELECT tg_id,prize_json FROM lottery_awards WHERE lottery_id=?', (lid,))
    assert {a['tg_id'] for a in awards if json.loads(a['prize_json'])['kind'] != 'registration'} == {str(MEMBER), str(ADMIN)}


def test_identity_changes_after_join_use_draw_time_not_entry_snapshot_and_no_unjoin(e):
    lid = create(e, config(prizes=[POINTS, REG]))
    join(e, lid, MEMBER)
    join(e, lid, GUEST)
    e.service.db.execute("UPDATE members SET tg_user_id='' WHERE emby_user_id='u2'")
    bind(e, GUEST, 'u3')
    assert enter(e, lid, MEMBER).json()['joined']
    before = e.service.db.query('SELECT tg_id,registered FROM lottery_entries WHERE lottery_id=?', (lid,))
    assert {r['tg_id']: r['registered'] for r in before} == {str(MEMBER): 1, str(GUEST): 0}
    draw(e, lid)
    awards = e.service.db.query('SELECT tg_id,prize_json FROM lottery_awards WHERE lottery_id=?', (lid,))
    assert {a['tg_id']: json.loads(a['prize_json'])['kind'] for a in awards} == {str(MEMBER): 'registration', str(GUEST): 'points'}
    assert e.service.points.balance('u3') == 10


def test_joined_identity_loses_prize_eligibility_but_entry_and_idempotency_remain(e):
    lid = create(e, config(prizes=[REG]))
    join(e, lid, GUEST)
    bind(e, GUEST, 'u3')
    repeat = enter(e, lid, GUEST).json()
    assert repeat['joined'] and repeat['prize_eligible'] is False
    assert draw(e, lid)['winners'] == []
    assert e.service.db.one('SELECT COUNT(*) AS n FROM lottery_entries')['n'] == 1


def test_awarded_member_unbinds_before_delivery_keeps_pending_award_and_can_claim(e):
    lid = create(e)
    join(e, lid, MEMBER)
    e.service.draw(lid)
    award = e.service.db.one('SELECT * FROM lottery_awards WHERE lottery_id=?', (lid,))
    e.service.db.execute("UPDATE members SET tg_user_id='' WHERE emby_user_id='u2'")
    e.service.deliver_award(award['id'])
    assert req(e, 'GET', '/' + lid, MEMBER).json()['my_award']['state'] == 'binding'
    assert req(e, 'GET', '/' + lid, MEMBER).json()['prize_eligible'] is False
    bind(e, MEMBER, 'u2')
    assert req(e, 'POST', f'/{lid}/claim', MEMBER).json()['my_award']['state'] == 'delivered'
    assert e.service.points.balance('u2') == 500
    e.service.draw(lid)
    e.service.deliver_award(award['id'])
    assert e.service.points.balance('u2') == 500


def test_concurrent_draw_and_deliver_across_connections_no_redraw_or_extra_credit(e):
    lid = create(e, config(prizes=[POINTS, REG]))
    for tg in (MEMBER, ADMIN, GUEST):
        join(e, lid, tg)
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    # A worker's dependencies must share its connection, not reach into a
    # different worker while that worker waits for the SQLite writer lock.
    other.groups = copy.copy(e.service.groups)
    other.groups._db = other.db
    other.registration = copy.copy(e.service.registration)
    cfg = e.service.registration._cfg()
    other.registration._config = lambda: cfg
    other.registration._db = other.db
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda s: s.draw(lid), [e.service, other] * 6))
        frozen = snapshot(e, lid)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda s: s.deliver_awards(lid), [e.service, other] * 6))
        assert len(frozen['lottery_awards']) == 3
        assert e.service.points.balance('u1') == e.service.points.balance('u2') == 10
        assert e.service.db.one("SELECT COUNT(*) AS n FROM lottery_notices WHERE kind='result'")['n'] == 1
        assert e.service.db.one("SELECT COUNT(*) AS n FROM points_ledger WHERE reason='lottery.award'")['n'] == 2
    finally:
        other.db.close()


@pytest.mark.parametrize('publish_first', [False, True])
def test_legacy_manual_all_fee_threshold_and_account_guest_award_remain(e, publish_first):
    deadline = int(time.time()) + 7200
    lid = legacy_create(e, config(draw_mode='manual', deadline=deadline, reminder=True,
                                  entry_fee=40, min_points=50), publish=publish_first)
    if not publish_first:
        assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1, 'eligibility_version': 2}).status_code == 200
    old = e.service.row(lid)['config']
    assert old['eligibility_version'] == 1 and old['deadline'] == deadline and old['reminder']
    assert old['entry_fee'] == 40 and old['min_points'] == 50
    e.service.points.add('u2', 100, 'admin.mint', 'old-fee')
    assert enter(e, lid, expected_entry_fee=40).json()['joined']
    assert e.service.points.balance('u2') == 60
    e.service.stage_reminders(now=deadline - 3500)
    assert e.service.db.one("SELECT id FROM lottery_notices WHERE lottery_id=? AND kind='reminder'", (lid,))
    # The prior free mixed pool still allows guest account rewards and later binding.
    free = legacy_create(e, config(prizes=[POINTS]))
    assert join(e, free, GUEST)['prize_eligible'] is True
    draw(e, free)
    assert req(e, 'GET', '/' + free, GUEST).json()['my_award']['state'] == 'binding'
    frozen = snapshot(e, free)
    bind(e, GUEST, 'u3')
    assert req(e, 'POST', f'/{free}/claim', GUEST).json()['my_award']['state'] == 'delivered'
    assert e.service.points.balance('u3') == 10
    assert snapshot(e, free)['lottery_notices'] == frozen['lottery_notices']


@pytest.mark.parametrize('action', ['create', 'copy', 'save'])
def test_only_server_controls_new_version_preserving_other_policy_boundaries(e, action):
    raw = config(draw_mode='manual', entry_fee=40, min_points=5, device_check=True,
                 eligibility_version=1, public_rules_version=1, legacy=True)
    if action == 'create':
        lid = create(e, raw, publish=False)
    else:
        old = legacy_create(e, raw, publish=False)
        snapshot_config = e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (old,))
        if action == 'copy':
            lid = req(e, 'POST', f'/{old}/copy', json={'eligibility_version': 1}).json()['id']
            assert e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (old,)) == snapshot_config
        else:
            lid = old
            r = req(e, 'PUT', '/' + lid, json={**raw, 'revision': 1, 'device_check': False})
            assert r.status_code == 200
    c = e.service.row(lid)['config']
    assert c['eligibility_version'] == 2 and c['deadline'] is None and c['reminder'] is False
    assert all(c[k] == 0 for k in HIDDEN)
    assert c['device_check'] is (action == 'save')
    assert c['public_rules_version'] == (1 if action == 'save' else 2)
    view = req(e, 'GET', '/' + lid).json()
    assert ELIGIBILITY_RULES in view['rules'] and (FAIRNESS in view['rules']) is (action == 'save')
    if action == 'save':
        assert 'consent' in view
    else:
        assert 'consent' not in view


@pytest.mark.parametrize('public_version', [1, 2])
@pytest.mark.parametrize('business_version', [1, 2])
def test_rule_versions_are_orthogonal_and_frozen_notice_not_rewritten(e, public_version, business_version):
    lid = legacy_create(e, config(), publish=False)
    c = e.service.row(lid)['config']
    c.update(public_rules_version=public_version, eligibility_version=business_version)
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))
    req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
    old = snapshot(e, lid)
    for user in (MEMBER, GUEST, ADMIN):
        view = req(e, 'GET', '/' + lid, user).json()
        assert view['config']['eligibility_version'] == business_version
        expected = ELIGIBILITY_RULES if business_version == 2 else RULES
        assert view['rules'] == '免费参与。' + expected + (FAIRNESS if public_version == 1 else '')
    req(e, 'POST', f'/{lid}/preview')
    asyncio.run(e.service.publish(lid, 1, str(ADMIN)))
    assert snapshot(e, lid) == old


def test_new_preview_prize_labels_and_rules_are_public_and_no_detection_hints(e):
    lid = create(e, config(prizes=[POINTS, REG, MANUAL, shop_prize(e)]), publish=False)
    preview = req(e, 'POST', f'/{lid}/preview').json()
    assert '积分 × 10 · 仅已注册' in preview['text']
    assert '本人资格 × 2 · 仅未注册' in preview['text']
    assert '通用人工奖 × 10 · 通用' in preview['text']
    assert '奖品资格以开奖时的注册状态为准' in preview['text']
    assert 'PRIVATE-NOTE' not in preview['text']
    assert not any(t in preview['rules'] + preview['text'] for t in ('防小号', '多账号', 'IP', '浏览器', '设备', '关联'))
    req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
    public = req(e, 'GET', '/' + lid, GUEST).json()
    assert_public(public)
    assert [p['audience'] for p in public['config']['prizes']] == ['member', 'guest', 'all', 'member']
