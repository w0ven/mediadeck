"""/pk dice duel through the real handlers and SQLite ledger; local transport only."""
import asyncio
import copy

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.modules import pk as pk_module
from app.modules.bot_pk import pk_args
from app.modules.game_ui import drain_ui
from app.modules.play_money import PlayError


@pytest.fixture
def env(request, monkeypatch):
    e = request.getfixturevalue('bw_env')
    e.services.registry.save('pk', enabled=True)
    e.rolls = []
    monkeypatch.setattr(pk_module, 'roll', lambda: e.rolls.pop(0))
    return e


def cash(e):
    return (sum(e.services.points.balances().values())
            + (e.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0)
            + (e.db.one('SELECT SUM(amount) n FROM play_funds')['n'] or 0))


def balance(e, index):
    return e.services.points.balance(e.uids[index])


def message(e, text, index=1, mid=2001, reply=None, entities=None, topic=0):
    m = {'chat': {'id': GROUP, 'type': 'supergroup'}, 'from': e.actors[index], 'text': text, 'message_id': mid,
         **({'is_topic_message': True, 'message_thread_id': topic} if topic else {})}
    if reply is not None:
        m['reply_to_message'] = {'message_id': mid - 1, 'from': e.actors[reply], 'text': 'hi'}
    if entities:
        m['entities'] = entities
    return m


async def challenge(e, text='/pk 100', **kw):
    await e.bot._dispatch_update({'message': message(e, text, **kw)})
    await drain_ui(e.bot)
    return e.db.one('SELECT * FROM pk_rounds ORDER BY created_at DESC LIMIT 1')


def card(e, row):
    m = copy.deepcopy(e.tg.message(GROUP, row['card_message_id']))
    m.update(chat={'id': GROUP, 'type': 'supergroup'}, message_id=row['card_message_id'],
             reply_to_message={'message_id': row['command_message_id']}, **{'from': {'id': 123, 'is_bot': True}})
    if row['thread_id']:
        m.update(is_topic_message=True, message_thread_id=row['thread_id'])
    return m


async def click(e, row, op, index):
    await e.bot._dispatch_update({'callback_query': {'id': f'pk-{op}-{index}', 'data': f'pk:{row["nonce"]}:{op}',
                                                     'from': e.actors[index], 'message': card(e, row)}})
    await drain_ui(e.bot)
    return e.bot._pk_service().get(row['nonce'])


def answer(e):
    return [p for m, p in e.tg.calls if m == 'answerCallbackQuery'][-1]


def shown(e, row):
    return e.tg.message(GROUP, row['card_message_id'])


def replies(e):
    return [p['text'] for m, p in e.tg.calls if m == 'sendMessage' and p.get('text', '').startswith('🍃')]


def test_reply_challenge_with_stake_pays_the_higher_roll(env):
    async def run():
        total = cash(env)
        row = await challenge(env, '/pk 100', index=1, reply=2)
        assert row['state'] == 'pending' and row['stake'] == 100
        assert row['target_user_id'] == env.uids[2] and balance(env, 1) == 900
        first = shown(env, row)
        assert f'tg://user?id={env.actors[2]["id"]}' in first['text'] and '押 <b>100</b> 积分' in first['text']
        buttons = [b['callback_data'] for line in first['reply_markup']['inline_keyboard'] for b in line]
        assert buttons == [f'pk:{row["nonce"]}:accept', f'pk:{row["nonce"]}:decline']
        env.rolls = [[6, 5, 4], [1, 2, 3]]
        row = await click(env, row, 'accept', 2)
        assert row['state'] == 'settled' and answer(env)['text'] == '🎲 已开骰'
        assert balance(env, 1) == 1100 and balance(env, 2) == 900 and cash(env) == total
        assert env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='pk'")['n'] == 0
        final = shown(env, row)
        assert '⚅ ⚄ ⚃  = <b>15</b>' in final['text'] and '⚀ ⚁ ⚂  = <b>6</b>' in final['text']
        assert '获胜，赢得 <b>100</b> 积分' in final['text'] and not final['reply_markup']['inline_keyboard']
    asyncio.run(run())


def test_tie_returns_both_stakes(env):
    async def run():
        total = cash(env)
        row = await challenge(env, '/pk 50', index=1, reply=2)
        env.rolls = [[3, 3, 3], [1, 4, 4]]
        row = await click(env, row, 'accept', 2)
        assert row['state'] == 'settled' and balance(env, 1) == balance(env, 2) == 1000 and cash(env) == total
        assert '平局，押注各自退回' in shown(env, row)['text']
    asyncio.run(run())


def test_without_a_number_it_is_a_friendly_match(env):
    async def run():
        row = await challenge(env, '/pk', index=1, reply=3)
        assert row['stake'] == 0 and '友谊赛' in shown(env, row)['text']
        env.rolls = [[1, 1, 1], [6, 6, 6]]
        row = await click(env, row, 'accept', 3)
        assert balance(env, 1) == balance(env, 3) == 1000
        assert env.db.one("SELECT COUNT(*) n FROM play_escrows WHERE scope='pk'")['n'] == 0
        assert '获胜' in shown(env, row)['text'] and '积分' not in shown(env, row)['text'].split('获胜')[1]
    asyncio.run(run())


