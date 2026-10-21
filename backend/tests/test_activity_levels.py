"""Level rights, exact migration and renewable/deletable state exclusion in isolation."""
import asyncio
import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from test_economy import build, proof

from app.adapters.mock import MockEmby
from app.core.db import Database
from app.modules.level_lifecycle import LevelLifecycle
from app.modules.level_migration import MIGRATION_START, LevelMigration
from app.modules.levels import DAY, LevelService, defaults, validate
from app.modules.watch_rewards import ActivityService

NOW = int(MIGRATION_START+20*DAY+12*3600)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    db = Database(tmp_path/'levels.db')
    db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(MIGRATION_START+5*DAY),))
    e = build(db)
    e.groups.seed_defaults()
    e.members.upsert('u', 'Demo', {'group_id': 'standard', 'expires_at': NOW+DAY})
    e.activity = ActivityService(db, e.points)
    e.levels = LevelService(db, e.members, e.points, e.activity)
    e.members.bind_levels(e.levels)
    e.config = defaults()
    e.config['enabled'] = True
    e.levels.save_config(e.config, 0)
    db.execute("INSERT INTO activity_members(emby_user_id,enrolled_at) VALUES('u',?)", (NOW,))
    yield e
    db.close()


def qualify(e, uid='u'):
    for i in range(3):
        proof(e.db, uid, now=NOW-i*DAY, seconds=1600, key=uid+str(i))


def test_config_validation_and_revision(env):
    with pytest.raises(ValueError, match='变化'):
        env.levels.save_config(env.config, 0)
    for mutate in [lambda c: c['levels'][1].update(threshold=0),
                   lambda c: c['levels'][7].update(threshold=7201),
                   lambda c: c['levels'][0].update(price=-1)]:
        config = copy.deepcopy(env.config)
        mutate(config)
        with pytest.raises(ValueError): validate(config)


def test_upgrade_changes_only_base_rights_preserves_personal_cards_and_special(env):
    env.members.set_overrides('u', {'bandwidth_limit_kbps': 130000, 'max_streams': 5, 'allow_download': 0})
    before = env.members.get('u')
    env.db.execute("INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) VALUES('u',7200,'test','seed',?)", (NOW,))
    member = env.members.get('u')
    assert member['activity_level']['level'] == 7
    assert member['bandwidth_limit_kbps'] == 130000 and member['max_streams'] == 5
    assert member['traffic_quota_bytes'] == 0  # retired, level keeps bandwidth/concurrency only
    assert not member['allow_download'] and member['allow_transcode'] == before['allow_transcode']
    env.levels.grant_special('u', 'test')
    env.db.execute("INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) VALUES('u',-7200,'test','reset',?)", (NOW,))
    assert env.members.get('u')['activity_level']['level'] == 8
    assert env.members.get('u')['activity_level']['price'] == 1


def test_zero_viewing_can_renew_but_funds_window_and_manual_restrictions_remain(env):
    env.points.add('u', 100, 'admin.adjust')
    assert not env.db.query('SELECT * FROM watch_verified_samples')
    env.members.upsert('u', '', {'status': 'suspended'})
    env.db.execute("UPDATE members SET traffic_used_bytes=12345 WHERE emby_user_id='u'")
    env.db.execute("UPDATE members SET request_used=2 WHERE emby_user_id='u'")
    result = env.levels.renew('u', 'request')
    assert result['expires_at'] == NOW+31*DAY and result['cost'] == 80
    assert env.levels.renew('u', 'request') == result
    member = env.members.get('u')
    assert member['status'] == 'suspended' and env.db.one("SELECT traffic_used_bytes FROM members WHERE emby_user_id='u'")['traffic_used_bytes'] == 12345 and member['request_used'] == 2
    assert env.points.balance('u') == 20
    with pytest.raises(ValueError, match='72小时'): env.levels.renew('u', 'another')
    env.members.upsert('u', '', {'expires_at': NOW-1})
    with pytest.raises(ValueError, match='积分不足'): env.levels.renew('u', 'poor')
    assert env.members.get('u')['expires_at_effective'] == NOW-1


