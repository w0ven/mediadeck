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


def test_private_card_zero_viewing_and_renew_preview_confirmation_retry(env):
    e = env
    e.db.execute("DELETE FROM watch_verified_samples WHERE emby_user_id='u1'")
    before = e.services.points.balance('u1')
    expiry = e.members.get('u1')['expires_at_effective']
    async def run():
        mid = await private_command(e, '/me')
        body = e.tg.text(VIEWER, mid)
        for text in ('🌱 Lv1 布衣「偷得浮生半日闲」', '成长 0',
                     '续期 80 积分 / 30 天',
                     '自动保号：未开启', '到期 · 剩'):
            assert text in body
        assert len(body.splitlines()) <= 12
        assert not any(text in body for text in ('删除候选', '保留至', '今日观影奖励', '观影已到账', '80分钟', '有效日', '近30天核验'))
        assert 'webaccount' not in body
        assert {'activity:status', 'activity:auto:on', 'activity:renew'} <= {b['callback_data'] for b in actions(e, mid)}
        await tap(e, 'activity:status', mid)
        assert '当前权益' in e.tg.text(VIEWER, mid)
        await tap(e, 'me', mid)
        await tap(e, 'activity:renew', mid)
        text = e.tg.text(VIEWER, mid)
        assert '费用：80 积分' in text and '当前到期：' in text and '续期后：' in text
        assert '5 分钟内确认' in text and '达标' not in text
        assert e.services.points.balance('u1') == before
        assert e.members.get('u1')['expires_at_effective'] == expiry
        confirm = find_action(e, mid, 'activity:confirm:')
        # Concurrent redelivery is one transaction/charge, independent of callback ID.
        await asyncio.gather(tap(e, confirm, mid), tap(e, confirm, mid))
        assert '续期成功' in e.tg.text(VIEWER, mid)
        assert e.services.points.balance('u1') == before-80
        assert e.members.get('u1')['expires_at_effective'] == expiry+30*DAY
        await private_command(e, '/renew30')  # command also previews, cannot charge
        assert e.services.points.balance('u1') == before-80
        await tap(e, confirm, mid)  # persisted result survives message navigation
        assert e.services.points.balance('u1') == before-80
    asyncio.run(run())
    assert not e.db.query('SELECT * FROM activity_watch_days')  # read/preview never settles


def test_activity_member_pages_escape_dynamic_text_and_fit_telegram(env, monkeypatch):
    from test_bot_reply_templates import plain

    e = env
    member = e.members.get('u1')
    member.update(username='<b>Demo&😀</b>', note='<i>Note&</i>')
    info = e.levels.status('u1')
    info['worn_title'] = '<u>Title&</u>'
    monkeypatch.setattr(e.levels, 'status', lambda uid: info)
    account = e.bot._account_card(member)
    e.bot._edit = AsyncMock()
    asyncio.run(e.bot._activity_action(VIEWER, 1, member, 'status',
                                      actor=str(VIEWER), callback=True))
    details, keyboard = e.bot._edit.await_args.args[2:]
    assert '&lt;b&gt;Demo&amp;😀&lt;/b&gt;' in account
    for body in (account, details):
        assert '&lt;u&gt;Title&amp;&lt;/u&gt;' in body
        assert len(plain(body).encode('utf-16-le')) // 2 <= 4096
    assert '&lt;i&gt;Note&amp;&lt;/i&gt;' in details
    for text in ('当前权益', '观看时长'):
        assert text in details
    for text in ('个人覆盖', '有效道具优先', '满10分钟', '自动结算', '免签到',
                 '到期停播', '72小时', '管理员开关和复核', '非发奖依据'):
        assert text not in details
    assert all(len(button['callback_data'].encode()) <= 64 for row in keyboard for button in row)


