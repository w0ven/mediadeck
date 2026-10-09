"""Real isolated DB/handlers. No production TG, real gambling, fake prize pool or odds in chat."""
import asyncio
import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.economy_rules import BEIJING
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.play_money import PlayError
from app.modules.points import PointsService
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.scratch9 import DEFAULTS, TIERS, Scratch9Service, configuration, next_slots, reward
from app.modules.scratch9_view import html_preview, render


@pytest.fixture
def env(request):
    e = request.getfixturevalue('bw_env')
    e.services.registry.save('scratch9', enabled=True)
    e.media = []
    for i in (7, 8):
        uid, tg = f'scratch-user-{i}', 990+i
        e.members.upsert(uid, f'private-login-{i}', {'group_id': 'standard'})
        e.members.bind_telegram(uid, str(tg))
        e.actors.append({'id': tg, 'is_bot': False, 'first_name': f'刮友{i}'})
        e.uids.append(uid)
        e.services.points.add(uid, 1000, 'isolated.fixture')
    async def media(method, fields, files, **kw):
        with Image.open(BytesIO(files['photo'][1])) as parsed:
            assert parsed.size == (1100, 1110) and parsed.format == 'PNG'
            parsed.verify()
        e.media.append((method, copy.deepcopy(fields)))
        return await e.tg.call(method, fields)
    e.bot._call_multipart = media
    return e


def svc(e):
    return e.bot._scratch9_service()


def msg(e, index=0, mid=1701, topic=44, text='/刮刮乐'):
    return {'chat': {'id': GROUP, 'type': 'supergroup'}, 'from': e.actors[index], 'message_id': mid, 'text': text,
            **({'message_thread_id': topic, 'is_topic_message': True} if topic else {})}


async def create(e, **kw):
    await e.bot._dispatch_update({'message': msg(e, **kw)})
    return e.db.one('SELECT * FROM scratch9_rounds ORDER BY created_at DESC LIMIT 1')


def card(e, row):
    m = copy.deepcopy(e.tg.message(GROUP, row['card_message_id']))
    m.update(message_id=row['card_message_id'], chat={'id': GROUP, 'type': 'supergroup'}, **{'from': {'id': 123, 'is_bot': True}})
    if row['thread_id']:
        m.update(is_topic_message=True, message_thread_id=row['thread_id'])
    return m


async def select(e, row, cell=1, index=0, request=None):
    request = request or f'select-{cell}-{index}'
    await e.bot._dispatch_update({'callback_query': {'id': request, 'data': f'gg:{row["nonce"]}:{cell}', 'from': e.actors[index], 'message': card(e, row)}})
    return e.db.one('SELECT * FROM scratch9_intents WHERE source_request=?', (request,))


def private(e, intent):
    actor = int(intent['tg_id'])
    m = copy.deepcopy(e.tg.message(actor, intent['message_id']))
    m.update(message_id=intent['message_id'], chat={'id': actor, 'type': 'private'}, **{'from': {'id': 123, 'is_bot': True}})
    return m


async def confirm(e, intent, index=0, action='yes', original=None):
    await e.bot._dispatch_update({'callback_query': {'id': 'confirm-'+intent['token'], 'data': f'ggc:{intent["token"]}:{action}', 'from': e.actors[index], 'message': original or private(e, intent)}})
    return e.db.one('SELECT * FROM scratch9_intents WHERE token=?', (intent['token'],))


def test_default_probabilities_exact_integer_boundaries_and_crypto_rng(monkeypatch):
    cfg = configuration({})
    assert {k: v for k, v in cfg.items() if k != 'reward_tiers'} == {k: v for k, v in DEFAULTS.items() if k != 'reward_tiers'}
    assert json.loads(cfg['reward_tiers']) == TIERS and sum(Decimal(str(t['probability'])) for t in TIERS) == 100
    boundary = 0
    for tier in TIERS:
        width = int(Decimal(str(tier['probability']))*10000)
        assert reward(cfg, lambda n, value=boundary: value) == tier['reward']
        assert reward(cfg, lambda n, value=boundary+width-1: value) == tier['reward']
        boundary += width
    assert boundary == 1000000
    calls = []
    monkeypatch.setattr('secrets.randbelow', lambda n: calls.append(n) or 999999)
    assert reward(cfg) == 888 and calls == [1000000]