def test_concurrent_renew_without_viewing_is_one_charge(env):
    assert not env.db.query('SELECT * FROM watch_verified_samples')
    env.points.add('u', 200, 'admin.adjust')
    db2 = Database(env.db.path)
    try:
        e2 = build(db2)
        other = LevelService(db2, e2.members, e2.points, ActivityService(db2, e2.points))
        e2.members.bind_levels(other)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda s: s.renew('u', 'same'), [env.levels, other]))
        assert results[0] == results[1] and env.points.balance('u') == 120
    finally:
        db2.close()


def test_auto_opt_in_without_viewing_and_new_activation_only_once(env):
    assert not env.db.query('SELECT * FROM watch_verified_samples')
    env.points.add('u', 100, 'admin.adjust')
    with pytest.raises(ValueError, match='未开启'): env.levels.renew('u', 'auto', automatic=True)
    env.levels.set_auto('u', True)
    assert env.levels.renew('u', 'auto', automatic=True)['cost'] == 80
    env.members.upsert('new', 'New', {'group_id': 'standard', 'status': 'pending', 'expires_at': NOW})
    assert not env.members.get('new').get('activity_managed')
    env.members.set_status('new', 'active')
    assert env.members.get('new')['expires_at_effective'] == NOW+30*DAY
    env.members.set_status('new', 'active')
    assert env.members.get('new')['expires_at_effective'] == NOW+30*DAY


def test_migration_strict_threshold_free_same_date_and_idempotence(env):
    env.db.execute('DELETE FROM activity_members')
    # Create old users while cutover is disabled.
    config = copy.deepcopy(env.config); config['enabled'] = False
    env.levels.save_config(config, 1)
    for uid in ('active', 'exact', 'three', 'poor', 'manual', 'white', 'long'):
        env.members.upsert(uid, uid, {'group_id': 'whitelist' if uid == 'white' else 'standard',
                                     'expires_at': NOW+2*DAY, 'status': 'suspended' if uid == 'manual' else 'active'})
    env.members.set_overrides('long', {'expires_at_override': int(NOW+60*DAY)})
    for uid, durations in [('active', [1501]*4), ('exact', [1500]*4), ('three', [2100]*3)]:
        for i, seconds in enumerate(durations):
            proof(env.db, uid, now=NOW-i*DAY, seconds=seconds, key=uid+str(i))
    migration = LevelMigration(env.levels)
    preview = migration.preview(NOW)
    rows = {r['user_id']: r for r in preview['members']}
    assert rows['active']['level'] == 8
    assert rows['exact']['level'] == rows['three']['level'] == 1
    assert rows['white']['action'] == 'exempt' and rows['long']['action'] == 'exception'
    assert all(r['cost'] == 0 for r in rows.values())
    assert not preview['coverage_complete']
    assert not env.db.query('SELECT * FROM activity_members')
    result = migration.apply('migration', preview['hash'], NOW)
    assert migration.apply('migration', preview['hash'], NOW) == result
    assert env.points.balance('active') == 0
    for uid in result['migrated']:
        assert env.db.one('SELECT expires_at FROM members WHERE emby_user_id=?', (uid,))['expires_at'] == NOW+30*DAY
    assert env.members.get('manual')['status'] == 'suspended'
    env.levels.save_config(env.config, 2)
    assert env.members.get('active')['activity_level']['level'] == 8
    assert env.members.get('poor')['activity_level']['level'] == 1
    assert env.members.get('long')['expires_at_effective'] == NOW+60*DAY
    assert not env.db.query('SELECT * FROM growth_ledger')
    assert all(r['action'] != 'migrate' for r in migration.preview(NOW)['members'])


def test_migration_rechecks_changed_facts(env):
    env.db.execute('DELETE FROM activity_members')
    migration = LevelMigration(env.levels)
    preview = migration.preview(NOW)
    env.points.add('u', 1, 'admin.adjust')
    with pytest.raises(ValueError, match='变化'): migration.apply('stale', preview['hash'], NOW)
    assert not env.db.query('SELECT * FROM activity_members')


