"""Real handlers/SQLite, fake Telegram: identity, terminal UI and durable new sends."""
import asyncio
import copy
import json
import time

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_niuniu import card, click, create, svc
from test_niuniu import env as nn_fixture  # noqa: F401
from test_packet_permanent import env as packet_fixture  # noqa: F401
from test_packet_permanent import issue, press
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.game_mentions import result_mention, text_units
from app.modules.niuniu_delivery import NiuniuDelivery
from app.modules.packet_delivery import PacketDelivery, complete_receipt
from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def nn_env(request):return request.getfixturevalue('nn_fixture')


@pytest.fixture
def packet_env(request):return request.getfixturevalue('packet_fixture')


def test_mention_uses_stable_identity_and_only_telegram_username():
    assert result_mention('42', '<昵称&>', 'real_user') == '<a href="tg://user?id=42">@real_user</a>'
    assert result_mention('42', '<昵称&>', '<fake>') == '<a href="tg://user?id=42">&lt;昵称&amp;&gt;</a>'
    assert 'tg://' not in result_mention('not-an-id', '昵称')


def test_niuniu_all_players_new_result_original_closed_and_no_replay(nn_env):
    async def run():
        e = nn_env
        e.actors[0]['username'] = 'real_banker'
        e.actors[0]['first_name'] = '测试庄家'
        row = await create(e)
        for i in range(1, 5):row = await click(e, row, 'join', i)
        assert row['state'] == 'settled' and row['result_state'] == 'sent'
        assert row['result_message_id'] != row['card_message_id']
        body = e.tg.text(GROUP, row['result_message_id'])
        assert '@real_banker' not in body and '&lt;小0&amp;&gt;' in body and '测试庄家' in body
        for p in svc(e).players(row['nonce']):assert f'tg://user?id={p["tg_id"]}' in body
        assert text_units(body) <= 1024
        for secret in ('余额', 'private-login', 'escrow', *e.uids):assert secret not in body
        original = e.tg.message(GROUP, row['card_message_id'])
        assert original['reply_markup']['inline_keyboard'] == []
        assert row['card_delete_state'] == 'deleted'
        assert [(p['chat_id'], p['message_id']) for m,p in e.tg.calls if m == 'deleteMessage'] == [(GROUP, row['card_message_id'])]
        assert '@real_banker' not in e.tg.text(GROUP, row['card_message_id'])
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        old = card(e, row)
        old['reply_markup'] = {'inline_keyboard': [[{'callback_data': f'nn:{row["nonce"]}:start'}]]}
        await click(e, row, 'start', original=old)
        await e.bot._niuniu_tick()
        assert e.db.query('SELECT * FROM points_ledger') == ledger
        assert sum(m == 'sendPhoto' and '本局战报' in p.get('caption', '') for m, p in e.photos) == 1
    asyncio.run(run())


@pytest.mark.parametrize('state', ['retry', 'failed', 'unknown'])
def test_niuniu_delivery_failure_reopen_no_financial_replay(nn_env, state):
    async def run():
        e = nn_env
        row = await create(e)
        row = await click(e, row, 'join', 1)
        transport = e.bot._call_multipart
        attempts = []
        async def fail(method, payload, files, **kw):
            if method == 'sendPhoto' and '本局战报' in payload.get('caption', ''):
                attempts.append(copy.deepcopy(payload))
                CALL_DELIVERY.set({'state': state})
                return None
            return await transport(method, payload, files, **kw)
        e.bot._call_multipart = fail
        row = await click(e, row, 'start')
        assert row['result_state'] == state
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        db = Database(e.db.path)
        db.close()
        e.db.execute('UPDATE niuniu_rounds SET result_due=0 WHERE nonce=?', (row['nonce'],))
        await e.bot._niuniu_tick()
        assert len(attempts) == (2 if state == 'retry' else 1)
        assert all(p == attempts[0] for p in attempts)
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())


