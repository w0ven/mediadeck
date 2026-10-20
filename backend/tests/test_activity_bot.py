"""Real Telegram update/callback dispatch with local ledger and mock TG/Emby."""
import asyncio
import json
import re
import time
from unittest.mock import AsyncMock

import pytest
from test_checkin_interaction import economy_env, interaction_env, message, posted  # noqa: F401
from test_checkin_interaction import env as bot_env  # noqa: F401
from test_economy import proof
from test_tg_interaction_context import GROUP, VIEWER, click, command

from app.modules.levels import DAY, LevelService, defaults
from app.modules.watch_rewards import ActivityService


@pytest.fixture
def env(request):
    e = request.getfixturevalue('bot_env')
    e.levels = LevelService(e.db, e.services.members, e.services.points, ActivityService(e.db, e.services.points))
    e.services.members.bind_levels(e.levels)
    e.members.bind_levels(e.levels)
    config = defaults(); config['enabled'] = True
    e.levels.save_config(config, 0)
    now = int(time.time())
    for uid in ('u1', 'u2'):
        e.db.execute('INSERT INTO activity_members(emby_user_id,enrolled_at) VALUES(?,?)', (uid, now))
        e.db.execute('UPDATE members SET expires_at=? WHERE emby_user_id=?', (now+DAY, uid))
        e.services.points.add(uid, 100, 'admin.adjust')
    e.services.registry.save('checkin', config={'dual_checkin':True})
    for i in range(3): proof(e.db, 'u1', now=now-i*DAY, seconds=1600, key='renew'+str(i))
    return e


def actions(e, mid):
    return e.tg.actions(VIEWER, mid)


def find_action(e, mid, prefix):
    return next(b['callback_data'] for b in actions(e, mid) if b['callback_data'].startswith(prefix))


async def private_command(e, text):
    return await command(e, text, chat=VIEWER, user=VIEWER)


async def tap(e, data, mid):
    return await click(e, data, mid, chat=VIEWER, user=VIEWER)


def test_private_card_complete_compact_and_renew_preview_confirmation_retry(env):
    e = env
    before = e.services.points.balance('u1')
    expiry = e.members.get('u1')['expires_at_effective']
    async def run():
        mid = await private_command(e, '/me')
        body = e.tg.text(VIEWER, mid)
        for text in ('Lv1 布衣', '偷得浮生半日闲', '成长 0', '今日观影奖励 0/48积分',
                     '0/60成长', '80/80分钟', '3/3有效日', '本级续费 80积分',
                     '自动保号：未开启', '到期：', '删除候选：', '北京时间'):
            assert text in body
        assert len(body.splitlines()) <= 12
        assert 'webaccount' not in body
        assert 'activity:status' in {b['callback_data'] for b in actions(e, mid)}
        await tap(e, 'activity:status', mid)
        assert '最终有效权益' in e.tg.text(VIEWER, mid)
        await tap(e, 'activity:renew', mid)
        text = e.tg.text(VIEWER, mid)
        assert '本次价格：80积分' in text and '原到期：' in text and '预计新到期：' in text
        assert e.services.points.balance('u1') == before
        assert e.members.get('u1')['expires_at_effective'] == expiry
        confirm = find_action(e, mid, 'activity:confirm:')
        # Concurrent redelivery is one transaction/charge, independent of callback ID.
        await asyncio.gather(tap(e, confirm, mid), tap(e, confirm, mid))
        assert '已入账' in e.tg.text(VIEWER, mid)
        assert e.services.points.balance('u1') == before-80
        assert e.members.get('u1')['expires_at_effective'] == expiry+30*DAY
        await private_command(e, '/renew30')  # command also previews, cannot charge
        assert e.services.points.balance('u1') == before-80
        await tap(e, confirm, mid)  # persisted result survives message navigation
        assert e.services.points.balance('u1') == before-80
    asyncio.run(run())
    assert not e.db.query('SELECT * FROM activity_watch_days')  # read/preview never settles


@pytest.mark.parametrize('change,reason', [('money','还缺'),('watch','有效日'),('early','72小时'),('deleting','待确认')])
def test_unqualified_preview_explains_without_confirm_or_charge(env, change, reason):
    e = env
    if change == 'money': e.services.points.add('u1', -e.services.points.balance('u1'), 'admin.adjust')
    if change == 'watch': e.db.execute("DELETE FROM watch_verified_samples WHERE emby_user_id='u1'")
    if change == 'early': e.db.execute("UPDATE members SET expires_at=? WHERE emby_user_id='u1'", (int(time.time())+4*DAY,))
    if change == 'deleting': e.db.execute("UPDATE activity_members SET lifecycle='deleting' WHERE emby_user_id='u1'")
    before = e.services.points.balance('u1')
    async def run():
        mid = await private_command(e, '续费')
        assert reason in e.tg.text(VIEWER, mid)
        assert not any(b['callback_data'].startswith('activity:confirm:') for b in actions(e, mid))
    asyncio.run(run())
    assert e.services.points.balance('u1') == before