@pytest.mark.asyncio
async def test_delete_72h_boundary_failure_retry_and_retained_history(env):
    env.members.upsert('u', '', {'expires_at': NOW-3*DAY})
    env.points.add('u', 10, 'admin.adjust')
    emby = MockEmby()
    emby._users['u'] = {'Id': 'u', 'Policy': {'IsAdministrator': False}}
    emby.list_users = AsyncMock(return_value=[{'Id': 'u', 'Policy': {'IsAdministrator': False}}])
    emby.delete_user = AsyncMock(return_value=False)
    service = LevelLifecycle(env.levels, emby)
    assert service.preview(NOW-1) == []
    assert len(service.preview(NOW)) == 1
    with pytest.raises(ValueError, match='未启用'): await service.delete('u')
    assert not emby.delete_user.called
    config = copy.deepcopy(env.config); config['delete_enabled'] = True
    env.levels.save_config(config, 1)
    result = await service.delete('u')
    assert not result['deleted'] and env.members.get('u')
    assert env.points.balance('u') == 10
    with pytest.raises(ValueError, match='待确认'): env.levels.renew('u', 'late')
    with pytest.raises(sqlite3.IntegrityError): env.members.renew('u', 30)
    emby.delete_user.return_value = True
    assert (await service.delete('u'))['deleted']
    assert env.members.get('u') is None
    assert env.points.balance('u') == 10


@pytest.mark.asyncio
async def test_renew_delete_race_and_remote_unknown_fail_closed(env):
    qualify(env)
    env.points.add('u', 100, 'admin.adjust')
    env.members.upsert('u', '', {'expires_at': NOW-3*DAY})
    config = copy.deepcopy(env.config); config['delete_enabled'] = True
    env.levels.save_config(config, 1)
    entered = asyncio.Event(); release = asyncio.Event()
    emby = MockEmby()
    emby._users['u'] = {'Id': 'u', 'Policy': {'IsAdministrator': False}}
    emby.list_users = AsyncMock(return_value=[{'Id': 'u', 'Policy': {'IsAdministrator': False}}])
    async def waiting(uid):
        entered.set(); await release.wait(); raise TimeoutError('unknown')
    emby.delete_user = waiting
    service = LevelLifecycle(env.levels, emby)
    task = asyncio.create_task(service.delete('u'))
    await entered.wait()
    with pytest.raises(ValueError, match='待确认'): env.levels.renew('u', 'racing')
    release.set()
    assert not (await task)['deleted']
    assert env.members.get('u')['activity_lifecycle'] == 'deleting'
    assert env.points.balance('u') == 100


def test_migration_old_dated_samples_only_before_cutover_never_issue_rewards(env):
    from app.modules.level_migration import migration_qualification, verification_cutover
    from app.modules.levels import qualification
    cutoff = MIGRATION_START+5*DAY
    # Three old dates, one strict date: qualifies only through authorized legacy window.
    for i in range(3):
        start = MIGRATION_START+i*DAY+3600
        env.db.execute('INSERT INTO watch_samples VALUES(?,?,?,?,?)', (str(i), 'u', start, start+1600, 1600))
        env.db.execute('INSERT INTO watch_samples VALUES(?,?,?,?,?)', ('overlap'+str(i), 'u', start, start+1600, 1600))
    proof(env.db, 'u', now=cutoff+DAY+1600, seconds=1600, key='strict')
    env.db.execute('INSERT INTO watch_samples VALUES(?,?,?,?,?)', ('after', 'u', cutoff+2*DAY, cutoff+3*DAY, DAY))
    result = migration_qualification(env.db, 'u', NOW, cutoff)
    assert result['seconds'] == 6400 and result['days'] == 4 and result['eligible']
    assert result['sources'] == {'watch_samples':4800, 'watch_verified_samples':1600}
    assert qualification(env.db, 'u', NOW)['seconds'] == 1600
    env.activity.activate(NOW)
    assert env.activity.settle(NOW)['points'] == 0
    assert env.points.balance('u') == 0
    assert verification_cutover(env.db, NOW) == (cutoff, 'watch_verified_since')
    env.db.execute("DELETE FROM meta WHERE key='watch_verified_since'")
    assert verification_cutover(env.db, NOW)[1] == 'first_verified_sample'


