"""Actual purchase callbacks + real SQLite outbox; all Telegram calls remain isolated."""
import asyncio
import copy
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_economy_bot import env as economy_bot_env  # noqa: F401
from test_tg_interaction_context import GROUP, VIEWER, click, command

from app.core.db import Database
from app.main import app
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.shop import ShopService
from app.modules.shop_notices import ShopNotices, view


@pytest.fixture
def env(request):
    e = request.getfixturevalue('economy_bot_env')
    e.cfg['shop_purchase_broadcast'] = True
    e.bot._shop = e.services.shop
    e.services.shop._notice_config = lambda: e.cfg
    e.services.points.add('u1', 2000, 'isolated.fixture')
    e.db.execute("UPDATE members SET tg_display_name='<小雨&>',tg_username='private-handle' WHERE emby_user_id='u1'")
    e.item = e.services.shop.create({'kind': 'custom', 'name': '<好物&>', 'cost': 10, 'amount': 1, 'enabled': True,
                                    'purchase_notice': 'secret-paid-content credential password redeem-code'})
    return e


def public_messages(e):
    return [p for m,p in e.tg.calls if m == 'sendMessage' and '好物入袋' in p.get('text','')]


async def buy(e):
    mid = await command(e, '/start', chat=VIEWER, user=VIEWER)
    await click(e, 'shop', mid, chat=VIEWER, user=VIEWER)
    await click(e, f'buy:{e.item["id"]}', mid, chat=VIEWER, user=VIEWER)
    await asyncio.gather(*[click(e, f'buyok:{e.item["id"]}', mid, chat=VIEWER, user=VIEWER) for _ in range(3)])
    return mid


def test_real_purchase_handler_concurrent_confirm_one_public_name_product_only_per_authorized_group(env):
    env.cfg['group_interaction_chats'].append('-1009001')
    async def run():
        await buy(env)
        assert len(env.services.shop.orders()) == 1
        assert len(env.db.query('SELECT * FROM shop_notices')) == 2
        assert not public_messages(env)
        await asyncio.gather(env.bot._shop_notice_tick(), env.bot._shop_notice_tick())
        await env.bot._shop_notice_tick()
        rows = public_messages(env)
        assert len(rows) == 2 and {p['chat_id'] for p in rows} == {str(GROUP), '-1009001'}
        for p in rows:
            assert '&lt;小雨&amp;&gt;' in p['text'] and '&lt;好物&amp;&gt;' in p['text']
            for secret in ('余额', '消耗', 'private-handle', 'ViewerA', 'u1', 'secret-paid-content', 'credential', 'password', 'redeem-code'):
                assert secret not in p['text']
            assert 'message_thread_id' not in p
        assert all(r['state'] == 'sent' and r['message_id'] for r in env.db.query('SELECT * FROM shop_notices'))
    asyncio.run(run())


def test_default_disabled_period_produces_no_history_and_enable_never_backfills(env):
    env.cfg.pop('shop_purchase_broadcast')
    for i in range(3):env.services.shop.redeem('u1', env.item['id'], request_id=str(i))
    assert not env.db.query('SELECT * FROM shop_notices')
    env.cfg['shop_purchase_broadcast'] = True
    asyncio.run(env.bot._shop_notice_tick())
    assert not public_messages(env)
    env.services.shop.redeem('u1', env.item['id'], request_id='new')
    asyncio.run(env.bot._shop_notice_tick())
    assert len(public_messages(env)) == 1
    env.services.shop.redeem('u1', env.item['id'], request_id='0')
    assert len(env.db.query('SELECT * FROM shop_notices')) == 1


def test_atomic_failed_purchase_rolls_back_debit_grant_order_and_public_task(env, monkeypatch):
    from app.modules import shop
    before = copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
    monkeypatch.setattr(shop, 'save_receipt', lambda *a: (_ for _ in ()).throw(ValueError('injected final transaction failure')))
    asyncio.run(buy(env))
    assert env.db.query('SELECT * FROM points_ledger') == before
    assert not env.services.shop.orders() and not env.services.bag.items('u1')
    assert not env.db.query('SELECT * FROM shop_notices')
    asyncio.run(env.bot._shop_notice_tick())
    assert not public_messages(env)