@pytest.mark.parametrize('change,reason', [('money','还差'),('early','72 小时'),('deleting','待确认')])
def test_unqualified_preview_explains_without_confirm_or_charge(env, change, reason):
    e = env
    if change == 'money': e.services.points.add('u1', -e.services.points.balance('u1'), 'admin.adjust')
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
    e.db.execute("DELETE FROM watch_verified_samples WHERE emby_user_id='u1'")
    e.db.execute("UPDATE members SET expires_at=?,status=? WHERE emby_user_id='u1'",
                 (int(time.time())-2*DAY, 'suspended' if state == 'suspended' else 'active'))
    e.bot._on_member_changed = AsyncMock(return_value={'remote_ok':False})
    async def run():
        mid = await private_command(e, '/me')
        assert '🌱 Lv1 布衣「偷得浮生半日闲」' in e.tg.text(VIEWER, mid)
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
    e.db.execute("DELETE FROM watch_verified_samples WHERE emby_user_id='u1'")
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


@pytest.mark.parametrize('entry', ['button', '/start account', '/myinfo', '等级权益', '开启自动保号'])
def test_auto_renew_explains_opt_in_before_confirm_and_can_disable(env, entry):
    e = env
    e.db.execute("DELETE FROM watch_verified_samples WHERE emby_user_id='u1'")
    balance = e.services.points.balance('u1')
    async def run():
        if entry == 'button':
            mid = await private_command(e, '/me')
        else:
            mid = await private_command(e, entry)
        if entry != '开启自动保号':
            assert {'activity:auto:on', 'activity:renew'} <= {b['callback_data'] for b in actions(e, mid)}
            assert '🤖 开启自动保号' in {b['text'] for b in actions(e, mid)}
            await tap(e, 'activity:auto:on', mid)
        body = e.tg.text(VIEWER, mid)
        assert not any(text in body for text in ('80分钟', '有效日', '每天核验', '达标'))
        for text in ('默认关闭', '确认后才会开启', '到期前 24 小时',
                     '积分足够', '按当时的等级价格自动扣积分续期 30 天',
                     '积分不足时不扣分', '到期后无法播放', '72 小时'):
            assert text in body
        confirm = find_action(e, mid, 'activity:auto-confirm:')
        assert '确认开启' in next(b['text'] for b in actions(e, mid)
                                             if b['callback_data'] == confirm)
        assert not e.members.get('u1')['auto_renew']
        assert e.services.points.balance('u1') == balance
        await tap(e, confirm, mid)
        assert e.members.get('u1')['auto_renew']
        assert e.services.points.balance('u1') == balance
        await tap(e, 'me', mid)
        assert '自动保号：已开启' in e.tg.text(VIEWER, mid)
        assert 'activity:auto:off' in {b['callback_data'] for b in actions(e, mid)}
        assert 'activity:auto:on' not in {b['callback_data'] for b in actions(e, mid)}
        await private_command(e, '关闭自动保号')
        mid = e.bot._panel[str(VIEWER)]
        assert e.members.get('u1')['auto_renew']
        await tap(e, find_action(e, mid, 'activity:auto-confirm:'), mid)
        assert not e.members.get('u1')['auto_renew']
    asyncio.run(run())


@pytest.mark.parametrize('view', ['private', 'group', 'admin'])
def test_non_exempt_account_views_no_longer_show_viewing_renewal_thresholds(env, view):
    member = env.members.get('u1')
    body = {'private': env.bot._account_card, 'group': env.bot._brief_card,
            'admin': lambda m: env.bot._member_admin_card(m, public=False)}[view](member)
    assert '80 积分 / 30 天' in body
    assert not any(word in body for word in ('80分钟', '有效日', '合格日', '近30天核验'))
    if view != 'private':
        assert '<b>🎬 观看</b>' in body