def test_migration_cross_cutover_midnight_and_strict_total(env):
    from app.modules.level_migration import migration_qualification
    midnight = MIGRATION_START+5*DAY
    env.db.execute('INSERT INTO watch_samples VALUES(?,?,?,?,?)', ('old', 'u', midnight-600, midnight+600, 1200))
    proof(env.db, 'u', now=midnight+600, seconds=1200, key='new')
    result = migration_qualification(env.db, 'u', NOW, midnight)
    assert result['seconds'] == 1200 and result['days'] == 2
    assert result['sources'] == {'watch_samples':600, 'watch_verified_samples':600}


@pytest.mark.asyncio
async def test_whitelist_temporary_cards_and_unsettled_funds_block_deletion(env):
    from app.modules.economy_rules import economy_write
    from app.modules.play_money import CashBook
    env.members.upsert('u', '', {'expires_at': NOW-4*DAY})
    env.points.add('u', 10, 'admin.adjust')
    config = copy.deepcopy(env.config); config['delete_enabled'] = True
    env.levels.save_config(config, 1)
    service = LevelLifecycle(env.levels, MockEmby())
    with economy_write(env.db) as conn:
        CashBook(env.points).reserve(conn, 'test', 'room', 'u', 5)
    assert service.preview(NOW)[0]['blocked']
    with pytest.raises(ValueError, match='未结算'): service.claim('u', NOW)
    with economy_write(env.db) as conn:
        CashBook(env.points).release(conn, 'test', 'room', 'u')
    env.members.upsert('u', '', {'group_id':'whitelist'})
    assert service.preview(NOW) == []
    assert env.members.get('u')['activity_level']['level'] == 1
    assert env.members.get('u')['billing_mode'] == 'none'


@pytest.mark.asyncio
async def test_remote_admin_promotion_rechecked_and_unknown_deletion_recoverable(env):
    env.members.upsert('u', '', {'expires_at': NOW-4*DAY})
    config = copy.deepcopy(env.config); config['delete_enabled'] = True
    env.levels.save_config(config, 1)
    emby = MockEmby()
    emby._users['u'] = {'Id':'u', 'Policy':{'IsAdministrator':True}}
    # First observation says ordinary; immediate deletion adapter sees new admin flag.
    emby.list_users = AsyncMock(return_value=[{'Id':'u','Policy':{'IsAdministrator':False}}])
    service = LevelLifecycle(env.levels, emby)
    result = await service.delete('u')
    assert not result['deleted'] and env.members.get('u')
    assert (await service.recover('u'))['recovered']
    assert env.members.get('u')['activity_lifecycle'] == 'ready'
    # Confirmed 404/absence after a lost deletion response permits local cleanup.
    del emby._users['u']
    emby.list_users = AsyncMock(return_value=[])
    assert (await service.delete('u'))['deleted']
    assert env.members.get('u') is None


@pytest.mark.asyncio
async def test_scheduled_auto_renew_notices_and_old_cleanup_exemption(env):
    from app.modules.level_lifecycle import LevelLifecyclePlugin
    ctx = env.checkin.ctx
    ctx.levels = env.levels
    ctx.telegram = type('Bot', (), {'enabled':True, 'send':AsyncMock(return_value=True)})()
    env.db.execute("UPDATE members SET tg_user_id='123',last_seen_at=?,created_at=? WHERE emby_user_id='u'", (NOW-20*DAY,NOW-20*DAY))
    env.registry.save('activity_lifecycle', enabled=True)
    plugin = env.registry.get('activity_lifecycle')
    result = await plugin.run({})
    assert result['已提醒'] == 1
    from datetime import datetime

    from app.modules.economy_rules import BEIJING
    text = ctx.telegram.send.call_args.args[1]
    assert '续费30天需80积分，余额缺口80' in text and text.count('北京时间') == 2
    assert not any(word in text for word in ('80分钟', '合格日', '达标续费'))
    expiry = env.members.get('u')['expires_at_effective']
    assert datetime.fromtimestamp(expiry, BEIJING).strftime('%Y-%m-%d %H:%M') in text
    assert (await plugin.run({}))['已提醒'] == 0
    await env.registry.get('inactive_cleanup').run({'days':7})
    assert env.members.get('u')['status'] == 'active'
    assert not env.db.query('SELECT * FROM watch_verified_samples')
    env.points.add('u', 100, 'admin.adjust')
    env.levels.set_auto('u', True)
    assert (await plugin.run({}))['已续期'] == 1
    assert env.points.balance('u') == 20
    assert (await plugin.run({}))['已续期'] == 0
    assert isinstance(plugin, LevelLifecyclePlugin)


