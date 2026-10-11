"""Self-funded rules, large stakes and exact-round navigation housekeeping."""
import asyncio
import json
import time

import pytest
from test_blackwhite import card as bw_card
from test_blackwhite import command_message
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_niuniu import click, create, message, svc
from test_niuniu import env as nn_env  # noqa: F401

from app.modules.play_money import PlayError


@pytest.fixture
def env(request):
    return request.getfixturevalue('nn_env')


def fund(e, amount):
    for uid in e.uids:
        e.services.points.add(uid, amount, 'isolated.fixture')


@pytest.mark.parametrize('stake', [501, 1000001])
def test_large_niuniu_command_ignores_legacy_cap_and_keeps_collateral(env, stake):
    async def run():
        fund(env, 10*stake)
        before = env.services.points.balance(env.uids[0])
        row = await create(env, f'/牛牛 {stake}')
        assert row['stake'] == stake
        assert json.loads(row['config_json'])['max_stake'] == 0
        assert env.services.points.balance(env.uids[0]) == before-4*stake
        row = await click(env, row, 'join', index=1)
        row = await click(env, row, 'start')
        assert row['state'] == 'settled'
        assert env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='niuniu'")['n'] == 0
    asyncio.run(run())


def test_blackwhite_large_command_and_settlement(env):
    async def run():
        stake = 1000001
        fund(env, 10*stake)
        await env.bot._dispatch_update({'message': command_message(env, text=f'/黑白板 {stake}')})
        service = env.bot._blackwhite_service()
        row = env.db.one("SELECT * FROM play_rounds WHERE kind='blackwhite'")
        assert row['stake'] == stake
        original = bw_card(env, row)
        before = sum(env.services.points.balances().values())
        for i in range(5):
            service.join(row['nonce'], env.actors[i], original, 'black' if i == 0 else 'white')
        assert service.get(row['nonce'])['state'] == 'settled'
        assert sum(env.services.points.balances().values()) == before
    asyncio.run(run())


def test_balance_minimum_and_storage_safety_still_reject(env):
    with pytest.raises(PlayError, match='积分不足'):
        svc(env).create(message(env), 1000001)
    for stake in (0, -1, True, 2**63):
        with pytest.raises(PlayError):svc(env).create(message(env), stake)
        with pytest.raises(PlayError):env.bot._blackwhite_service().create(command_message(env), stake)
    assert not env.db.query('SELECT * FROM niuniu_rounds')


@pytest.mark.parametrize('topic', [0, 25])
def test_active_hint_links_exact_card_then_removed_once_without_deleting_other_round(env, topic):
    async def run():
        row = await create(env, topic=topic)
        await env.bot._dispatch_update({'message': message(env, mid=1002, index=1, topic=topic)})
        guide = env.db.one('SELECT * FROM niuniu_guides WHERE nonce=?', (row['nonce'],))
        assert guide
        sent = [p for m, p in env.tg.calls if m == 'sendMessage' and '点击前往本局' in p.get('text', '')][-1]
        url = sent['reply_markup']['inline_keyboard'][0][0]['url']
        path = f'{topic}/{row["card_message_id"]}' if topic else str(row['card_message_id'])
        assert url == f'https://t.me/c/{str(row["chat_id"])[4:]}/{path}'
        await env.bot._niuniu_delete_guides(row['nonce'])
        assert env.db.one('SELECT state FROM niuniu_guides')['state'] == 'pending'
        unrelated = await create(env, index=2, mid=1003, topic=99)
        await click(env, row, 'join', index=1)
        await click(env, row, 'start')
        await env.bot._niuniu_tick()
        deletes = [p['message_id'] for m, p in env.tg.calls if m == 'deleteMessage']
        assert deletes.count(guide['message_id']) == 1
        assert unrelated['card_message_id'] not in deletes
        assert env.db.one('SELECT state FROM niuniu_guides')['state'] == 'deleted'
    asyncio.run(run())


def test_expired_guide_delete_failure_recovers_without_repeating_money(env):
    async def run():
        row = await create(env)
        await env.bot._dispatch_update({'message': message(env, mid=1002, index=1)})
        guide = env.db.one('SELECT * FROM niuniu_guides')
        original_call = env.bot._call
        async def fail(method, payload, **kwargs):
            if method == 'deleteMessage' and payload['message_id'] == guide['message_id']:
                return None
            return await original_call(method, payload, **kwargs)
        env.bot._call = fail
        svc(env).expire(row['nonce'], now=time.time()+1000)
        await env.bot._niuniu_delete_guides(row['nonce'])
        assert env.db.one('SELECT state FROM niuniu_guides')['state'] == 'pending'
        ledger = env.db.query('SELECT * FROM points_ledger')
        env.bot._call = original_call
        env.db.execute('UPDATE niuniu_guides SET due=0')
        await env.bot._niuniu_tick()
        assert env.db.one('SELECT state FROM niuniu_guides')['state'] == 'deleted'
        assert env.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())
