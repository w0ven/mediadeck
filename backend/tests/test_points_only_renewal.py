"""Renewal consumes points only; rewards and all non-viewing protections remain."""
import copy
from pathlib import Path

import pytest
from test_activity_levels import NOW, env  # noqa: F401
from test_economy import proof

from app.modules.levels import DAY


def no_viewing(e):
    for table in ('watch_verified_samples', 'activity_watch_intervals', 'activity_watch_days'):
        assert not e.db.query(f'SELECT * FROM {table}')


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('level,growth,cost', [
    (1, 0, 80), (2, 120, 75), (3, 360, 70), (4, 900, 65),
    (5, 1800, 60), (6, 3600, 55), (7, 7200, 50), (8, 0, 1),
])
def test_zero_viewing_uses_unchanged_level_price_and_thirty_days(env, automatic, level, growth, cost):  # noqa: F811 - pytest fixture injection
    if growth:
        env.db.execute("INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) VALUES('u',?,'test','level',?)", (growth, NOW))
    if level == 8:
        env.levels.grant_special('u', 'test')
    env.levels.set_auto('u', automatic)
    env.points.add('u', cost, 'admin.adjust')
    no_viewing(env)
    # Legacy metric remains false; it is neither a transaction nor preview gate.
    assert not env.levels.status('u')['qualification']['eligible']
    result = env.levels.renew('u', 'points-only', automatic=automatic)
    assert result['cost'] == cost and result['level'] == level
    assert result['expires_at'] == NOW+31*DAY and result['days'] == 30
    assert env.points.balance('u') == 0
    assert env.members.get('u')['activity_level']['growth'] == growth
    assert env.levels.renew('u', 'points-only', automatic=automatic) == result
    assert len(env.db.query("SELECT * FROM points_ledger WHERE reason='activity.renew'")) == 1
    no_viewing(env)


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('offset', [0, 1, -DAY-1])
def test_manual_72h_and_auto_24h_windows_still_apply_without_viewing(env, automatic, offset):  # noqa: F811 - pytest fixture injection
    window = DAY if automatic else 3*DAY
    expiry = NOW+window+offset if offset >= 0 else NOW+offset
    env.members.upsert('u', '', {'expires_at':expiry})
    env.levels.set_auto('u', True)
    env.points.add('u', 80, 'admin.adjust')
    if offset == 1:
        with pytest.raises(ValueError, match='72小时.*24小时'):
            env.levels.renew('u', 'outside', automatic=automatic)
        assert env.points.balance('u') == 80
        assert env.members.get('u')['expires_at_effective'] == expiry
    else:
        result = env.levels.renew('u', 'inside', automatic=automatic)
        assert result['expires_at'] == max(NOW, expiry)+30*DAY
        assert env.points.balance('u') == 0
    no_viewing(env)


@pytest.mark.parametrize('automatic', [False, True])
def test_zero_viewing_with_insufficient_balance_is_atomic(env, automatic):  # noqa: F811 - pytest fixture injection
    env.points.add('u', 79, 'admin.adjust')
    env.levels.set_auto('u', True)
    before = env.members.get('u')['expires_at_effective']
    with pytest.raises(ValueError, match='积分不足'):
        env.levels.renew('u', 'insufficient', automatic=automatic)
    assert env.points.balance('u') == 79
    assert env.members.get('u')['expires_at_effective'] == before
    assert not env.db.query("SELECT * FROM points_ledger WHERE reason='activity.renew'")
    no_viewing(env)