def test_niuniu_sending_restart_unknown_and_historical_no_backfill(nn_env):
    async def run():
        e = nn_env
        row = await create(e)
        row = await click(e, row, 'join', 1)
        deliver = e.bot._niuniu_result
        async def quiet(nonce):pass
        e.bot._niuniu_result = quiet
        row = await click(e, row, 'start')
        assert row['result_state'] == 'pending' and row['result_payload']
        d = NiuniuDelivery(svc(e))
        job = d.claim(row['nonce'])
        db = Database(e.db.path)
        db.close()
        assert d.claim(row['nonce'], now=job['_lease']+1) is None
        d.finish(job, {'message_id': 777}, {})
        assert svc(e).get(row['nonce'])['result_state'] == 'unknown'
        e.bot._niuniu_result = deliver
        await e.bot._niuniu_tick()
        assert sum(m == 'sendPhoto' and '本局战报' in p.get('caption', '') for m, p in e.photos) == 0
        # An already finished pre-upgrade record has no new-result intent.
        e.db.execute("UPDATE niuniu_rounds SET result_state='',result_payload='',result_message_id=NULL WHERE nonce=?", (row['nonce'],))
        db = Database(e.db.path)
        db.close()
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        await e.bot._niuniu_tick()
        assert svc(e).get(row['nonce'])['result_state'] == ''
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())


def test_packet_first_result_mentions_complete_more_than_ten(packet_env):
    async def run():
        e = packet_env
        actors = []
        for i in range(15):
            uid, tg = f'result-fixture-{i}', 3000+i
            e.members.upsert(uid, f'private-login-{i}', {'group_id': 'standard'})
            e.members.bind_telegram(uid, str(tg))
            actors.append({'id': tg, 'is_bot': False, 'first_name': f'<玩家{i}&>', **({'username': f'real_user_{i}'} if i % 2 == 0 else {})})
        e.actors.extend(actors)
        row = await issue(e, parts=15)
        for i in range(15):row = await press(e, row, 'rpclaim', 7+i)
        assert row['receipt_state'] == 'sent'
        body = e.tg.text(GROUP, row['receipt_message_id'])
        assert all(f'tg://user?id={a["id"]}' in body for a in actors)
        assert '@real_user_14' in body and '&lt;玩家13&amp;&gt;' in body
        assert '余额' not in body and 'private-login' not in body
        assert e.tg.actions(GROUP, row['receipt_message_id']) == []
        payload = json.loads(row['receipt_payload'])
        assert payload['cursor'] == 1 and len(payload['message_ids']) == 1
    asyncio.run(run())


def test_packet_oversized_complete_list_chunk_cursor_preserves_previous_ack(packet_env):
    claims = [{'tg_user_id': str(4000+i), 'tg_username': 'u'*31+str(i % 10), 'display_name': '😀'*40, 'slot': i, 'amount': 5000} for i in range(200)]
    payload = complete_receipt({'parts': 200, 'total': 1000000, 'mode': 'equal'}, claims)
    assert len(payload['messages']) >= 2
    all_text = ''.join(p['text'] for p in payload['messages'])
    assert all(f'tg://user?id={c["tg_user_id"]}' in all_text for c in claims)
    assert all(text_units(p['text']) <= 4096 and p['text'].count('tg://user') <= 90 for p in payload['messages'])
    async def run():
        e = packet_env
        row = await issue(e, parts=1)
        effects = e.bot._packet_effects
        async def quiet(nonce):pass
        e.bot._packet_effects = quiet
        row = await press(e, row, 'rpclaim', 2)
        e.db.execute('UPDATE red_packets SET receipt_payload=? WHERE nonce=?', (json.dumps(payload), row['nonce']))
        d = PacketDelivery(e.bot._packet_service())
        first = d.claim(row['nonce'], 'receipt')
        d.finish(first, {'message_id': 888}, {})
        second = d.claim(row['nonce'], 'receipt')
        d.finish(second, None, {'state': 'unknown'})
        e.bot._packet_effects = effects
        await e.bot._packet_tick()
        current = e.bot._packet_service().get(row['nonce'])
        saved = json.loads(current['receipt_payload'])
        assert current['receipt_state'] == 'unknown' and current['receipt_message_id'] == 888
        assert saved['cursor'] == 1 and saved['message_ids'] == [888]
        assert d.claim(row['nonce'], 'receipt', now=time.time()+100) is None
    asyncio.run(run())
