"""Real handler, money and SQLite; exact new-result ACK/deletion boundaries."""
import asyncio
import copy
import json
from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_niuniu import card, click, create, svc
from test_niuniu import env as nn_fixture  # noqa: F401
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.game_mentions import text_units
from app.modules.game_ui import drain_ui
from app.modules.niuniu_delivery import NiuniuDelivery, result_amount, result_caption, result_name
from app.modules.niuniu_image import render_room
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.telegram import _CALL_ERROR


@pytest.fixture
def env(request):
    e = request.getfixturevalue('nn_fixture')
    e.delete_mode = 'ok'
    transport = e.bot._call
    async def call(method, payload, **kw):
        if method != 'deleteMessage':return await transport(method, payload, **kw)
        e.tg.calls.append((method, copy.deepcopy(payload)))
        if e.delete_mode == 'ok':
            e.tg.messages.pop((str(payload['chat_id']), payload['message_id']), None)
            return True
        if e.delete_mode == 'gone':
            _CALL_ERROR.set('Bad Request: message to delete not found')
            CALL_DELIVERY.set({'state': 'failed'})
        else:CALL_DELIVERY.set({'state': e.delete_mode})
        return None
    e.bot._call = call
    return e


def deletions(e):return [p for m,p in e.tg.calls if m == 'deleteMessage']


def reports(e):return [p for m,p in e.photos if m == 'sendPhoto' and '本局战报' in p.get('caption', '')]


def test_send_confirmed_before_exact_old_card_delete_once(env):
    async def run():
        e = env
        row = await create(e)
        row = await click(e, row, 'join', 1)
        original = card(e, row)
        e.tg.messages[(str(GROUP), 999)] = {'text': 'unrelated fixture message'}
        entered, release = asyncio.Event(), asyncio.Event()
        transport = e.bot._call_multipart
        async def gated(method, payload, files, **kw):
            if method == 'sendPhoto' and '本局战报' in payload.get('caption', ''):
                entered.set();await release.wait()
            return await transport(method, payload, files, **kw)
        e.bot._call_multipart = gated
        await e.bot._dispatch_update({'callback_query': {'id': 'finish', 'data': f'nn:{row["nonce"]}:start', 'from': e.actors[0], 'message': original}})
        await asyncio.wait_for(entered.wait(), 3)
        assert not deletions(e)
        assert (str(GROUP), row['card_message_id']) in e.tg.messages
        assert '本局已结束' not in e.tg.text(GROUP, row['card_message_id'])
        assert '结果另发' not in e.tg.text(GROUP, row['card_message_id'])
        assert e.tg.actions(GROUP, row['card_message_id']) == []
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        release.set();await drain_ui(e.bot)
        current = svc(e).get(row['nonce'])
        assert current['result_state'] == 'sent' and current['card_delete_state'] == 'deleted'
        assert (str(GROUP), current['card_message_id']) not in e.tg.messages
        assert (str(GROUP), current['result_message_id']) in e.tg.messages
        assert e.tg.text(GROUP, 999) == 'unrelated fixture message'
        assert deletions(e) == [{'chat_id': GROUP, 'message_id': row['card_message_id']}]
        await e.bot._niuniu_result(row['nonce']);await e.bot._niuniu_tick()
        assert len(deletions(e)) == 1 and len(reports(e)) == 1
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())


@pytest.mark.parametrize('state', ['failed', 'unknown', 'retry'])
def test_result_send_failure_keeps_only_original_card(env, state):
    async def run():
        e = env
        row = await create(e)
        row = await click(e, row, 'join', 1)
        transport = e.bot._call_multipart
        async def reject(method, payload, files, **kw):
            if method == 'sendPhoto' and '本局战报' in payload.get('caption', ''):
                CALL_DELIVERY.set({'state': state});return None
            return await transport(method, payload, files, **kw)
        e.bot._call_multipart = reject
        row = await click(e, row, 'start')
        assert row['result_state'] == state and row['card_delete_state'] == 'waiting'
        await e.bot._niuniu_tick()
        assert not deletions(e) and e.tg.message(GROUP, row['card_message_id'])
        assert not e.tg.actions(GROUP, row['card_message_id'])
    asyncio.run(run())


@pytest.mark.parametrize('state', ['retry', 'unknown', 'failed'])
def test_delete_failure_does_not_replay_report_or_money(env, state):
    async def run():
        e = env
        row = await create(e)
        row = await click(e, row, 'join', 1)
        e.delete_mode = state
        row = await click(e, row, 'start')
        assert row['result_state'] == 'sent' and row['card_delete_state'] == ('failed' if state == 'failed' else 'retry')
        assert e.tg.message(GROUP, row['card_message_id']) and len(reports(e)) == 1
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        db = Database(e.db.path);db.close()
        e.delete_mode = 'ok'
        e.db.execute('UPDATE niuniu_rounds SET card_delete_due=0 WHERE nonce=?', (row['nonce'],))
        await e.bot._niuniu_tick();await e.bot._niuniu_tick()
        current = svc(e).get(row['nonce'])
        assert current['card_delete_state'] == ('failed' if state == 'failed' else 'deleted')
        assert len(reports(e)) == 1 and len(deletions(e)) == (1 if state == 'failed' else 2)
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())