def test_optional_scheduling_failure_does_not_undo_purchase_or_reannounce_replay(env, monkeypatch):
    monkeypatch.setattr('app.modules.shop_notices.record', lambda *a: (_ for _ in ()).throw(RuntimeError('outbox unavailable')))
    result = env.services.shop.redeem('u1', env.item['id'], request_id='once')
    assert result['ok'] and len(env.services.bag.items('u1')) == 1 and len(env.services.shop.orders()) == 1
    assert not env.db.query('SELECT * FROM shop_notices')
    monkeypatch.undo()
    assert env.services.shop.redeem('u1', env.item['id'], request_id='once') == result
    assert not env.db.query('SELECT * FROM shop_notices')


def test_two_sqlite_connections_same_purchase_request_one_charge_and_notification(env):
    paths = [Database(env.db.path) for _ in range(2)]
    before = env.services.points.balance('u1')
    def redeem(db):
        from app.modules.groups import GroupService
        from app.modules.members import MemberService
        from app.modules.points import PointsService
        return ShopService(db, MemberService(db, GroupService(db)), PointsService(db), notice_config=lambda: env.cfg).redeem('u1', env.item['id'], request_id='parallel')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:results = list(pool.map(redeem, paths))
    finally:
        for d in paths:d.close()
    assert results[0] == results[1] and env.services.points.balance('u1') == before-10
    assert len(env.services.shop.orders()) == len(env.db.query('SELECT * FROM shop_notices')) == 1


@pytest.mark.parametrize('failure', ['failed', 'unknown'])
def test_permission_and_unknown_transport_never_retry_or_change_purchase(env, failure):
    env.services.shop.redeem('u1', env.item['id'])
    baseline = copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
    async def bad(method, body, **kw):
        assert method == 'sendMessage';env.tg.calls.append((method, body));CALL_DELIVERY.set({'state': failure});
    env.bot._call = bad
    asyncio.run(env.bot._shop_notice_tick());asyncio.run(env.bot._shop_notice_tick())
    assert env.db.one('SELECT * FROM shop_notices')['state'] == failure
    assert len(public_messages(env)) == 1 and env.db.query('SELECT * FROM points_ledger') == baseline
    reopened = Database(env.db.path)
    try:assert ShopNotices(reopened).claim(env.cfg, now=time.time()+10000) is None
    finally:reopened.close()


@pytest.mark.parametrize('exception', [TimeoutError, asyncio.CancelledError])
def test_actual_transport_timeout_or_cancel_purchase_stays_success_and_restart_no_resend(env, exception):
    env.services.shop.redeem('u1', env.item['id'])
    async def interrupted(*a, **kw):
        raise exception()
    env.bot._call = interrupted
    if exception is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):asyncio.run(env.bot._shop_notice_tick())
        ShopNotices(env.db).claim(env.cfg, now=time.time()+91)
    else:asyncio.run(env.bot._shop_notice_tick())
    assert env.db.one('SELECT * FROM shop_notices')['state'] == 'unknown'
    assert len(env.services.shop.orders()) == len(env.services.bag.items('u1')) == 1
    assert ShopNotices(env.db).claim(env.cfg, now=time.time()+10000) is None


@pytest.mark.parametrize('refusal', ['insufficient', 'disabled', 'changed'])
def test_refused_real_confirmation_has_no_order_money_grant_or_notification(env, refusal):
    async def run():
        mid = await command(env, '/start', chat=VIEWER, user=VIEWER)
        await click(env, 'shop', mid, chat=VIEWER, user=VIEWER)
        await click(env, f'buy:{env.item["id"]}', mid, chat=VIEWER, user=VIEWER)
        if refusal == 'insufficient':env.services.points.add('u1', -env.services.points.balance('u1'), 'isolated.fixture')
        else:env.services.shop.update(env.item['id'], {'enabled': False} if refusal == 'disabled' else {'cost': 11})
        before = copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
        await click(env, f'buyok:{env.item["id"]}', mid, chat=VIEWER, user=VIEWER)
        await env.bot._shop_notice_tick()
        assert env.db.query('SELECT * FROM points_ledger') == before
        assert not env.services.shop.orders() and not env.services.bag.items('u1')
        assert not env.db.query('SELECT * FROM shop_notices') and not public_messages(env)
    asyncio.run(run())