@pytest.mark.parametrize('patch', [{'cost': 0}, {'cost': True}, {'per_person': 10}, {'duration_minutes': 0},
    {'schedule_times': '12:00,12:00'}, {'schedule_times': '24:00'}, {'reward_tiers': '[]'},
    {'reward_tiers': '[{"reward":0,"probability":99.9999}]'}, {'reward_tiers': '[{"reward":-1,"probability":100}]'},
    {'reward_tiers': '[{"reward":0,"probability":100.00001}]'}])
def test_admin_config_rejects_invalid(patch):
    with pytest.raises(ValueError):
        configuration(patch)


@pytest.mark.parametrize('draw,amount', [(0, 0), (999999, 888)])
def test_actual_admin_pays_owner_confirm_cancel_repeat_original_public_card_and_independent_reward(env, monkeypatch, draw, amount):
    monkeypatch.setattr('secrets.randbelow', lambda n: draw)
    async def run():
        base = env.services.points.balance(env.uids[0])
        total = sum(env.services.points.balances().values())
        row = await create(env)
        assert row['state'] == 'active' and row['thread_id'] == 44
        mid = row['card_message_id']
        intent = await select(env, row)
        assert env.services.points.balance(env.uids[0]) == base
        original = private(env, intent)
        await confirm(env, intent, action='no')
        assert env.services.points.balance(env.uids[0]) == base
        intent = await select(env, row, request='again')
        original = private(env, intent)
        done = await confirm(env, intent, original=original)
        assert done['state'] == 'done' and json.loads(done['result_json'])['reward'] == amount
        assert env.services.points.balance(env.uids[0]) == base-30+amount
        ledger = copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
        await confirm(env, intent, original=original)
        await select(env, svc(env).get(row['nonce']), 2, request='second-cell')
        assert env.db.query('SELECT * FROM points_ledger') == ledger
        assert sum(env.services.points.balances().values()) == total-30+amount
        cells = svc(env).cells(row['nonce'])
        assert len(cells) == 1 and cells[0]['reward'] == amount and cells[0]['cost'] == 30
        assert bool(cells[0]['reward_ledger_id']) == bool(amount)
        rows = env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'scratch9.%'")
        assert [r['delta'] for r in rows] == ([-30, amount] if amount else [-30])
        assert not env.db.query('SELECT * FROM play_funds') and not env.db.query('SELECT * FROM play_escrows')
        assert env.db.one("SELECT COUNT(*) n FROM audit_log WHERE action='scratch9.claim'")['n'] == 1
        current = svc(env).get(row['nonce'])
        assert current['card_message_id'] == mid and current['revision'] == current['rendered_revision']
        body = env.tg.text(GROUP, mid)
        assert 'admin' in body and '1号' in body and str(amount)+'积分' in body
        for secret in ('概率', '%', '奖池', 'private-login', '余额'):
            assert secret not in body
        buttons = env.tg.actions(GROUP, mid)
        assert len(buttons) == 9 and '分' in buttons[0]['text']
        assert sum(m == 'sendPhoto' for m, _ in env.media) == 1
        assert all(p['message_id'] == mid for m, p in env.media if m == 'editMessageMedia')
    asyncio.run(run())


