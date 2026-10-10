"""Real received-update handlers and durable state, with isolated Telegram adapter."""
import asyncio
import copy
import json
import os
from html import escape
from pathlib import Path

import pytest
from test_group_points import msg
from test_packets_bot import (
    alert,
    callback,
    card_message,
    economy_env,  # noqa: F401
    interaction_env,  # noqa: F401
)
from test_packets_bot import env as bot_env  # noqa: F401
from test_red_packets import confirm, context, make_service
from test_red_packets import env as packet_env  # noqa: F401
from test_tg_interaction_context import ADMIN, GROUP, VIEWER

from app.core.db import Database
from app.modules.bot_packets import PacketBotMixin
from app.modules.game_ui import drain_ui
from app.modules.red_packets import public_name

BANNED = ('每人一次', '发起人不可领', '结果仅本人可见', '北京时间', '领取范围',
          '全体有效成员', '奖励红包', '扣除', '已退回', '不扣', 'mint', '余额')


@pytest.fixture
def env(request):
    return request.getfixturevalue('bot_env')


async def issue(e, *, total=100, parts=2, mode='random', sender=VIEWER, audience=''):
    text = f'/红包 {"等额 " if mode == "equal" else ""}{total} {parts}' + audience
    message = msg(text, actor=sender, mid=700, thread=7)
    message.pop('reply_to_message')
    message['is_topic_message']=True
    message['from']['first_name'] = 'admin' if sender == ADMIN else '发起<&😀>'
    await e.bot._dispatch_update({'message': message})
    row = e.db.one('SELECT * FROM red_packets WHERE command_message_id=700')
    # Preserve the original expiring-card regression as a pre-upgrade snapshot.
    e.db.execute('UPDATE red_packets SET permanent=0 WHERE nonce=?',(row['nonce'],))
    row=e.db.one('SELECT * FROM red_packets WHERE nonce=?',(row['nonce'],))
    await callback(e, row)
    return row


async def press(e, row, data, actor, name='', *, message=None, bot=False):
    await e.bot._dispatch_update({'callback_query': {
        'id': f'public-{actor}', 'data': data,
        'from': {'id': actor, 'is_bot': bot, 'first_name': name},
        'message': message or card_message(e, row)}})
    await drain_ui(e.bot)


def add_members(e, count):
    actors = []
    for index in range(count):
        uid, tg = f'public-{index}', 100000 + index
        e.members.upsert(uid, f'PRIVATE_ACCOUNT_{index}', {'group_id': 'standard'})
        e.members.bind_telegram(uid, str(tg))
        actors.append(tg)
    return actors


def text(e, row):
    return e.tg.text(GROUP, row['card_message_id'])


def assert_public(body):
    assert all(word not in body for word in BANNED)
    assert 'PRIVATE_ACCOUNT' not in body and 'ViewerB' not in body
    assert len(body.encode('utf-16-le')) // 2 < 4096


@pytest.mark.parametrize('sender', [VIEWER, ADMIN])
@pytest.mark.parametrize('mode', ['random', 'equal'])
def test_actual_claim_public_name_amount_original_edit_snapshot_no_private_fields(env, sender, mode):
    async def run():
        row = await issue(env, sender=sender, mode=mode)
        initial = text(env, row)
        assert ('等额红包' if mode == 'equal' else '拼手气红包') in initial
        assert '0/2' in initial and '截止 ' not in initial
        assert_public(initial)
        assert '发起&lt;&amp;😀&gt;' in initial if sender == VIEWER else 'admin' in initial
        name = '<小明&>"😀'
        await press(env, row, 'rpclaim:' + row['nonce'], 904, name)
        claim = env.db.one('SELECT * FROM red_packet_claims')
        body = text(env, row)
        assert '1/2' in body and escape(name) in body
        assert f"<b>{claim['amount']}</b> 积分" in body
        assert '904' not in body and '手气最佳' not in body
        assert claim['display_name'] == name
        assert alert(env)['show_alert'] and str(claim['amount']) in alert(env)['text']
        await press(env, row, 'rpclaim:' + row['nonce'], 904, '改名不改历史快照')
        assert text(env, row) == body
        assert env.services.points.balance('u2') == claim['amount']
        assert len(env.db.query('SELECT * FROM red_packet_claims')) == 1
        assert_public(body)
        sends = [p for method, p in env.tg.calls if method == 'sendMessage']
        assert len(sends) == 1 and sends[0]['chat_id'] == GROUP
        edits = [p for method, p in env.tg.calls if method == 'editMessageText']
        assert all(p['message_id'] == row['card_message_id'] and p['chat_id'] == GROUP for p in edits)
        if sender == ADMIN:
            assert env.services.points.balance('admin') == 0
            assert env.services.points.ledger('u2')[0]['reason'] == 'packet.reward'
        else:
            assert env.services.points.balance('u1') == 4900
    asyncio.run(run())


@pytest.mark.parametrize('actor', [{'id':123456, 'username':'SECRET'}, {'first_name':'123456'},
                                  {'first_name':'', 'last_name':''}, {'first_name':123}])