def test_pending_activation_and_startup_term_rollback_together(env, monkeypatch):
    env.members.upsert('pending', 'Pending', {'group_id':'standard', 'status':'pending'})
    def fail(*args): raise RuntimeError('injected term write failure')
    monkeypatch.setattr(env.levels, 'write_expiry', fail)
    with pytest.raises(RuntimeError): env.members.set_status('pending', 'active')
    assert env.members.get('pending')['status'] == 'pending'
    assert env.db.one("SELECT * FROM activity_members WHERE emby_user_id='pending'") is None
    with pytest.raises(RuntimeError): env.members.upsert('new', 'New', {'group_id':'standard'})
    assert env.members.get('new') is None


def test_active_cards_survive_upgrade_and_expire_without_erasing_personal_rights(env, monkeypatch):
    from app.modules.economy_rules import DEFAULT_CARDS, economy_write
    from app.modules.inventory import InventoryService
    env.members.set_overrides('u', {'bandwidth_limit_kbps':130000, 'max_streams':5})
    with economy_write(env.db) as conn:
        ids = [InventoryService.add(conn, 'u', DEFAULT_CARDS[i], 'test', NOW) for i in (1,2)]
    for card_id in ids: env.bag.use('u', card_id, now=NOW)
    env.db.execute("INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) VALUES('u',7200,'test','upgrade',?)", (NOW,))
    member = env.members.get('u')
    assert member['bandwidth_limit_kbps'] == 140000 and member['max_streams'] == 6
    monkeypatch.setattr('time.time', lambda: NOW+31*DAY)
    member = env.members.get('u')
    assert member['bandwidth_limit_kbps'] == 130000 and member['max_streams'] == 5
    assert member['activity_level']['level'] == 7


def test_expiry_retention_read_and_renew_only_no_shop_or_card_use(env):
    from app.modules.economy_rules import DEFAULT_CARDS, economy_write
    from app.modules.inventory import InventoryService
    with economy_write(env.db) as conn:
        card = InventoryService.add(conn, 'u', DEFAULT_CARDS[0], 'test', NOW)
    item = env.shop.create(dict(DEFAULT_CARDS[0], enabled=True))
    env.points.add('u', 1000, 'admin.adjust')
    env.members.upsert('u', '', {'expires_at':NOW-1})
    from app.modules.shop import ShopError
    with pytest.raises(ShopError, match='仅可查看'): env.shop.redeem('u', item['id'])
    with pytest.raises(ValueError, match='仅可查看'): env.bag.use('u', card)
    assert env.points.balance('u') == 1000
    assert env.bag.items('u')[0]['used_at'] is None
    assert env.levels.status('u')['window_open']


@pytest.mark.asyncio
async def test_live_deletion_adapter_absence_requires_explicit_option_and_local_authority(monkeypatch):
    import httpx

    from app.adapters.live import LiveEmby
    adapter = LiveEmby(lambda:{'enabled':True,'url':'https://emby.example.invalid','api_key':'test-only'})
    requests=[]
    def handler(request):
        requests.append(request.method)
        return httpx.Response(404)
    monkeypatch.setattr(adapter, '_client', lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert not await adapter.delete_selfservice_user('missing', authorize=lambda:True)
    assert not await adapter.delete_selfservice_user('missing', authorize=lambda:False, allow_absent=True)
    assert await adapter.delete_selfservice_user('missing', authorize=lambda:True, allow_absent=True)
    assert requests == ['GET']*3