def test_cancelled_delete_ack_recovers_exact_target_as_gone_without_resend(env):
    async def run():
        e = env
        row = await create(e)
        row = await click(e, row, 'join', 1)
        original = card(e, row)
        call = e.bot._call
        accepted, never = asyncio.Event(), asyncio.Event()
        async def lost(method, payload, **kw):
            result = await call(method, payload, **kw)
            if method == 'deleteMessage':accepted.set();await never.wait()
            return result
        e.bot._call = lost
        await e.bot._dispatch_update({'callback_query': {'id': 'finish', 'data': f'nn:{row["nonce"]}:start', 'from': e.actors[0], 'message': original}})
        await asyncio.wait_for(accepted.wait(), 3)
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        await e.bot.stop()
        assert svc(e).get(row['nonce'])['card_delete_state'] == 'deleting'
        assert (str(GROUP), row['card_message_id']) not in e.tg.messages
        e.bot._call = call;e.delete_mode = 'gone'
        e.db.execute('UPDATE niuniu_rounds SET card_delete_lease=0 WHERE nonce=?', (row['nonce'],))
        await e.bot._niuniu_tick();await e.bot._niuniu_tick()
        assert svc(e).get(row['nonce'])['card_delete_state'] == 'deleted'
        assert len(reports(e)) == 1 and len(deletions(e)) == 2
        assert deletions(e)[0] == deletions(e)[1]
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())


def test_historical_sent_record_never_enrolled_on_upgrade_or_tick(env):
    async def run():
        e = env
        row = await create(e)
        row = await click(e, row, 'join', 1)
        delete = e.bot._niuniu_delete_card
        async def quiet(nonce):pass
        e.bot._niuniu_delete_card = quiet
        row = await click(e, row, 'start')
        e.db.execute('UPDATE niuniu_rounds SET card_delete_state=? WHERE nonce=?', ('', row['nonce']))
        for column in ('card_delete_state', 'card_delete_lease', 'card_delete_due', 'card_delete_attempts'):
            e.db.execute('ALTER TABLE niuniu_rounds DROP COLUMN '+column)
        db = Database(e.db.path);db.close()
        e.bot._niuniu_delete_card = delete
        await e.bot._niuniu_tick()
        assert svc(e).get(row['nonce'])['card_delete_state'] == ''
        assert not deletions(e) and len(reports(e)) == 1
        assert e.tg.message(GROUP, row['card_message_id'])
    asyncio.run(run())


def test_caption_image_public_nickname_amounts_escaping_and_long_names(monkeypatch):
    row = {'state': 'settled', 'stake': 10, 'config_json': '{"game":"niuniu-banker-v1"}', 'actor_user_id': 'fixture-0', 'result_layout': 'war-report-v1'}
    names = ['<庄家&>', '小明', '小王', '小周', '长名😀<&>'*20]
    players = [{'user_id': f'fixture-{i}', 'tg_id': str(6000+i), 'display_name': name,
                'tg_username': 'not_the_display_label', 'cards_json': json.dumps(list(range(i*5, i*5+5))),
                'result_amount': amount} for i,(name,amount) in enumerate(zip(names, [40,20,0,10,10]))]
    assert [result_amount(row,p) for p in players] == ['0 积分 · 持平', '+10 积分', '-10 积分', '0 积分 · 持平', '0 积分 · 持平']
    body = result_caption(row, players)
    assert body.startswith('🐂 <b>牛牛 · 本局战报</b>')
    assert '<b>庄家</b>' in body and '<b>闲家</b>' in body
    assert '<a href="tg://user?id=6000">&lt;庄家&amp;&gt;</a>' in body
    assert '各自对庄' not in body and '@not_the_display_label' not in body and 'fixture-' not in body
    assert text_units(body) < 1024
    captured, original = [], ImageDraw.ImageDraw.text
    def observe(self, xy, text, *args, **kwargs):
        if text in [result_name(p) for p in players]:
            captured.append(text)
            assert self.textlength(text, font=kwargs['font']) <= 574
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', observe)
    with Image.open(BytesIO(render_room(row, players))) as image:
        assert image.width == 1100 and image.height < 1510
        image.verify()
    assert captured == [result_name(p) for p in players]
    row['state'] = 'cancelled'
    for p, amount in zip(players, [40,10,10,10,10]):p['result_amount'] = amount
    body = result_caption(row, players)
    assert '退回 40 积分' in body and body.count('退回 10 积分') == 4
    assert '+40' not in body and '0 积分 · 持平' not in body
    assert result_name({'display_name': ''}) == '成员'
    assert result_name({'display_name': None, 'username': 'PRIVATE_LOGIN'}) == '成员'
    assert text_units(result_caption(row, [dict(p, display_name='😀'*100) for p in players])) < 1024


def test_delete_lease_protects_other_bot_and_result_message(env):
    async def run():
        e = env
        row = await create(e)
        row = await click(e, row, 'join', 1)
        delete = e.bot._niuniu_delete_card
        async def quiet(nonce):pass
        e.bot._niuniu_delete_card = quiet
        row = await click(e, row, 'start')
        d = NiuniuDelivery(svc(e))
        job = d.claim_delete(row['nonce'])
        assert d.claim_delete(row['nonce']) is None
        d.finish_delete(job, True, {})
        assert d.claim_delete(row['nonce']) is None
        e.db.execute('UPDATE niuniu_rounds SET card_delete_state=?,bot_id=? WHERE nonce=?', ('pending', '999', row['nonce']))
        assert d.claim_delete(row['nonce']) is None
        e.db.execute('UPDATE niuniu_rounds SET bot_id=?,card_message_id=result_message_id WHERE nonce=?', ('123', row['nonce']))
        assert d.claim_delete(row['nonce']) is None
        e.bot._niuniu_delete_card = delete
    asyncio.run(run())