def test_missing_or_numeric_public_name_uses_neutral_member(actor):
    assert public_name(actor) == '成员'


def test_display_name_single_line_control_safe_last_name():
    assert public_name({'first_name': ' 小\n明\u202e ', 'last_name': ' 王 '}) == '小明 王'


def test_fifty_long_names_all_results_accessible_terminal_best_and_paging_retry(env, monkeypatch):
    async def run():
        actors = add_members(env, 50)
        row = await issue(env, total=500, parts=50, mode='equal')
        names = [f'P{i:02d} ' + '<&😀"' * 40 for i in range(50)]
        await asyncio.gather(*[press(env, row, 'rpclaim:' + row['nonce'], tg, name)
                               for tg, name in zip(actors, names)])
        final = env.db.one('SELECT * FROM red_packets')
        assert final['status'] == 'exhausted' and final['result_page'] == 4
        assert final['rendered_version'] == final['render_version']
        assert len(env.db.query('SELECT * FROM red_packet_claims')) == 50
        assert env.db.one('SELECT SUM(amount) AS total FROM red_packet_claims')['total'] == 500
        assert '已领完' in text(env, row) and '50/50' in text(env, row)
        assert '手气最佳' in text(env, row) and 'P00' in text(env, row)
        original_lock = env.bot._packet_render_locks[row['nonce']]
        seen = set()
        for page in range(4, -1, -1):
            body = text(env, row)
            assert_public(body)
            for index in range(page * 10, (page + 1) * 10):
                assert f'P{index:02d} ' in body
                assert ' · <b>10</b> 积分' in body
                seen.add(index)
            buttons = env.tg.actions(GROUP, row['card_message_id'])
            assert all(len(b['callback_data'].encode()) <= 64 for b in buttons)
            assert not any(b['callback_data'].startswith('rpclaim:') for b in buttons)
            if page:
                await press(env, row, f'rppage:{row["nonce"]}:{page-1}', 904, '访客')
                assert env.db.one('SELECT result_page FROM red_packets')['result_page'] == page-1
                assert env.bot._packet_render_locks[row['nonce']] is original_lock
        assert seen == set(range(50))
        # Failed page edit survives a process-lock reset and normal retry tick.
        env.tg.edit_error = 'isolated edit failure'
        await press(env, row, f'rppage:{row["nonce"]}:1', 904)
        failed = env.db.one('SELECT * FROM red_packets')
        assert failed['publish_error'] and failed['result_page'] == 1
        env.tg.edit_error = ''
        env.bot._packet_render_locks = {}
        monkeypatch.setattr('time.time', lambda: failed['next_publish_at'])
        await env.bot._packet_tick()
        assert 'P10' in text(env, row) and 'P19' in text(env, row)
        assert env.services.points.balance('u1') == 4500
        assert len([1 for method, _ in env.tg.calls if method == 'sendMessage']) == 1
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['chat','topic','message','forward','sender_chat','actor_bot','forged'])
def test_page_button_requires_original_group_topic_message_identity(env, mutation):
    async def run():
        row = await issue(env, total=110, parts=11, mode='equal')
        for tg in add_members(env, 11):
            await press(env, row, 'rpclaim:' + row['nonce'], tg, '公开名')
        message = copy.deepcopy(card_message(env, row))
        if mutation == 'chat':message['chat']['id'] -= 1
        elif mutation == 'topic':message['message_thread_id'] += 1
        elif mutation == 'message':message['message_id'] += 1
        elif mutation == 'forward':message['forward_origin'] = {'type':'user'}
        elif mutation == 'sender_chat':message['sender_chat'] = {'id':GROUP}
        elif mutation == 'forged':message['reply_markup']['inline_keyboard'] = []
        before = env.db.one('SELECT * FROM red_packets')
        before_body = text(env, row)
        await press(env, row, f'rppage:{row["nonce"]}:0', 904, message=message, bot=mutation=='actor_bot')
        assert alert(env)['show_alert']
        assert env.db.one('SELECT * FROM red_packets') == before
        assert text(env, row) == before_body
        assert env.services.points.balance('u1') == 4890
    asyncio.run(run())


def test_delayed_old_progress_cannot_overwrite_new_terminal_public_results(env):
    async def run():
        row = await issue(env, total=20, parts=2, mode='equal')
        actor = add_members(env, 1)[0]
        entered, release = asyncio.Event(), asyncio.Event()
        original = env.bot._call
        async def delay(method, payload=None, **kwargs):
            if method == 'editMessageText' and '已领取 <b>1/2</b>' in payload.get('text',''):
                entered.set()
                await asyncio.wait_for(release.wait(), 3)
            return await original(method, payload, **kwargs)
        env.bot._call = delay
        first = asyncio.create_task(press(env, row, 'rpclaim:' + row['nonce'], 904, '小明'))
        await asyncio.wait_for(entered.wait(), 3)
        # Normal per-chat dispatch serializes callbacks. Exercise the actual
        # handler component concurrently with its render, as with worker edits.
        second = asyncio.create_task(env.bot._handle_callback({
            'id':'parallel', 'data':'rpclaim:' + row['nonce'],
            'from':{'id':actor, 'is_bot':False, 'first_name':'小王'},
            'message':card_message(env,row)}))
        for _ in range(100):
            if env.db.one('SELECT claimed_count FROM red_packets')['claimed_count']==2:break
            await asyncio.sleep(0.01)
        assert env.db.one('SELECT claimed_count FROM red_packets')['claimed_count']==2
        release.set();await asyncio.gather(first, second)
        body = text(env, row)
        assert '已领完' in body and '2/2' in body and '1/2' not in body
        assert '小明' in body and '小王' in body and '手气最佳' in body
        final = env.db.one('SELECT * FROM red_packets')
        assert final['rendered_version'] == final['render_version']
        assert_public(body)
    asyncio.run(run())


