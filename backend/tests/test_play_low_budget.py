"""Actual update dispatch, synthetic players, real transactions; no Telegram probes."""
import asyncio
import copy
import json

import pytest
from test_blackwhite import card as bw_card
from test_blackwhite import choose, command_message
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_poker import click, create, current, service, total
from test_poker import env as poker_env  # noqa: F401
from test_tg_interaction_context import GROUP


@pytest.fixture
def env(request):
    e = request.getfixturevalue('poker_env')
    e.services.registry.save('poker', config={'default_budget': 30})
    e.services.registry.save('niuniu',enabled=True)
    e.services.registry.get('niuniu').ctx.telegram=e.bot
    return e


def available(e, index, amount):
    uid = e.uids[index]
    e.services.points.add(uid, amount-e.services.points.balance(uid), 'isolated.balance')


def test_two_30_point_players_default_create_join_atomic_start_cap_refund(env):
    async def run():
        for i in (0, 1): available(env, i, 30)
        before = total(env)
        row = await create(env, text='/炸金花', thread=0)
        assert row['stake'] == 10 and json.loads(row['config_json'])['budget'] == 30
        assert env.services.points.balance(env.uids[0]) == 30
        assert '开局冻结 30/人' in env.tg.text(GROUP, row['card_message_id'])
        row = await click(env, row, 'join', index=1)
        assert all(env.services.points.balance(env.uids[i]) == 30 for i in (0, 1))
        row = await click(env, row, 'start', index=0)
        assert row['state'] == 'running'
        assert all(env.services.points.balance(env.uids[i]) == 0 for i in (0, 1))
        assert env.db.one('SELECT SUM(reserved) n FROM play_escrows')['n'] == 60
        assert env.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 40
        assert total(env) == before
        row = await click(env, row, 'follow')
        row = await click(env, row, 'follow')
        # Seen player's follow would exceed remaining10; showdown, no new debit.
        row = await click(env, row, 'look')
        actor = current(env, row)
        row = await click(env, row, 'follow')
        assert row['state'] == 'settled' and json.loads(row['result_json'])['mode'] == 'cap'
        assert env.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0
        assert sum(p['invested'] for p in service(env).players(row['nonce'])) == 40
        assert all(p['invested'] <= 30 for p in service(env).players(row['nonce']))
        assert total(env) == before
        ledger = copy.deepcopy(env.services.points.ledger(actor['user_id']))
        await click(env, row, 'follow', index=next(i for i,a in enumerate(env.actors) if str(a['id'])==actor['tg_id']))
        assert env.services.points.ledger(actor['user_id']) == ledger
    asyncio.run(run())


def test_29_creation_and_join_fail_without_funds_or_registration(env):
    async def run():
        available(env, 0, 29)
        ledger = copy.deepcopy(env.services.points.ledger(env.uids[0]))
        assert await create(env, text='/炸金花', thread=0) is None
        assert env.services.points.ledger(env.uids[0]) == ledger
        assert any('30' in p.get('text','') and '未报名' in p.get('text','') for m,p in env.tg.calls if m=='sendMessage')
        available(env, 0, 30)
        row = await create(env, text='/炸金花', mid=711, thread=0)
        available(env, 1, 29)
        row = await click(env, row, 'join', index=1)
        assert len(service(env).players(row['nonce'])) == 1
        assert env.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 0
        assert env.services.points.balance(env.uids[1]) == 29
        available(env, 1, 30)
        row = await click(env, row, 'join', index=1)
        available(env, 1, 29)
        row = await click(env, row, 'start', index=0)
        assert row['state'] == 'lobby' and row['start_error']
        assert env.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 0
        assert all(p['cards_json'] == '[]' for p in service(env).players(row['nonce']))
    asyncio.run(run())


@pytest.mark.parametrize('command,budget', [('/炸金花 10 500',500),('/炸金花 50 50',50),('/炸金花 10 10',10)])
def test_explicit_common_budget_snapshot_survives_new_defaults(env, command, budget):
    async def run():
        row = await create(env, text=command)
        assert json.loads(row['config_json'])['budget'] == budget
        env.services.registry.save('poker', config={'default_budget': 40})
        row = await click(env, row, 'join', index=1)
        row = await click(env, row, 'start', index=0)
        assert json.loads(row['config_json'])['budget'] == budget
        assert env.db.one('SELECT SUM(reserved) n FROM play_escrows')['n'] == budget*2
    asyncio.run(run())


@pytest.mark.parametrize('command', ['/炸金花 10 501','/炸金花 50 30','/炸金花 9 30','/炸金花 10 29.5','/炸金花 10 0','/炸金花 10 30 extra'])
def test_invalid_budget_has_no_financial_or_round_side_effect(env, command):
    before = total(env)
    assert asyncio.run(create(env, text=command)) is None
    assert env.db.one('SELECT COUNT(*) n FROM play_rounds')['n'] == 0
    assert env.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == 0
    assert total(env) == before


def test_bw_create_checks_ten_but_does_not_debit_join_rechecks_and_requires_five(env):
    async def run():
        available(env, 0, 9)
        await env.bot._dispatch_update({'message':command_message(env,text='/黑白板',thread=0)})
        assert env.db.one('SELECT COUNT(*) n FROM play_rounds')['n'] == 0
        assert any('10' in p.get('text','') and '未创建' in p.get('text','') for m,p in env.tg.calls if m=='sendMessage')
        available(env, 0, 10)
        await env.bot._dispatch_update({'message':command_message(env,text='/黑白板',mid=602,thread=0)})
        row = env.db.one("SELECT * FROM play_rounds WHERE kind='blackwhite'")
        assert row['stake'] == 10 and env.services.points.balance(env.uids[0]) == 10
        available(env, 0, 9)
        await choose(env,row,0,'white',message=bw_card(env,row))
        assert not env.bot._blackwhite_service().players(row['nonce'])
        available(env, 0, 10)
        for i in range(4): row = await choose(env,row,i,'white')
        assert row['state'] == 'lobby' and len(env.bot._blackwhite_service().players(row['nonce'])) == 4
        row = await choose(env,row,4,'black')
        assert row['state'] == 'settled' and len(env.bot._blackwhite_service().players(row['nonce'])) == 5
    asyncio.run(run())


def test_actual_menu_shows_fixed_4n_collateral_and_rejects_30_point_banker(env):
    async def run():
        available(env, 0, 30)
        source=command_message(env,text='/游戏',thread=0)
        await env.bot._dispatch_update({'message':source})
        panel=env.db.one('SELECT * FROM play_panels ORDER BY created_at DESC LIMIT 1')
        body=env.tg.message(GROUP,panel['message_id'])
        assert '参与需 10' in body['text'] and '庄家担保 40 积分' in body['text'] and '闲家每位 10 积分' in body['text']
        body.update(chat={'id':GROUP,'type':'supergroup'},message_id=panel['message_id'])
        body['from']={'id':123,'is_bot':True}
        await env.bot._dispatch_update({'callback_query':{'id':'menu-launch','data':f'pp:{panel["nonce"]}:launch:niuniu', 'from':env.actors[0], 'message':body}})
        row=env.db.one('SELECT * FROM niuniu_rounds')
        assert row is None and env.services.points.balance(env.uids[0])==30
        assert not env.db.query("SELECT * FROM play_escrows WHERE scope='niuniu'")
    asyncio.run(run())