@pytest.mark.asyncio
@pytest.mark.parametrize('condition,renewed,reason', [
    ('ready', 1, None), ('poor', 0, '积分不足'), ('early', 0, None),
    ('opt_out', 0, None), ('suspended', 0, '人工禁用'),
    ('pending', 0, '未开通'), ('whitelist', 0, None),
    ('deleting', 0, None), ('disabled', 0, None),
])
async def test_actual_scheduler_has_no_viewing_gate_but_keeps_other_protections(env, condition, renewed, reason):  # noqa: F811 - pytest fixture injection
    ctx = env.checkin.ctx
    ctx.levels = env.levels
    ctx.telegram = None
    env.registry.save('activity_lifecycle', enabled=True)
    env.levels.set_auto('u', condition != 'opt_out')
    balance = 79 if condition == 'poor' else 100
    env.points.add('u', balance, 'admin.adjust')
    if condition == 'early':
        env.members.upsert('u', '', {'expires_at':NOW+DAY+1})
    if condition in ('suspended', 'pending'):
        env.members.upsert('u', '', {'status':condition})
    if condition == 'whitelist':
        env.members.upsert('u', '', {'group_id':'whitelist'})
    if condition == 'deleting':
        env.db.execute("UPDATE activity_members SET lifecycle='deleting' WHERE emby_user_id='u'")
    if condition == 'disabled':
        config = copy.deepcopy(env.config)
        config['enabled'] = False
        env.levels.save_config(config, 1)
    before = env.members.get('u')
    no_viewing(env)
    plugin = env.registry.get('activity_lifecycle')
    result = await plugin.run({})
    assert result.get('已续期', 0) == renewed
    if reason:
        assert any(reason in item['reason'] for item in result['待处理'])
    after = env.members.get('u')
    assert after['status'] == before['status']
    assert env.points.balance('u') == balance-80*renewed
    expected_expiry = before['expires_at_effective']+30*DAY if renewed else before['expires_at_effective']
    assert after['expires_at_effective'] == expected_expiry
    assert not (await plugin.run({})).get('已续期', 0)
    assert len(env.db.query("SELECT * FROM points_ledger WHERE reason='activity.renew'")) == renewed
    no_viewing(env)


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('condition,reason', [('whitelist', '白名单'), ('deleting', '待确认'), ('disabled', '未启用')])
def test_manual_and_auto_transaction_guards_without_viewing(env, automatic, condition, reason):  # noqa: F811 - pytest fixture injection
    env.points.add('u', 100, 'admin.adjust')
    env.levels.set_auto('u', True)
    if condition == 'whitelist':
        env.members.upsert('u', '', {'group_id':'whitelist'})
    elif condition == 'deleting':
        env.db.execute("UPDATE activity_members SET lifecycle='deleting' WHERE emby_user_id='u'")
    else:
        config = copy.deepcopy(env.config)
        config['enabled'] = False
        env.levels.save_config(config, 1)
    expiry = env.members.get('u')['expires_at_effective']
    with pytest.raises(ValueError, match=reason):
        env.levels.renew('u', 'guarded', automatic=automatic)
    assert env.points.balance('u') == 100
    assert env.members.get('u')['expires_at_effective'] == expiry
    assert not env.db.query("SELECT * FROM points_ledger WHERE reason='activity.renew'")
    no_viewing(env)


def test_viewing_still_earns_original_rewards_but_does_not_unlock_renewal(env):  # noqa: F811 - pytest fixture injection
    env.activity.activate(NOW-3600)
    proof(env.db, 'u', now=NOW, seconds=1200, key='reward-independent')
    result = env.activity.settle(NOW)
    assert result['points'] == 32 and result['growth'] == 20
    assert env.points.balance('u') == 32
    assert not env.levels.status('u')['qualification']['eligible']  # one day, twenty minutes
    env.points.add('u', 48, 'admin.adjust')
    assert env.levels.renew('u', 'earned-points')['cost'] == 80
    assert env.points.balance('u') == 0
    assert env.members.get('u')['activity_level']['growth'] == 20
    assert env.activity.settle(NOW)['points'] == 0
    assert env.points.balance('u') == 0


def test_admin_member_details_do_not_present_watch_stats_as_renewal_qualification():
    script = (Path(__file__).parents[1]/'app/static/members.js').read_text()
    assert '纯积分续期 · 观影获取积分与成长值' in script
    assert '30天续费资格' not in script and '/80分钟' not in script and '/3合格日' not in script