def test_white_expiry_public_result_and_refund_unchanged(env, monkeypatch):
    async def run():
        row = await issue(env, mode='equal', audience=' 白名单')
        assert '· 白名单' in text(env, row)
        await press(env, row, 'rpclaim:' + row['nonce'], 904, '小明')
        assert '白名单' in alert(env)['text'] and not env.db.query('SELECT * FROM red_packet_claims')
        env.members.upsert('u2', 'PRIVATE_ACCOUNT', {'group_id':'whitelist'})
        await press(env, row, 'rpclaim:' + row['nonce'], 904, '小明')
        active = env.db.one('SELECT * FROM red_packets')
        monkeypatch.setattr('time.time', lambda: active['expires_at']+1)
        await env.bot._packet_tick()
        body = text(env, row)
        assert '已过期' in body and '1/2' in body and '小明' in body
        assert '截止' not in body and '手气最佳' not in body
        assert not env.tg.actions(GROUP, row['card_message_id'])
        assert env.services.points.balance('u1') == 4950 and env.services.points.balance('u2') == 50
        assert env.services.points.ledger('u1')[0]['reason']=='packet.refund'
        assert_public(body)
    asyncio.run(run())


def test_legacy_migration_preserves_economic_rows_and_neutral_public_results(request):
    e=request.getfixturevalue('packet_env');row=confirm(e, total=100, parts=2, mode='equal')
    e.service.claim(row['nonce'], {'id':1002,'is_bot':False,'first_name':'小明'}, context(row,claim=True))
    e.db.execute('ALTER TABLE red_packets DROP COLUMN public_actor_name')
    e.db.execute('ALTER TABLE red_packets DROP COLUMN result_page')
    e.db.execute('ALTER TABLE red_packet_claims DROP COLUMN display_name')
    tables=('red_packets','red_packet_claims','points_ledger','members','audit_log')
    before={t:e.db.query('SELECT * FROM '+t) for t in tables}
    migrated=Database(e.db.path)
    try:
        for t,rows in before.items():
            after=migrated.query('SELECT * FROM '+t)
            assert [{k:r[k] for k in rows[0]} for r in after] == rows
        service=make_service(migrated,e)
        old=service.get(row['nonce'])
        body,_=PacketBotMixin()._packet_view(old,service)
        assert '成员' in body and '隔离用户' not in body and '1001' not in body and '1002' not in body
        assert ' · <b>50</b> 积分' in body
        # New received claim after migration adds a public snapshot, not backfill.
        service.claim(row['nonce'],{'id':1003,'is_bot':False,'first_name':'小王'},context(old,claim=True))
        final=service.get(row['nonce']);body,_=PacketBotMixin()._packet_view(final,service)
        assert '已领完' in body and '成员' in body and '小王' in body and '手气最佳' in body
        assert service.claims(row['nonce'],1)==[{'amount':50,'slot':0,'display_name':'','tg_user_id':'1002','tg_username':''}]
        assert not migrated.query('PRAGMA foreign_key_check')
        again=Database(e.db.path);again.close()
        assert service.get(row['nonce'])==final
    finally:migrated.close()


def test_simplified_effect_samples_from_actual_handler(env, monkeypatch):
    async def run():
        values=[18,12,10,8,7,6,5,11,13,10]
        monkeypatch.setattr('app.modules.red_packets.allocations',lambda total,parts,mode:values)
        row=await issue(env,parts=10,sender=ADMIN)
        names=['小明','小王','小李','小陈','小林','小赵','小周','小吴','小杨','小刘']
        actors=add_members(env,10);samples={}
        for index,(actor,name) in enumerate(zip(actors,names)):
            await press(env,row,'rpclaim:'+row['nonce'],actor,name)
            if index==6:samples['active']=text(env,row)
        samples['exhausted']=text(env,row)
        assert '已领取 <b>7/10</b>' in samples['active'] and '小明 · <b>18</b> 积分' in samples['active']
        assert '手气最佳 <b>小明</b> · 18 积分' in samples['exhausted']
        for body in samples.values():assert_public(body)
        if os.getenv('PACKET_UI_ARTIFACTS'):
            target=Path(os.environ['PACKET_UI_ARTIFACTS']);target.mkdir(parents=True,exist_ok=True)
            (target/'effect-samples.json').write_text(json.dumps(samples,ensure_ascii=False,indent=2)+'\n')
    asyncio.run(run())