def test_hand_create_only_trusted_admin_one_scene_per_group_and_snapshot(env):
    async def run():
        assert await create(env, index=1) is None
        untrusted = msg(env)
        untrusted['sender_chat'] = {'id': GROUP}
        await env.bot._dispatch_update({'message': untrusted})
        assert not env.db.query('SELECT * FROM scratch9_rounds')
        row = await create(env)
        again = await create(env, mid=1702, topic=99)
        assert row['nonce'] == again['nonce'] and env.db.one('SELECT COUNT(*) n FROM scratch9_rounds')['n'] == 1
        env.services.registry.save('scratch9', config={'cost': 55, 'per_person': 2, 'reward_tiers': '[{"reward":888,"probability":100}]'})
        intent = await select(env, row, index=1)
        base = env.services.points.balance(env.uids[1])
        await confirm(env, intent, index=1)
        assert svc(env).cells(row['nonce'])[0]['cost'] == 30
        assert env.services.points.balance(env.uids[1]) == base-30+svc(env).cells(row['nonce'])[0]['reward']
        assert json.loads(svc(env).get(row['nonce'])['config_json'])['cost'] == 30
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['chat', 'topic', 'mid', 'sender', 'forward', 'botactor', 'balance', 'deadline', 'confirm_owner', 'confirm_mid', 'confirm_group', 'rebind', 'disabled'])
def test_forged_wrong_context_deadline_low_balance_all_no_debit(env, fault):
    async def run():
        row = await create(env)
        m = card(env, row)
        base = env.db.query('SELECT * FROM points_ledger')
        actor = env.actors[1]
        service = svc(env)
        if fault in ('chat', 'topic', 'mid', 'sender', 'forward', 'botactor'):
            if fault == 'chat':
                m['chat']['id'] -= 1
            elif fault == 'topic':
                m['message_thread_id'] += 1
            elif fault == 'mid':
                m['message_id'] += 1
            elif fault == 'sender':
                m['from']['id'] = 124
            elif fault == 'forward':
                m['forward_origin'] = {'type': 'channel'}
            elif fault == 'botactor':
                actor = {'id': 911, 'is_bot': True}
            with pytest.raises(PlayError):
                service.select(row['nonce'], 1, actor, m, 'forged')
        else:
            intent = await select(env, row, index=1)
            p = private(env, intent)
            clock = time.time()
            if fault == 'balance':
                env.services.points.add(env.uids[1], 29-env.services.points.balance(env.uids[1]), 'isolated.balance')
                base = env.db.query('SELECT * FROM points_ledger')
            elif fault == 'deadline':
                clock = row['expires_at']+1
            elif fault == 'confirm_owner':
                actor = env.actors[2]
            elif fault == 'confirm_mid':
                p['message_id'] += 1
            elif fault == 'confirm_group':
                p['chat'] = {'type': 'supergroup', 'id': GROUP}
            elif fault == 'rebind':
                env.members.bind_telegram(env.uids[1], '12000')
            elif fault == 'disabled':
                env.services.registry.save('scratch9', enabled=False)
            with pytest.raises(PlayError):
                service.confirm(intent['token'], actor, p, 'yes', now=clock)
        assert env.db.query('SELECT * FROM points_ledger') == base and not service.cells(row['nonce'])
    asyncio.run(run())


def local(e, db):
    return Scratch9Service(db, MemberService(db, GroupService(db)), PointsService(db), lambda: e.services.registry.config('scratch9'), lambda: True, e.bot._group_chat_allowed, '123', randbelow=lambda n: 999999)


@pytest.mark.parametrize('same_person', [True, False])
def test_two_db_writers_same_cell_or_same_person_other_cells_exact_one_claim(env, same_person):
    async def prepare():
        row = await create(env)
        intents = [await select(env, row, 1, 1, 'r1'), await select(env, row, 2 if same_person else 1, 1 if same_person else 2, 'r2')]
        return row, intents, [private(env, i) for i in intents]
    row, intents, messages = asyncio.run(prepare())
    before = sum(env.services.points.balances().values())
    conns = [Database(env.db.path), Database(env.db.path)]
    def write(i):
        try:
            return local(env, conns[i]).confirm(intents[i]['token'], env.actors[1 if same_person else i+1], messages[i], 'yes')
        except PlayError:
            return None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(write, (0, 1)))
    finally:
        for db in conns:
            db.close()
    assert sum(o is not None for o in outputs) == 1
    assert len(svc(env).cells(row['nonce'])) == 1
    assert sum(env.services.points.balances().values()) == before-30+888
    assert env.db.one("SELECT COUNT(*) n FROM points_ledger WHERE reason='scratch9.cost'")['n'] == 1