@pytest.mark.parametrize('state', ['expired', 'suspended'])
def test_expired_and_manually_disabled_can_view_confirm_without_unblocking(env, state):
    e = env
    e.db.execute("UPDATE members SET expires_at=?,status=? WHERE emby_user_id='u1'",
                 (int(time.time())-2*DAY, 'suspended' if state == 'suspended' else 'active'))
    e.bot._on_member_changed = AsyncMock(return_value={'remote_ok':False})
    async def run():
        mid = await private_command(e, '/me')
        assert 'Lv1 布衣' in e.tg.text(VIEWER, mid)
        await tap(e, 'activity:renew', mid)
        confirm = find_action(e, mid, 'activity:confirm:')
        await tap(e, confirm, mid)
        assert '本地已更新，远端未确认' in e.tg.text(VIEWER, mid)
    asyncio.run(run())
    assert e.members.get('u1')['expires_at_effective'] == int(time.time())+30*DAY
    assert e.members.get('u1')['status'] == ('suspended' if state == 'suspended' else 'active')


@pytest.mark.parametrize('change,reason', [('cancel','过期'),('timeout','过期'),('config','变化'),('binding','不属于'),('funds','积分不足')])
def test_confirmation_revalidates_intent_and_binding(env, monkeypatch, change, reason):
    e = env
    before = e.services.points.balance('u1')
    async def run():
        mid = await private_command(e, '续费30天')
        confirm = find_action(e, mid, 'activity:confirm:')
        if change == 'cancel': await tap(e, find_action(e, mid, 'activity:cancel:'), mid)
        if change == 'timeout':
            now = int(time.time()); monkeypatch.setattr('time.time', lambda:now+301)
        if change == 'config':
            cfg = e.levels.config(); cfg['config']['levels'][0]['price']=79
            e.levels.save_config(cfg['config'], cfg['revision'])
        if change == 'binding':
            e.db.execute("UPDATE members SET tg_user_id='905' WHERE emby_user_id='u1'")
            e.db.execute("UPDATE members SET tg_user_id=? WHERE emby_user_id='u2'", (str(VIEWER),))
        if change == 'funds': e.services.points.add('u1', -before, 'admin.adjust')
        await tap(e, confirm, mid)
        assert reason in e.tg.text(VIEWER, mid)
    asyncio.run(run())
    assert not e.db.query("SELECT * FROM points_ledger WHERE reason='activity.renew'")


def test_other_actor_forged_private_or_group_callbacks_never_charge_or_enable(env):
    e = env
    async def run():
        mid = await private_command(e, '/activity')
        await tap(e, 'activity:renew', mid)
        confirm = find_action(e, mid, 'activity:confirm:')
        for chat in (VIEWER, 904, GROUP):
            await click(e, confirm, mid, chat=chat, user=904)
            await click(e, 'activity:auto-confirm:on', mid, chat=chat, user=904)
        await command(e, '续费', chat=GROUP, user=VIEWER)
        await command(e, '开启自动保号', chat=GROUP, user=VIEWER)
        await e.bot._dispatch_update({'message':message('/renew30@OtherDemoBot')})
    asyncio.run(run())
    assert not e.db.query("SELECT * FROM points_ledger WHERE reason='activity.renew'")
    assert not e.db.query('SELECT * FROM activity_members WHERE auto_renew=1')


def test_auto_renew_explains_opt_in_before_confirm_and_can_disable(env):
    e = env
    async def run():
        mid = await private_command(e, '等级权益')
        await tap(e, 'activity:auto:on', mid)
        assert '默认关闭' in e.tg.text(VIEWER, mid) and '自动按当时等级价格扣积分' in e.tg.text(VIEWER, mid)
        assert not e.members.get('u1')['auto_renew']
        await tap(e, find_action(e, mid, 'activity:auto-confirm:'), mid)
        assert e.members.get('u1')['auto_renew']
        await tap(e, 'me', mid)
        assert '自动保号：已开启' in e.tg.text(VIEWER, mid)
        await private_command(e, '关闭自动保号')
        mid = e.bot._panel[str(VIEWER)]
        assert e.members.get('u1')['auto_renew']
        await tap(e, find_action(e, mid, 'activity:auto-confirm:'), mid)
        assert not e.members.get('u1')['auto_renew']
    asyncio.run(run())


def test_whitelist_and_worn_title_in_bot_card_and_no_web_code(env):
    e = env
    e.members.upsert('u1', '', {'group_id':'whitelist'})
    # Existing title projection remains the authoritative display source.
    from app.modules.economy_rules import DEFAULT_CARDS, economy_write
    from app.modules.inventory import InventoryService
    with economy_write(e.db) as conn:
        card = InventoryService.add(conn, 'u1', DEFAULT_CARDS[3], 'test', int(time.time()))
    e.services.bag.use('u1', card, title='清风明月')
    async def run():
        title = e.bot._titles.titles('u1')[0]
        await e.bot._titles.wear('u1', title['id'])
        mid = await private_command(e, '/me')
        body = e.tg.text(VIEWER, mid)
        assert 'Lv1 布衣 · 白名单' in body and '清风明月' in body and '白名单豁免' in body
        await tap(e, 'activity:status', mid)
        assert 'activity:renew' not in {b['callback_data'] for b in actions(e, mid)}
        await private_command(e, '/webaccount')
        assert '登录码' not in str(posted(e))
    asyncio.run(run())