def test_username_and_text_mentions_pick_the_opponent(env):
    async def run():
        env.db.execute('UPDATE members SET tg_username=? WHERE emby_user_id=?', ('Target_Four', env.uids[4]))
        text = '/pk @target_four 20'
        row = await challenge(env, text, index=1, entities=[{'type': 'mention', 'offset': 4, 'length': 12}])
        assert row['target_user_id'] == env.uids[4] and row['target_username'] == 'target_four'
        await click(env, row, 'decline', 4)
        text = '/pk 小5 30'
        row = await challenge(env, text, index=1, mid=2003,
                              entities=[{'type': 'text_mention', 'offset': 4, 'length': 2, 'user': env.actors[5]}])
        assert row['target_user_id'] == env.uids[5] and row['stake'] == 30
    asyncio.run(run())


def test_decline_withdraw_and_strangers(env):
    async def run():
        total = cash(env)
        row = await challenge(env, '/pk 100', index=1, reply=2)
        await click(env, row, 'accept', 3)
        assert answer(env)['show_alert'] and '只有被挑战的人可以应战' in answer(env)['text']
        await click(env, row, 'decline', 3)
        assert '这不是你的 PK' in answer(env)['text']
        await click(env, row, 'accept', 1)
        assert '等对方应战' in answer(env)['text']
        row = await click(env, row, 'decline', 2)
        assert row['state'] == 'declined' and balance(env, 1) == 1000 and cash(env) == total
        assert '拒绝了' in shown(env, row)['text'] and '押注已退回' in shown(env, row)['text']
        row = await challenge(env, '/pk 100', index=1, reply=2, mid=2005)
        row = await click(env, row, 'decline', 1)
        assert row['state'] == 'withdrawn' and balance(env, 1) == 1000 and '撤回了' in shown(env, row)['text']
        again = await click(env, row, 'accept', 2)
        assert again['state'] == 'withdrawn' and balance(env, 2) == 1000
    asyncio.run(run())


def test_unanswered_challenge_expires_with_refund(env):
    async def run():
        row = await challenge(env, '/pk 100', index=1, reply=2)
        env.bot._pk_service().expire_due(now=row['expires_at'] + 1)
        await env.bot._pk_tick()
        row = env.bot._pk_service().get(row['nonce'])
        assert row['state'] == 'expired' and balance(env, 1) == 1000
        assert '没有应战' in shown(env, row)['text'] and not shown(env, row)['reply_markup']['inline_keyboard']
    asyncio.run(run())


def test_balance_checks_on_both_sides(env):
    async def run():
        await challenge(env, '/pk 5000', index=1, reply=2)
        assert '积分不足' in replies(env)[-1] and not env.db.query('SELECT * FROM pk_rounds')
        assert balance(env, 1) == 1000
        env.services.points.add(env.uids[1], 4000, 'isolated.fixture')
        row = await challenge(env, '/pk 3000', index=1, reply=2, mid=2007)
        row = await click(env, row, 'accept', 2)
        assert row['state'] == 'pending' and '应战需要 3000 积分' in answer(env)['text']
        assert balance(env, 2) == 1000 and balance(env, 1) == 2000
    asyncio.run(run())


def test_rejected_opponents_and_one_pending_duel_each(env):
    async def run():
        await challenge(env, '/pk 10', index=1, reply=1)
        assert '不能和自己 PK' in replies(env)[-1]
        bot_reply = message(env, '/pk 10', index=1, mid=2010)
        bot_reply['reply_to_message'] = {'message_id': 1, 'from': {'id': 77, 'is_bot': True, 'first_name': 'Bot'}}
        await env.bot._dispatch_update({'message': bot_reply})
        assert '不能和机器人 PK' in replies(env)[-1]
        stranger = message(env, '/pk 10', index=1, mid=2011)
        stranger['reply_to_message'] = {'message_id': 1, 'from': {'id': 4242, 'is_bot': False, 'first_name': 'x'}}
        await env.bot._dispatch_update({'message': stranger})
        assert '不是有效的绑定会员' in replies(env)[-1]
        await challenge(env, '/pk 10', index=1, reply=2, mid=2012)
        await challenge(env, '/pk 10', index=1, reply=3, mid=2013)
        assert '你还有一场 PK 在等对方回应' in replies(env)[-1]
        await challenge(env, '/pk 10', index=4, reply=2, mid=2014)
        assert '对方正在另一场 PK 里' in replies(env)[-1]
        assert len(env.db.query('SELECT * FROM pk_rounds')) == 1
    asyncio.run(run())


def test_forum_topic_root_is_not_a_reply_target(env):
    m = message(env, '/pk 10', index=1, topic=55)
    m['reply_to_message'] = {'message_id': 55, 'from': env.actors[3], 'forum_topic_created': {'name': 't'}}
    with pytest.raises(PlayError, match='用法'):
        pk_args(m)
    m['reply_to_message'] = {'message_id': 70, 'from': env.actors[3]}
    assert pk_args(m)[0]['tg_id'] == str(env.actors[3]['id'])


@pytest.mark.parametrize('text', ['/pk 10 20', '/pk abc', '/pk -5'])
def test_malformed_arguments_show_usage(env, text):
    with pytest.raises(PlayError, match='用法'):
        pk_args(message(env, text, reply=2))


def test_disabled_plugin_refuses_and_refunds_pending(env):
    async def run():
        row = await challenge(env, '/pk 100', index=1, reply=2)
        env.services.registry.save('pk', enabled=False)
        await env.bot._pk_tick()
        assert env.bot._pk_service().get(row['nonce'])['state'] == 'cancelled' and balance(env, 1) == 1000
        await challenge(env, '/pk 100', index=1, reply=2, mid=2020)
        assert '暂未开放' in replies(env)[-1]
    asyncio.run(run())