@pytest.mark.parametrize('mode', ['nine', 'timeout'])
def test_nine_complete_or_30minute_close_original_card_no_announcements_and_previews(env, monkeypatch, mode):
    monkeypatch.setattr('secrets.randbelow', lambda n: 999999)
    async def run():
        row = await create(env)
        stages = [('initial', copy.deepcopy(row), [])]
        for i in range(9 if mode == 'nine' else 3):
            intent = await select(env, row, i+1, i)
            await confirm(env, intent, index=i)
            if i == 2:
                stages.append(('playing', svc(env).get(row['nonce']), svc(env).cells(row['nonce'])))
        service = svc(env)
        if mode == 'timeout':
            service.expire_due(now=row['expires_at'])
            await env.bot._scratch9_publish(row['nonce'])
        final = service.get(row['nonce'])
        assert final['state'] == 'closed' and final['end_reason'] == ('complete' if mode == 'nine' else 'timeout')
        assert final['expires_at']-final['created_at'] == 1800
        assert final['card_message_id'] == row['card_message_id'] and final['revision'] == final['rendered_revision']
        # All extra sendMessages are owner-only confirmations, never group award announcements.
        assert all(str(p['chat_id']) != str(GROUP) for m, p in env.tg.calls if m == 'sendMessage')
        ledger = copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
        await select(env, row, 9, 1, 'late')
        service.expire_due(now=row['expires_at']+1)
        assert env.db.query('SELECT * FROM points_ledger') == ledger
        if os.environ.get('SCRATCH9_ARTIFACTS') and mode == 'nine':
            path = Path(os.environ['SCRATCH9_ARTIFACTS'])
            stages.append(('result', final, service.cells(row['nonce'])))
            for name, snapshot, cells in stages:
                (path/f'scratch9-{name}.png').write_bytes(render(snapshot, cells))
                (path/f'scratch9-{name}.html').write_text(html_preview(snapshot, cells, f'scratch9-{name}.png'))
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['unknown', 'failed', 'retry'])
def test_public_initial_delivery_recovery_never_charges_unconfirmed_or_blind_resends(env, failure):
    async def run():
        async def lost(method, fields, files, **kw):
            env.media.append((method, fields))
            CALL_DELIVERY.set({'state': failure})
        env.bot._call_multipart = lost
        row = await create(env)
        assert row['card_message_id'] is None and not svc(env).cells(row['nonce'])
        assert row['state'] == ('pending' if failure == 'retry' else 'closed' if failure == 'failed' else 'unknown')
        await env.bot._scratch9_tick()
        assert len(env.media) == 1
        env.db.execute('UPDATE scratch9_rounds SET next_publish_at=0 WHERE nonce=?', (row['nonce'],))
        await env.bot._scratch9_tick()
        assert len(env.media) == (2 if failure == 'retry' else 1)
        assert not env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'scratch9.%'")
    asyncio.run(run())


def test_confirmation_failed_or_unknown_no_charge_and_cannot_use_forged_private_mid(env):
    async def run():
        row = await create(env)
        original = env.bot._call
        async def fail(method, payload=None, **kw):
            if method == 'sendMessage':
                CALL_DELIVERY.set({'state': 'unknown'})
                return None
            return await original(method, payload, **kw)
        env.bot._call = fail
        before = env.db.query('SELECT * FROM points_ledger')
        intent = await select(env, row, index=1)
        assert intent['state'] == 'unknown' and intent['message_id'] is None
        p = {'message_id': 1888, 'chat': {'id': env.actors[1]['id'], 'type': 'private'}, 'from': {'id': 123, 'is_bot': True}, 'reply_markup': {'inline_keyboard': [[{'callback_data': f'ggc:{intent["token"]}:yes'}]]}}
        with pytest.raises(PlayError):
            svc(env).confirm(intent['token'], env.actors[1], p, 'yes')
        assert env.db.query('SELECT * FROM points_ledger') == before
    asyncio.run(run())


def test_snapshot_render_lease_order_exact_card_retry_and_restarted_send_unknown(env):
    async def run():
        row = await create(env)
        intent = await select(env, row, index=1)
        svc(env).confirm(intent['token'], env.actors[1], private(env, intent), 'yes')
        older = svc(env).begin_publish(row['nonce'])
        second = await select(env, row, cell=2, index=2)
        svc(env).confirm(second['token'], env.actors[2], private(env, second), 'yes')
        assert svc(env).begin_publish(row['nonce']) is None
        assert svc(env).published(older, row['card_message_id'])
        current = svc(env).get(row['nonce'])
        assert current['revision'] > current['rendered_revision']
        newer = svc(env).begin_publish(row['nonce'])
        assert not svc(env).published(older, row['card_message_id'])
        svc(env).publish_failed(newer, {'state': 'unknown'}, now=time.time()-20)
        await env.bot._scratch9_tick()
        assert svc(env).get(row['nonce'])['rendered_revision'] == svc(env).get(row['nonce'])['revision']
        assert sum(m == 'sendPhoto' for m, _ in env.media) == 1
    asyncio.run(run())