@pytest.mark.parametrize('chat', [VIEWER, GROUP])
def test_normal_receipt_luck_button_only_owner_and_terminal_callbacks(env, monkeypatch, chat):
    e = env
    monkeypatch.setattr('app.modules.dual_checkin.draw', lambda *args:130 if args[3].endswith(':luck') else 999999)
    async def run():
        await e.bot._dispatch_update({'message':message('签到', chat=chat)})
        mid = e.tg.next_id
        text = e.tg.text(chat, mid)
        assert '可能更低' in text
        assert any(b['callback_data']=='luckcheckin' for b in e.tg.actions(chat, mid))
        await click(e, 'luckcheckin', mid, chat=chat, user=904)
        assert json.loads(e.db.one("SELECT result_json FROM checkins WHERE emby_user_id='u1'")['result_json'])['mode']=='normal'
        await click(e, 'luckcheckin', mid, chat=chat, user=VIEWER)
        assert '净调整 -7' in posted(e)[-1]['text']
        assert not e.tg.actions(chat, e.tg.next_id)
        await click(e, 'checkin', mid, chat=chat, user=VIEWER)
        assert '不可重抽或恢复普通' in posted(e)[-1]['text']
        assert json.loads(e.db.one("SELECT result_json FROM checkins WHERE emby_user_id='u1'")['result_json'])['points']==3
        assert 'webaccount' not in re.sub('<[^>]*>', '', str(posted(e)))
    asyncio.run(run())


def test_same_binding_reassigned_invalidates_old_confirmation(env):
    e = env
    async def run():
        mid = await private_command(e, '续费')
        confirm = find_action(e, mid, 'activity:confirm:')
        e.members.bind_telegram('u1', '905')
        e.members.bind_telegram('u1', str(VIEWER))
        await tap(e, confirm, mid)
        assert '绑定已变化' in e.tg.text(VIEWER, mid)
    asyncio.run(run())
    assert not e.db.query("SELECT * FROM points_ledger WHERE reason='activity.renew'")


def test_confirm_result_persists_across_reconstructed_services(env):
    e = env
    async def run():
        mid = await private_command(e, '续费')
        confirm = find_action(e, mid, 'activity:confirm:')
        await tap(e, confirm, mid)
        before = e.services.points.balance('u1')
        from test_economy import build

        from app.core.db import Database
        db = Database(e.db.path)
        try:
            other = build(db)
            levels = LevelService(db, other.members, other.points, ActivityService(db, other.points))
            other.members.bind_levels(levels)
            nonce = confirm.split(':')[-1]
            result = levels.renew('u1', 'bot-renew:'+nonce, bot_intent=(nonce, str(VIEWER), str(VIEWER)))
            assert result['cost'] == 80 and other.points.balance('u1') == before
        finally:
            db.close()
    asyncio.run(run())


def test_luck_terminal_hides_both_checkin_buttons_in_fresh_bag(env, monkeypatch):
    e = env
    monkeypatch.setattr('app.modules.dual_checkin.draw', lambda *args:0)
    async def run():
        mid = await private_command(e, '/me')
        await tap(e, 'bag', mid)
        assert {'checkin','luckcheckin'} <= {b['callback_data'] for b in actions(e, mid)}
        await tap(e, 'checkin', mid)
        await tap(e, 'bag', mid)
        assert 'checkin' not in {b['callback_data'] for b in actions(e, mid)}
        assert 'luckcheckin' in {b['callback_data'] for b in actions(e, mid)}
        await tap(e, 'luckcheckin', mid)
        await tap(e, 'bag', mid)
        assert not {'checkin','luckcheckin'} & {b['callback_data'] for b in actions(e, mid)}
    asyncio.run(run())


def test_lost_confirmation_result_has_safe_same_intent_retry(env, monkeypatch):
    e = env
    original = e.levels.renew
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('simulated lost response')
    async def run():
        mid = await private_command(e, '续费')
        confirm = find_action(e, mid, 'activity:confirm:')
        monkeypatch.setattr(e.levels, 'renew', lost)
        await tap(e, confirm, mid)
        assert '暂未确认' in e.tg.text(VIEWER, mid)
        assert find_action(e, mid, 'activity:confirm:') == confirm
        balance = e.services.points.balance('u1')
        monkeypatch.setattr(e.levels, 'renew', original)
        await tap(e, confirm, mid)
        assert '已入账' in e.tg.text(VIEWER, mid)
        assert e.services.points.balance('u1') == balance
    asyncio.run(run())