@pytest.mark.parametrize('public', [False, True])
def test_whitelist_card_keeps_rewards_and_usage_without_renewal_clutter(env, public):
    from test_bot_reply_templates import plain
    e = env
    e.members.upsert('u1', '', {'group_id': 'whitelist'})
    member = e.members.get('u1')
    body = e.bot._brief_card(member) if public else e.bot._account_card(member)
    assert '白名单' in body and '♾ 永久' in body
    for text in ('积分', '成长', '偷得浮生半日闲'):
        assert text in body
    for text in ('续费预算', '本级续费', '缺口', '近30天核验', '合格日', '有效日', '自动保号', '删除候选'):
        assert text not in body
    if public:
        for text in ('🚀 带宽', '📺 同播', '📥 ', '🎞️ ', '<b>🎬 观看</b>', '<b>🪙 积分', '📈'):
            assert text in body
    assert len(plain(body).encode('utf-16-le')) // 2 <= 4096


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
        assert '🌱 Lv1 布衣「清风明月」 · 白名单' in body and '♾ 永久有效' in body
        assert not {'activity:renew', 'activity:auto:on', 'activity:auto:off'} & {b['callback_data'] for b in actions(e, mid)}
        await tap(e, 'activity:status', mid)
        assert 'activity:renew' not in {b['callback_data'] for b in actions(e, mid)}
        await private_command(e, '/webaccount')
        assert '登录码' not in str(posted(e))
    asyncio.run(run())


@pytest.mark.parametrize('chat', [VIEWER, GROUP])
def test_no_new_receipt_buttons_but_legacy_callback_owner_guard_is_preserved(env, monkeypatch, chat):
    e = env
    monkeypatch.setattr('app.modules.dual_checkin.draw', lambda *args:130 if args[3].endswith(':luck') else 999999)
    async def run():
        await e.bot._dispatch_update({'message':message('签到', chat=chat)})
        mid = e.tg.next_id
        text = e.tg.text(chat, mid)
        assert '可能更低' in text
        assert not e.tg.actions(chat, mid)
        # Simulate a pre-change receipt; old callbacks must retain the owner guard.
        with e.bot._bind_session(chat, VIEWER, group=(chat == GROUP)):
            e.bot._remember_card_actor(chat, mid)
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


@pytest.mark.parametrize('change', ['other_actor', 'group', 'binding', 'timeout', 'cancel'])
def test_account_auto_button_confirmation_remains_actor_and_binding_bound(env, monkeypatch, change):
    e = env
    async def run():
        mid = await private_command(e, '/me')
        assert 'activity:auto:on' in {b['callback_data'] for b in actions(e, mid)}
        await tap(e, 'activity:auto:on', mid)
        confirm = find_action(e, mid, 'activity:auto-confirm:')
        if change == 'other_actor':
            await click(e, confirm, mid, chat=VIEWER, user=904)
        elif change == 'group':
            await click(e, confirm, mid, chat=GROUP, user=VIEWER)
        else:
            if change == 'binding':
                e.members.bind_telegram('u1', '905')
                e.members.bind_telegram('u1', str(VIEWER))
            elif change == 'timeout':
                now = time.time()
                monkeypatch.setattr(time, 'time', lambda: now+301)
            else:
                await tap(e, 'activity:status', mid)
            await tap(e, confirm, mid)
        assert not e.members.get('u1')['auto_renew']
    asyncio.run(run())
    assert not e.db.query("SELECT * FROM points_ledger WHERE reason='activity.renew'")


@pytest.mark.parametrize('enabled,dual', [(False, True), (True, False), (True, True)])
def test_home_and_bag_never_offer_checkin_buttons_in_any_plugin_mode(env, enabled, dual):
    e = env
    e.services.registry.save('checkin', enabled=enabled, config={'dual_checkin': dual})
    async def run():
        mid = await private_command(e, '/start')
        assert not {b['callback_data'] for b in actions(e, mid)} & {'checkin', 'luckcheckin'}
        await tap(e, 'bag', mid)
        assert not {b['callback_data'] for b in actions(e, mid)} & {'checkin', 'luckcheckin'}
    asyncio.run(run())


def test_level_name_and_title_are_one_identity_on_account_home_and_activity(env):
    from test_bot_reply_templates import plain
    e = env
    cfg = e.levels.config()
    cfg['config']['levels'][1].update(name='听风', icon='🍃', title='风止星归')
    e.levels.save_config(cfg['config'], cfg['revision'])
    e.db.execute("INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) VALUES('u1',120,'test','level2',?)", (int(time.time()),))
    label = '🍃 Lv2 听风「风止星归」'
    member = e.members.get('u1')
    for card in (e.bot._account_card(member), e.bot._brief_card(member), e.bot._home(str(VIEWER), 'Demo')[0]):
        assert label in plain(card)
        assert plain(card).count('风止星归') == 1
    async def run():
        mid = await private_command(e, '/activity')
        assert label in plain(e.tg.text(VIEWER, mid))
    asyncio.run(run())
    member['activity_level'].update(name='<听风&>', title='<风止&星归>', icon='🍃')
    assert '🍃 Lv2 &lt;听风&amp;&gt;「&lt;风止&amp;星归&gt;」' in e.bot._group_label(member)


