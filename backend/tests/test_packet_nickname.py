"""Packet-only public nickname labels; freeze/cursor/money stay unchanged."""
import asyncio
import copy
import json
import time

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_packet_permanent import env as packet_fixture  # noqa: F401
from test_packet_permanent import issue, press
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.game_mentions import text_units
from app.modules.packet_delivery import (
    PacketDelivery,
    complete_receipt,
    receipt_mention,
    receipt_view,
)


@pytest.fixture
def env(request):return request.getfixturevalue('packet_fixture')


@pytest.mark.parametrize('name,label', [('<昵称&>', '&lt;昵称&amp;&gt;'), ('', '成员'), (None, '成员'), ('1234', '成员'), (' 小\n明\u202e ', '小明')])
def test_packet_label_uses_public_nickname_not_handle_or_login(name, label):
    claim = {'tg_user_id': '42', 'display_name': name, 'tg_username': 'not_the_label', 'username': 'PRIVATE_LOGIN'}
    assert receipt_mention(claim) == f'<a href="tg://user?id=42">{label}</a>'
    assert 'tg://' not in receipt_mention(dict(claim, tg_user_id='invalid'))
    assert 'PRIVATE_LOGIN' not in receipt_mention(claim) and '@not_the_label' not in receipt_mention(claim)


def test_packet_complete_and_page_names_best_order_amount_and_long_segments():
    claims = [{'tg_user_id': str(5000+i), 'tg_username': f'handle_{i}', 'display_name': f'公开{i:03d} '+('😀<&>'*40), 'slot': i, 'amount': i+1} for i in range(200)]
    row = {'nonce': 'fixture', 'parts': 200, 'total': 20100, 'mode': 'random'}
    payload = complete_receipt(row, claims)
    assert len(payload['messages']) > 1 and payload['cursor'] == 0 and payload['message_ids'] == []
    texts = [p['text'] for p in payload['messages']]
    body = ''.join(texts)
    positions = []
    for claim in claims:
        label = receipt_mention(claim)
        assert label in body and f'{label} · {claim["amount"]} 积分' in body
        positions.append(body.index(label))
    assert positions == sorted(positions)
    assert '@handle_' not in body and 'PRIVATE_LOGIN' not in body and '余额' not in body
    assert body.endswith('✨ 手气最佳 '+receipt_mention(claims[-1])+' · 200 积分')
    assert all(text_units(text) <= 3800 and text.count('tg://user') <= 90 for text in texts)
    assert '…</a>' in body and '&lt;' in body and '&amp;' in body
    page = receipt_view(row, claims, 19)['text']
    assert '✨ 手气最佳 '+receipt_mention(claims[-1])+' · 200 积分' in page
    assert all(receipt_mention(c) in page for c in claims[-10:]) and '@handle_' not in page


def test_real_handler_random_complete_list_and_best_both_nickname(env, monkeypatch):
    async def run():
        e = env
        monkeypatch.setattr('app.modules.red_packets.allocations', lambda total, parts, mode: [10]*14+[20])
        actors = []
        for i in range(15):
            uid, tg = f'nickname-fixture-{i}', 7000+i
            e.members.upsert(uid, f'PRIVATE_LOGIN_{i}', {'group_id': 'standard'})
            e.members.bind_telegram(uid, str(tg))
            actors.append({'id': tg, 'is_bot': False, 'first_name': f'<昵称{i}&>', 'username': f'handle_{i}'})
        e.actors.extend(actors)
        await e.bot._dispatch_update({'message': {'message_id': 4101, 'chat': {'id': GROUP, 'type': 'supergroup'}, 'from': e.actors[1], 'text': '/红包 160 15'}})
        row = e.db.one('SELECT * FROM red_packets WHERE command_message_id=4101')
        row = await press(e, row)
        for i in range(15):row = await press(e, row, 'rpclaim', 7+i)
        body = e.tg.text(GROUP, row['receipt_message_id'])
        assert row['receipt_state'] == 'sent' and row['unpin_state'] == 'sent'
        assert all(f'<a href="tg://user?id={a["id"]}">&lt;昵称{i}&amp;&gt;</a>' in body for i,a in enumerate(actors))
        assert '手气最佳 <a href="tg://user?id=7014">&lt;昵称14&amp;&gt;</a> · 20 积分' in body
        assert '@handle_' not in body and 'PRIVATE_LOGIN' not in body and '余额' not in body
        claims = e.bot._packet_service().claims(row['nonce'], 15)
        assert [c['amount'] for c in claims] == [10]*14+[20]
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        db = Database(e.db.path);db.close()
        await e.bot._packet_tick()
        assert e.bot._packet_service().get(row['nonce'])['receipt_payload'] == row['receipt_payload']
        assert e.db.query('SELECT * FROM points_ledger') == ledger
        assert sum(m == 'sendMessage' and '红包圆满收官' in p.get('text', '') for m,p in e.tg.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('state', ['sent', 'unknown'])
def test_preexisting_frozen_handle_payload_never_reformatted_or_resent(env, state):
    async def run():
        e = env
        row = await issue(e, parts=1)
        effects = e.bot._packet_effects
        async def quiet(nonce):pass
        e.bot._packet_effects = quiet
        row = await press(e, row, 'rpclaim', 2)
        old = {'messages': [{'text': '<a href="tg://user?id=42">@old_fixture_handle</a>', 'parse_mode': 'HTML'}], 'cursor': 0, 'message_ids': []}
        frozen = json.dumps(old)
        e.db.execute('UPDATE red_packets SET receipt_payload=? WHERE nonce=?', (frozen, row['nonce']))
        delivery = PacketDelivery(e.bot._packet_service())
        job = delivery.claim(row['nonce'], 'receipt')
        assert job['_payload'] == old['messages'][0]  # Existing pending snapshots too.
        delivery.finish(job, {'message_id': 888} if state == 'sent' else None, {'state': 'unknown'})
        saved = e.bot._packet_service().get(row['nonce'])
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        e.db.execute('UPDATE red_packet_claims SET display_name=?,tg_username=? WHERE nonce=?', ('新的公开昵称', 'new_handle', row['nonce']))
        db = Database(e.db.path);db.close()
        e.bot._packet_effects = effects
        await e.bot._packet_tick()
        current = e.bot._packet_service().get(row['nonce'])
        assert (current['receipt_state'], current['receipt_payload'], current['receipt_message_id']) == (saved['receipt_state'], saved['receipt_payload'], saved['receipt_message_id'])
        assert delivery.claim(row['nonce'], 'receipt', now=time.time()+1000) is None
        assert not any(m == 'sendMessage' and ('old_fixture_handle' in p.get('text', '') or '新的公开昵称' in p.get('text', '')) for m,p in e.tg.calls)
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())