@pytest.mark.parametrize('hour', [12, 20])
def test_future_slot_timezone_dedup_restart_disable_and_manual_auto_overlap(env, hour):
    service = svc(env)
    local_day = datetime.now(BEIJING).replace(hour=hour, minute=0, second=0, microsecond=0)
    at = local_day.timestamp()
    assert service.schedule([str(GROUP)], 'boot', now=at-5) == []
    slots = service.schedule([str(GROUP)], 'boot', now=at+1)
    assert len(slots) == 1 and slots[0]['slot_at'] == at
    row = service.auto_create(slots[0], {'id': GROUP, 'type': 'supergroup'}, now=at+1)
    assert row['thread_id'] == 0 and row['command_message_id'] is None
    assert service.auto_create(slots[0], {'id': GROUP, 'type': 'supergroup'}, now=at+2) is None
    assert service.schedule([str(GROUP)], 'boot', now=at+3) == []
    assert service.schedule([str(GROUP)], 'restarted', now=at+10) == []
    assert service.schedule([str(GROUP)], 'restarted', now=at+400) == []
    future = next_slots(configuration({}), at+400)
    assert all(d.timestamp() > at+400 for d in future) and future[0].tzinfo == BEIJING
    # A manual scene and an auto slot resolve to only one activity per group.
    env.db.execute("UPDATE scratch9_rounds SET state='closed' WHERE nonce=?", (row['nonce'],))
    manual = service.create(msg(env, mid=1777), now=at+500)
    other = at+86400
    service.schedule([str(GROUP)], 'restarted', now=other-4)
    slot = service.schedule([str(GROUP)], 'restarted', now=other+1)[0]
    env.db.execute('UPDATE scratch9_rounds SET expires_at=? WHERE nonce=?', (other+1000, manual['nonce']))
    assert service.auto_create(slot, {'id': GROUP, 'type': 'supergroup'}, now=other+1) is None
    assert env.db.one("SELECT COUNT(*) n FROM scratch9_rounds WHERE state IN ('pending','active','unknown')")['n'] == 1
    env.services.registry.save('scratch9', enabled=False)
    assert service.schedule([str(GROUP)], 'restarted', now=other+2) == []
    env.services.registry.save('scratch9', enabled=True)
    assert service.schedule([str(GROUP)], 'restarted', now=other+3) == []


def test_random_failure_rolls_back_fee_and_cell_and_confirmation_timeout(env, monkeypatch):
    async def run():
        row = await create(env)
        intent = await select(env, row, index=1)
        before = env.db.query('SELECT * FROM points_ledger')
        with pytest.raises(PlayError, match='超时'):
            svc(env).confirm(intent['token'], env.actors[1], private(env, intent), 'yes', now=intent['expires_at']+1)
        def broken(n):
            raise RuntimeError('isolated entropy failure')
        monkeypatch.setattr('secrets.randbelow', broken)
        with pytest.raises(RuntimeError):
            svc(env).confirm(intent['token'], env.actors[1], private(env, intent), 'yes')
        assert env.db.query('SELECT * FROM points_ledger') == before and not svc(env).cells(row['nonce'])
        assert env.db.one('SELECT state FROM scratch9_intents WHERE token=?', (intent['token'],))['state'] == 'pending'
    asyncio.run(run())


def test_actual_tick_crosses_future_slot_sends_once_and_restarted_does_not_catch_up(env, monkeypatch):
    clock = [datetime.now(BEIJING).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()-5]
    monkeypatch.setattr('app.modules.scratch9.time.time', lambda: clock[0])
    async def run():
        await env.bot._scratch9_tick()
        assert not env.media
        clock[0] += 6
        await env.bot._scratch9_tick()
        await env.bot._scratch9_tick()
        assert [method for method, _ in env.media] == ['sendPhoto']
        row = env.db.one('SELECT * FROM scratch9_rounds')
        assert row['state'] == 'active' and row['command_message_id'] is None
        assert env.db.one('SELECT state FROM scratch9_slots')['state'] == 'created'
        del env.bot._scratch9_boot_key
        clock[0] += 8*3600+1
        await env.bot._scratch9_tick()
        assert sum(method == 'sendPhoto' for method, _ in env.media) == 1 and env.db.one('SELECT COUNT(*) n FROM scratch9_rounds')['n'] == 1
    asyncio.run(run())


def test_restarted_sending_becomes_unknown_without_new_message_or_charge(env):
    service = svc(env)
    row = service.create(msg(env))
    claimed = service.begin_publish(row['nonce'])
    assert claimed is not None
    resumed = local(env, Database(env.db.path))
    try:
        resumed.expire_due(now=time.time()+95)
        assert resumed.get(row['nonce'])['state'] == 'unknown'
        assert resumed.begin_publish(row['nonce'], now=time.time()+96) is None
        assert not resumed.pending_cards(now=time.time()+96)
    finally:
        resumed.db.close()
    assert not env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'scratch9.%'")