def test_unmanaged_account_does_not_offer_automatic_charging(env):
    e = env
    cfg = e.levels.config()
    cfg['config']['enabled'] = False
    e.levels.save_config(cfg['config'], cfg['revision'])
    async def run():
        mid = await private_command(e, '/start account')
        assert not {'activity:renew', 'activity:auto:on', 'activity:auto:off'} & {b['callback_data'] for b in actions(e, mid)}
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


@pytest.mark.parametrize('chat', [VIEWER, GROUP])
@pytest.mark.parametrize('normal,luck', [('/checkin', '/luckcheckin'), ('签到', '运气签到'),
    ('/签到@MediaDeckDemoBot', '/运气签到@MediaDeckDemoBot')])
def test_checkin_commands_preserve_normal_luck_and_terminal_behavior_without_buttons(env, monkeypatch, chat, normal, luck):
    e = env
    monkeypatch.setattr('app.modules.dual_checkin.draw', lambda *args:0)
    async def run():
        for text in (normal, luck, normal):
            await e.bot._dispatch_update({'message':message(text, chat=chat)})
            assert not e.tg.actions(chat, e.tg.next_id)
        assert '不可重抽或恢复普通' in posted(e)[-1]['text']
        saved = json.loads(e.db.one("SELECT result_json FROM checkins WHERE emby_user_id='u1'")['result_json'])
        assert saved['mode'] == 'luck' and saved['points'] == 1
        mid = await private_command(e, '/start')
        assert not {'checkin', 'luckcheckin'} & {b['callback_data'] for b in actions(e, mid)}
    asyncio.run(run())


def test_zero_viewing_bot_auto_confirmation_then_scheduler_renews_once(env):
    e = env
    e.db.execute("DELETE FROM watch_verified_samples WHERE emby_user_id='u1'")
    ctx = e.services.checkin.ctx
    ctx.levels, ctx.telegram = e.levels, None
    e.services.registry.save('activity_lifecycle', enabled=True)
    plugin = e.services.registry.get('activity_lifecycle')
    balance = e.services.points.balance('u1')
    expiry = e.members.get('u1')['expires_at_effective']
    async def run():
        mid = await private_command(e, '开启自动保号')
        assert not e.members.get('u1')['auto_renew']
        assert e.services.points.balance('u1') == balance
        await tap(e, find_action(e, mid, 'activity:auto-confirm:'), mid)
        assert e.members.get('u1')['auto_renew']
        assert '积分足够时会自动续期' in e.tg.text(VIEWER, mid)
        assert e.services.points.balance('u1') == balance
        assert (await plugin.run({}))['已续期'] == 1
        assert e.services.points.balance('u1') == balance-80
        assert e.members.get('u1')['expires_at_effective'] == expiry+30*DAY
        assert (await plugin.run({}))['已续期'] == 0
        assert e.services.points.balance('u1') == balance-80
    asyncio.run(run())
    assert not e.db.query('SELECT * FROM activity_watch_days')


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
        body = e.tg.text(VIEWER, mid)
        assert '暂未确认' in body and '不要重新发起续期' in body
        assert not any(text in body for text in ('未扣费', '操作成功', 'simulated lost response'))
        assert find_action(e, mid, 'activity:confirm:') == confirm
        balance = e.services.points.balance('u1')
        monkeypatch.setattr(e.levels, 'renew', original)
        await tap(e, confirm, mid)
        assert '续期成功' in e.tg.text(VIEWER, mid)
        assert e.services.points.balance('u1') == balance
    asyncio.run(run())
