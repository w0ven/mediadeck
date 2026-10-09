"""Telegram non-topic replies can carry incidental thread ids; no real identities/network."""
import asyncio
import copy

import pytest
from test_blackwhite import card, command_message, create
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401


@pytest.fixture
def env(request):
    return request.getfixturevalue('bw_env')


@pytest.mark.parametrize('topic_flag', [None, False])
def test_non_topic_reply_callback_joins_original_blackwhite_card(env, topic_flag):
    e = env
    async def run():
        row = await create(e, stake=10, thread=0)
        assert row['thread_id'] == 0
        callback_message = card(e, row)
        callback_message['message_thread_id'] = 777  # reply chain, NOT a forum topic
        if topic_flag is not None:
            callback_message['is_topic_message'] = topic_flag
        actor, uid = e.actors[1], e.uids[1]
        before = e.services.points.balance(uid)
        update = {'callback_query': {'id':'isolated-reply-join', 'data':f'bw:{row["nonce"]}:white',
                  'from':actor, 'message':callback_message}}
        await e.bot._dispatch_update(copy.deepcopy(update))
        assert len(e.bot._blackwhite_service().players(row['nonce'])) == 1
        assert e.services.points.balance(uid) == before - 10
        await e.bot._dispatch_update(copy.deepcopy(update))
        assert len(e.bot._blackwhite_service().players(row['nonce'])) == 1
        assert e.services.points.balance(uid) == before - 10
    asyncio.run(run())


def test_true_topic_still_rejects_a_different_topic_without_debit(env):
    e = env
    async def run():
        source = command_message(e, text='/黑白板 10', thread=25)
        source['is_topic_message'] = True
        await e.bot._dispatch_update({'message': source})
        row = e.db.one("SELECT * FROM play_rounds WHERE kind='blackwhite'")
        assert row['thread_id'] == 25
        original = card(e, row)
        original.update(message_thread_id=26, is_topic_message=True)
        before = e.services.points.balance(e.uids[1])
        await e.bot._dispatch_update({'callback_query': {'id':'wrong-topic', 'data':f'bw:{row["nonce"]}:black',
                                  'from':e.actors[1], 'message':original}})
        assert e.services.points.balance(e.uids[1]) == before
        assert e.db.one('SELECT COUNT(*) n FROM play_players')['n'] == 0
    asyncio.run(run())


def test_30_available_points_cannot_register_explicit_500_fixed_niuniu_stake(env):
    e = env
    e.services.registry.save('niuniu', enabled=True)
    uid = e.uids[0]
    e.services.points.add(uid, 30-e.services.points.balance(uid), 'isolated.budget-threshold')
    ledger = copy.deepcopy(e.services.points.ledger(uid))
    source = command_message(e, text='/牛牛 500', thread=0)
    asyncio.run(e.bot._dispatch_update({'message': source}))
    assert e.db.one("SELECT COUNT(*) n FROM play_rounds WHERE kind='poker'")['n'] == 0
    assert e.db.one('SELECT COUNT(*) n FROM poker_hands')['n'] == 0
    assert e.db.one('SELECT COUNT(*) n FROM niuniu_rounds')['n'] == 0
    assert e.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 0
    assert e.services.points.balance(uid) == 30 and e.services.points.ledger(uid) == ledger
    assert any(method=='sendMessage' and payload.get('text')=='🍃 本局需 500 积分，积分不足，未加入'
               for method, payload in e.tg.calls)