def test_crash_after_claim_restart_unknown_no_late_ack_or_blind_repeat(env):
    env.services.shop.redeem('u1', env.item['id'])
    now = time.time();service = ShopNotices(env.db);job = service.claim(env.cfg, now=now)
    assert job
    other = Database(env.db.path)
    try:
        assert ShopNotices(other).claim(env.cfg, now=now+91) is None
        service.finish(job, {'message_id': 999}, {}, now=now+92)
        assert env.db.one('SELECT * FROM shop_notices')['state'] == 'unknown'
    finally:other.close()
    asyncio.run(env.bot._shop_notice_tick())
    assert not public_messages(env)


def test_known_rate_limit_retry_is_bounded_and_no_purchase_change(env):
    env.services.shop.redeem('u1', env.item['id'])
    async def rate(method, body, **kw):
        env.tg.calls.append((method, body));CALL_DELIVERY.set({'state': 'retry', 'retry_after': 5});
    env.bot._call = rate
    for _ in range(5):
        env.db.execute('UPDATE shop_notices SET due_at=0')
        asyncio.run(env.bot._shop_notice_tick())
    assert len(public_messages(env)) == 3
    assert env.db.one('SELECT * FROM shop_notices')['state'] == 'failed'
    assert len(env.services.bag.items('u1')) == 1


@pytest.mark.parametrize('change', ['disabled', 'removed_group', 'changed_bot'])
def test_queued_notification_never_crosses_current_authorization_or_bot(env, change):
    env.services.shop.redeem('u1', env.item['id'])
    if change == 'disabled':env.cfg['shop_purchase_broadcast'] = False
    elif change == 'removed_group':env.cfg['group_interaction_chats'] = []
    else:env.cfg['bot_token'] = '999:local-only'
    asyncio.run(env.bot._shop_notice_tick())
    assert not public_messages(env)
    if change != 'changed_bot':
        assert env.db.one('SELECT * FROM shop_notices')['state'] == 'cancelled'
        env.cfg.update(shop_purchase_broadcast=True, group_interaction_chats=[str(GROUP)])
        asyncio.run(env.bot._shop_notice_tick());assert not public_messages(env)


def test_no_interaction_group_never_uses_registration_notification_destination(env):
    env.cfg.update(group_interaction_chats=[], registration_notify_chat_id='-1009999')
    env.services.shop.redeem('u1', env.item['id'])
    asyncio.run(env.bot._shop_notice_tick())
    assert not env.db.query('SELECT * FROM shop_notices') and not public_messages(env)


def test_missing_public_name_is_neutral_never_private_login(env):
    env.db.execute("UPDATE members SET tg_display_name='' WHERE emby_user_id='u1'")
    env.services.shop.redeem('u1', env.item['id'])
    assert '成员' in view(env.db.one('SELECT * FROM shop_notices'))


def test_setting_is_default_off_partial_save_preserves_and_main_binds_purchase_config():
    with TestClient(app) as c:
        c.auth = ('admin', 'change-me')
        assert not c.get('/api/settings/telegram').json()['shop_purchase_broadcast']
        assert c.post('/api/settings/telegram', json={'enabled': True, 'bot_token': '123:local-only', 'group_interaction_chats': [str(GROUP)], 'shop_purchase_broadcast': True}).status_code == 200
        assert c.post('/api/settings/telegram', json={'menu_logo_url': ''}).json()['shop_purchase_broadcast'] is True
        assert app.state.shop._notice_config()['shop_purchase_broadcast'] is True
