"""Real isolated services: authoritative groups, paid snapshots, expiry/restart."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from test_economy import NOW, build, parallel
from test_economy import env as economy_env  # noqa: F401

from app.core.db import Database
from app.modules.economy_rules import DEFAULT_WHITELIST_CARD
from app.modules.shop import ShopError
from app.modules.whitelist_route import WhitelistRoute


@pytest.fixture
def env(request):
    e = request.getfixturevalue('economy_env')
    e.points.add('u', 50000, 'test')
    return e


def buy_white(e, days=0):
    item = e.shop.create(dict(DEFAULT_WHITELIST_CARD, duration_days=days, enabled=True))
    return e.shop.redeem('u', item['id'])['card_id']


def test_whitelist_purchase_use_changes_real_group_route_and_keeps_original_account_and_overrides(env):
    before = env.db.one("SELECT * FROM members WHERE emby_user_id='u'")
    route = WhitelistRoute(SimpleNamespace(members=env.members))
    with pytest.raises(HTTPException):
        route.member('u')
    card = buy_white(env)
    assert env.members.get('u')['group_id'] == 'standard'
    results = parallel(env, lambda e: e.bag.use('u', card))
    assert all(r == results[0] for r in results)
    assert results[0]['expires_at'] is None
    assert route.member('u')['group_id'] == 'whitelist'
    after = env.db.one("SELECT * FROM members WHERE emby_user_id='u'")
    assert {k: v for k, v in before.items() if k not in ('group_id', 'group_revision', 'updated_at')} == {
        k: v for k, v in after.items() if k not in ('group_id', 'group_revision', 'updated_at')
    }
    assert env.members.get('u')['expires_at_effective'] == before['expires_at']
    assert len(env.db.query("SELECT * FROM whitelist_card_grants")) == 1
    assert env.points.balance('u') == 45000
    second = buy_white(env, 3)
    with pytest.raises(ValueError, match='永久白名单'):
        env.bag.use('u', second)
    assert next(r for r in env.bag.items('u') if r['id'] == second)['used_at'] is None


def test_limited_renewal_and_upgrade_permanent_are_explicit_and_do_not_waste_cards(env):
    first = buy_white(env, 1)
    assert env.bag.use('u', first)['expires_at'] == NOW + 86400
    env.clock[0] += 400
    second = buy_white(env, 2)
    assert env.bag.use('u', second)['expires_at'] == NOW + 3 * 86400
    assert env.bag.use('u', second)['expires_at'] == NOW + 3 * 86400
    permanent = buy_white(env)
    assert env.bag.use('u', permanent)['expires_at'] is None
    env.clock[0] = NOW + 4 * 86400
    sync = AsyncMock(return_value={'ok': True})
    asyncio.run(env.bag.reconcile_expired(sync))
    assert env.members.get('u')['group_id'] == 'whitelist'
    sync.assert_not_called()


@pytest.mark.parametrize('status', ['suspended', 'pending'])
def test_disabled_whitelist_use_refused_without_consumption_and_post_use_disable_still_blocks(env, status):
    card = buy_white(env)
    env.members.set_status('u', status)
    with pytest.raises(ValueError, match='不可用'):
        env.bag.use('u', card)
    assert env.bag.items('u')[0]['used_at'] is None
    env.members.set_status('u', 'active')
    env.bag.use('u', card)
    env.members.set_status('u', status)
    with pytest.raises(HTTPException):
        WhitelistRoute(SimpleNamespace(members=env.members)).member('u')


def test_permanent_group_card_does_not_make_expired_base_account_permanent(env):
    card = buy_white(env)
    env.bag.use('u', card)
    expiry = env.members.get('u')['expires_at_effective']
    env.clock[0] = expiry
    assert env.members.get('u')['state'] == 'expired'
    with pytest.raises(HTTPException):
        WhitelistRoute(SimpleNamespace(members=env.members)).member('u')


def test_limited_expiry_authority_denies_before_worker_then_restart_restores_and_retries_remote(env):
    card = buy_white(env, 1)
    before = env.members.get('u')
    env.bag.use('u', card)
    env.clock[0] += 86400
    assert env.db.one("SELECT group_id FROM members WHERE emby_user_id='u'")['group_id'] == 'whitelist'
    assert env.members.get('u')['group_id'] == 'standard'
    with pytest.raises(HTTPException):
        WhitelistRoute(SimpleNamespace(members=env.members)).member('u')
    restart_db = Database(env.db.path)
    try:
        restarted = build(restart_db)
        sync = AsyncMock(return_value={'ok': False})
        asyncio.run(restarted.bag.reconcile_expired(sync))
        assert restarted.members.get('u')['group_id'] == 'standard'
        assert restarted.members.get('u')['overrides'] == before['overrides']
        assert restarted.members.get('u')['expires_at_effective'] == before['expires_at_effective']
        assert '未确认' in restarted.bag.items('u')[0]['expiry_error']
        asyncio.run(restarted.bag.reconcile_expired(sync))
        assert sync.call_count == 1
        env.clock[0] += 60
        sync.return_value = {'ok': True}
        asyncio.run(restarted.bag.reconcile_expired(sync))
        assert sync.call_count == 2 and not restarted.bag.items('u')[0]['expiry_error']
        asyncio.run(restarted.bag.reconcile_expired(sync))
        assert sync.call_count == 2
    finally:
        restart_db.close()


@pytest.mark.parametrize('new_group', ['vip', 'whitelist'])
def test_manual_group_intent_even_same_value_wins_expiry(env, new_group):
    card = buy_white(env, 1)
    env.bag.use('u', card)
    prior_revision = env.members.get('u')['group_revision']
    env.members.upsert('u', 'u', {'group_id': new_group}, actor='admin')
    assert env.members.get('u')['group_revision'] == prior_revision + 1
    env.clock[0] += 86401
    sync = AsyncMock(return_value={'ok': True})
    asyncio.run(env.bag.reconcile_expired(sync))
    assert env.members.get('u')['group_id'] == new_group
    assert env.db.one('SELECT status FROM whitelist_card_grants')['status'] == 'protected'
    sync.assert_not_called()


def test_restore_failure_is_fail_closed_and_retries_without_overwriting_other_rights(env, monkeypatch):
    import app.modules.whitelist_cards as module
    card = buy_white(env, 1)
    env.bag.use('u', card)
    env.clock[0] += 86400
    original = module._restore_local
    monkeypatch.setattr(module, '_restore_local', lambda *a: (_ for _ in ()).throw(RuntimeError('isolated failure')))
    sync = AsyncMock(return_value={'ok': True})
    asyncio.run(env.bag.reconcile_expired(sync))
    assert env.members.get('u')['group_id'] == 'standard'
    assert env.db.one('SELECT status,error FROM whitelist_card_grants')['status'] == 'active'
    assert '未确认' in env.bag.items('u')[0]['expiry_error']
    monkeypatch.setattr(module, '_restore_local', original)
    env.clock[0] += 60
    asyncio.run(env.bag.reconcile_expired(sync))
    assert env.db.one("SELECT group_id FROM members WHERE emby_user_id='u'")['group_id'] == 'standard'
    assert not env.bag.items('u')[0]['expiry_error']


def test_custom_public_description_paid_notice_snapshot_privacy_and_expiry_preserve_history(env):
    item = env.shop.create({'kind': 'custom', 'name': '隔离测试自定义', 'cost': 123,
                           'description': '公开商品介绍', 'purchase_notice': '已购买私密内容'})
    assert item['retention_days'] == 7
    public = env.shop.get(item['id'])
    assert 'purchase_notice' not in public and '已购买私密内容' not in json.dumps(env.shop.items())
    result = env.shop.redeem('u', item['id'], request_id='custom-once', expected_spec=public)
    assert env.shop.redeem('u', item['id'], request_id='custom-once', expected_spec=public) == result
    assert env.points.balance('u') == 50000 - 123 and len(env.shop.orders()) == 1
    cid = result['card_id']
    env.shop.update(item['id'], {'purchase_notice': '后来修改内容', 'retention_days': 1})
    assert env.bag.custom_notice('u', cid)['notice'] == '已购买私密内容'
    assert env.bag.custom_notice('u', cid)['expires_at'] == NOW + 7 * 86400
    with pytest.raises(ValueError, match='不属于'):
        env.bag.custom_notice('v', cid)
    with pytest.raises(ValueError, match='无需使用'):
        env.bag.use('u', cid)
    env.bag.mark_notice('u', cid, False)
    assert env.bag.custom_notice('u', cid)['notice_state'] == 'failed'
    assert env.bag.custom_notice('u', cid)['notice'] == '已购买私密内容'
    history, ledger = env.shop.orders(), env.points.ledger('u')
    env.clock[0] += 7 * 86400
    assert not env.bag.items('u')
    with pytest.raises(ValueError, match='保留期'):
        env.bag.custom_notice('u', cid)
    assert env.shop.orders() == history and env.points.ledger('u') == ledger
    assert json.loads(history[0]['spec_json'])['purchase_notice'] == '已购买私密内容'


def test_custom_paid_notice_edit_invalidates_old_confirmation_and_invalid_spec_rejected(env):
    item = env.shop.create({'kind': 'custom', 'name': '隔离', 'cost': 1, 'purchase_notice': '说明'})
    spec = env.shop.get(item['id'])
    env.shop.update(item['id'], {'purchase_notice': '新版说明'})
    with pytest.raises(ShopError, match='规格/价格已变化'):
        env.shop.redeem('u', item['id'], expected_spec=spec)
    assert not env.bag.items('u') and not env.shop.orders()
    for changes in ({'purchase_notice': ''}, {'retention_days': 0}, {'retention_days': True}, {'amount': 2}):
        with pytest.raises(ShopError):
            env.shop.update(item['id'], changes)
