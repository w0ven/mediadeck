"""Actual handlers/SQLite: fixed 4N banker, pairwise ±N, same picture and legacy snapshots."""
import asyncio
import copy
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from itertools import combinations
from pathlib import Path

import pytest
from PIL import Image
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.economy_rules import economy_write, encode
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.niuniu import CATEGORIES, NiuniuService, rank, strength
from app.modules.niuniu_image import render_room
from app.modules.play_money import PlayError
from app.modules.points import PointsService
from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def env(request):
    e = request.getfixturevalue('bw_env')
    e.services.registry.save('niuniu', enabled=True)
    e.photos = []
    async def photo(method, fields, files, **kw):
        with Image.open(BytesIO(files['photo'][1])) as parsed:
            assert parsed.width == 1100 and parsed.format == 'PNG'
            parsed.verify()
        e.photos.append((method, copy.deepcopy(fields)))
        return await e.tg.call(method, fields)
    e.bot._call_multipart = photo
    return e


def svc(e):
    return e.bot._niuniu_service()


def cash(e):
    return sum(e.services.points.balances().values())+(e.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0)+(e.db.one('SELECT SUM(amount) n FROM play_funds')['n'] or 0)


def message(e, text='/牛牛', index=0, mid=1001, topic=0):
    return {'chat': {'id': GROUP, 'type': 'supergroup'}, 'from': e.actors[index], 'text': text, 'message_id': mid,
            **({'is_topic_message': True, 'message_thread_id': topic} if topic else {'message_thread_id': mid+100})}


async def create(e, text='/牛牛', **kw):
    await e.bot._dispatch_update({'message': message(e, text, **kw)})
    return e.db.one('SELECT * FROM niuniu_rounds ORDER BY created_at DESC LIMIT 1')


def card(e, row):
    m = copy.deepcopy(e.tg.message(GROUP, row['card_message_id']))
    m.update(chat={'id': GROUP, 'type': 'supergroup'}, message_id=row['card_message_id'], reply_to_message={'message_id': row['command_message_id']})
    m['from'] = {'id': 123, 'is_bot': True}
    if row['thread_id']:
        m.update(is_topic_message=True, message_thread_id=row['thread_id'])
    else:
        m['message_thread_id'] = 991
    return m


async def click(e, row, op, index=0, original=None):
    await e.bot._dispatch_update({'callback_query': {'id': 'nn-'+op, 'data': f'nn:{row["nonce"]}:{op}', 'from': e.actors[index], 'message': original or card(e, row)}})
    return svc(e).get(row['nonce'])


def c(value, suit=0):
    return suit*13+(12 if value == 1 else value-2)


def test_all_eleven_types_and_fixed_rank_suit_without_special_hands():
    samples = {}
    for hand in combinations(range(20), 5):
        samples.setdefault(strength(hand)[0], hand)
        if len(samples) == 11:
            break
    assert set(samples) == set(range(11)) and len(CATEGORIES) == 11
    for key, hand in samples.items():
        points = [min(rank(x), 10) for x in hand]
        independent = 0
        for triple in combinations(range(5), 3):
            if sum(points[i] for i in triple)%10 == 0:
                independent = sum(points[i] for i in range(5) if i not in triple)%10 or 10
        assert strength(hand)[0] == independent == key
        assert strength(hand[::-1]) == strength(hand)
    assert strength([c(11), c(12), c(13), c(11, 1), c(12, 1)])[0] == 10
    for hand in ([c(1), c(1, 1), c(1, 2), c(1, 3), c(2)], [c(2), c(2, 1), c(2, 2), c(2, 3), c(4)]):
        assert strength(hand)[0] == 0
    same = [c(10), c(6), c(4), c(11), c(1)]
    royal = [c(10, 1), c(6, 1), c(4, 1), c(13, 1), c(1, 1)]
    assert strength(royal)[0] == strength(same)[0] and strength(royal) > strength(same)
    assert strength([c(10, 2), c(6, 2), c(4, 2), c(13), c(1, 2)]) > strength(royal)
    assert rank(c(1)) == 1 and rank(c(13)) == 13
    for hand in ([0, 1, 2], [0, 0, 1, 2, 3], [0, 1, 2, 3, 52], [True, 1, 2, 3, 4]):
        with pytest.raises(ValueError):
            strength(hand)


@pytest.mark.parametrize('stake', [10, 100])
def test_actual_bank4n_guestn_start_permissions_repeat_and_same_media(env, stake):
    async def run():
        initial = cash(env)
        balances = [env.services.points.balance(u) for u in env.uids]
        row = await create(env, f'/牛牛 {stake}')
        assert env.services.points.balance(env.uids[0]) == balances[0]-4*stake
        assert row['thread_id'] == 0 and row['card_format'] == 'photo'
        mid = row['card_message_id']
        origin = card(env, row)
        assert not any(b['callback_data'].endswith(':leave') for line in origin['reply_markup']['inline_keyboard'] for b in line)
        await click(env, row, 'start')
        assert svc(env).get(row['nonce'])['state'] == 'lobby'
        row = await click(env, row, 'join', 1)
        assert env.services.points.balance(env.uids[1]) == balances[1]-stake
        await click(env, row, 'join', 1, origin)
        await click(env, row, 'start', 1)
        assert len(svc(env).players(row['nonce'])) == 2 and svc(env).get(row['nonce'])['state'] == 'lobby'
        row = await click(env, row, 'start')
        players = svc(env).players(row['nonce'])
        assert row['state'] == 'settled' and row['card_message_id'] == mid
        assert len({x for p in players for x in json.loads(p['cards_json'])}) == 10
        assert sum(p['result_amount'] for p in players) == 5*stake
        result = json.loads(row['result_json'])
        assert result['mode'] == 'banker' and result['unused_refund'] == 3*stake
        delta = [env.services.points.balance(u)-balances[i] for i, u in enumerate(env.uids[:2])]
        assert delta[0] == -delta[1] and abs(delta[0]) == stake
        ledger = copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
        await click(env, row, 'start', 0, origin)
        await click(env, row, 'join', 1, origin)
        await env.bot._niuniu_tick()
        assert env.db.query('SELECT * FROM points_ledger') == ledger and cash(env) == initial
        body = env.tg.text(GROUP, mid)
        assert '揭晓' in body and '对庄' in body and '积分' in body
        for secret in ('余额', 'private-login', '总池'):
            assert secret not in body
        assert [m for m, _ in env.photos] == ['sendPhoto', 'editMessageMedia', 'editMessageMedia']
        assert all(p['message_id'] == mid for m, p in env.photos if m == 'editMessageMedia')
        assert row['revision'] == row['rendered_revision']
    asyncio.run(run())


@pytest.mark.parametrize('count', [1, 2, 3, 4])
@pytest.mark.parametrize('pattern', ['all_guest_win', 'all_guest_lose', 'mixed'])
def test_each_guest_only_against_banker_fixed_plusminus_and_worst_loss(env, monkeypatch, count, pattern):
    rng = random.Random(4)
    for _ in range(10000):
        deck = list(range(52))
        rng.shuffle(deck)
        values = [strength(deck[i*5:(i+1)*5]) for i in range(count+1)]
        wins = [v > values[0] for v in values[1:]]
        if ((pattern == 'all_guest_win' and all(wins)) or (pattern == 'all_guest_lose' and not any(wins))
                or (pattern == 'mixed' and (count == 1 or 0 < sum(wins) < count))):
            break
    else:
        raise AssertionError('no deterministic deck')
    monkeypatch.setattr('secrets.SystemRandom.shuffle', lambda self, target: target.__setitem__(slice(None), deck))
    async def run():
        initial = cash(env)
        base = [env.services.points.balance(u) for u in env.uids]
        row = await create(env)
        for i in range(1, count+1):
            row = await click(env, row, 'join', i)
        row = await click(env, row, 'start') if count < 4 else row
        assert row['state'] == 'settled'
        for i, won in enumerate(wins, 1):
            assert env.services.points.balance(env.uids[i])-base[i] == (10 if won else -10)
        bank_net = env.services.points.balance(env.uids[0])-base[0]
        assert bank_net == 10*(count-2*sum(wins)) and abs(bank_net) <= 40
        assert cash(env) == initial
        assert env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='niuniu'")['n'] == 0
        assert env.db.one("SELECT amount FROM play_funds WHERE ref=?", ('niuniu:'+row['nonce'],))['amount'] == 0
        assert env.db.one("SELECT COUNT(*) n FROM play_cash_events WHERE ref=? AND operation='payout'", ('niuniu:'+row['nonce'],))['n'] == count
    asyncio.run(run())


def test_no_active_leave_even_old_button_and_timeout_full_refund(env):
    async def run():
        balances = env.services.points.balances()
        row = await create(env)
        row = await click(env, row, 'join', 1)
        original = card(env, row)
        original['reply_markup']['inline_keyboard'].append([{'text': '旧退桌', 'callback_data': f'nn:{row["nonce"]}:leave'}])
        ledger = env.db.query('SELECT * FROM points_ledger')
        for index in (0, 1):
            await click(env, row, 'leave', index, original)
            with pytest.raises(PlayError, match='不能主动'):
                svc(env).lobby(row['nonce'], env.actors[index], original, 'leave')
        assert env.db.query('SELECT * FROM points_ledger') == ledger
        assert svc(env).get(row['nonce'])['state'] == 'lobby'
        svc(env).expire(row['nonce'], now=row['expires_at']+1)
        svc(env).expire(row['nonce'], now=row['expires_at']+2)
        assert env.services.points.balances() == balances
        assert [p['result_amount'] for p in svc(env).players(row['nonce'])] == [40, 10]
    asyncio.run(run())


def local(e, db):
    return NiuniuService(db, MemberService(db, GroupService(db)), PointsService(db), lambda: e.services.registry.config('niuniu'), lambda: True, e.bot._group_chat_allowed, '123')


@pytest.mark.parametrize('compete', ['host', 'last_seat'])
def test_two_connection_race_no_sixth_or_duplicate_settlement(env, compete):
    async def prepare():
        row = await create(env)
        for i in range(1, 4):
            row = await click(env, row, 'join', i)
        return row
    row = asyncio.run(prepare())
    m = card(env, row)
    before = cash(env)
    conns = [Database(env.db.path), Database(env.db.path)]
    pairs = [(conns[0], 0 if compete == 'host' else 5), (conns[1], 4)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda pair: local(env, pair[0]).lobby(row['nonce'], env.actors[pair[1]], m, 'start' if pair[1] == 0 else 'join'), pairs))
    finally:
        for db in conns:
            db.close()
    row = svc(env).get(row['nonce'])
    players = svc(env).players(row['nonce'])
    assert row['state'] == 'settled' and len(players) in (4, 5) and cash(env) == before
    assert env.db.one("SELECT COUNT(*) n FROM play_cash_events WHERE ref=? AND operation='payout'", ('niuniu:'+row['nonce'],))['n'] == len(players)-1


@pytest.mark.parametrize('balance', [0, 10, 39, 40])
def test_banker_always_needs_4n_no_dynamic_seats_or_partial_debit(env, balance):
    env.services.points.add(env.uids[0], balance-env.services.points.balance(env.uids[0]), 'isolated.balance')
    row = asyncio.run(create(env))
    assert (row is not None) == (balance >= 40)
    if balance < 40:
        assert not env.db.query("SELECT * FROM play_escrows WHERE scope='niuniu'")
        assert env.services.points.balance(env.uids[0]) == balance
    else:
        assert env.services.points.balance(env.uids[0]) == 0


@pytest.mark.parametrize('fault', ['member', 'dealer', 'running'])
def test_system_fault_or_restart_running_refunds_full_collateral_once(env, monkeypatch, fault):
    async def run():
        base = env.services.points.balances()
        row = await create(env)
        row = await click(env, row, 'join', 1)
        if fault == 'member':
            env.members.upsert(env.uids[1], 'private-login', {'status': 'suspended'})
        elif fault == 'dealer':
            def broken(self, deck):
                raise RuntimeError('isolated failure')
            monkeypatch.setattr('secrets.SystemRandom.shuffle', broken)
        if fault == 'running':
            env.db.execute("UPDATE niuniu_rounds SET state='running' WHERE nonce=?", (row['nonce'],))
            db = Database(env.db.path)
            try:
                local(env, db).expire_due()
                local(env, db).expire_due()
            finally:
                db.close()
        else:
            await click(env, row, 'start')
        assert svc(env).get(row['nonce'])['state'] == 'cancelled'
        assert env.services.points.balances() == base
        assert all(p['cards_json'] == '[]' for p in svc(env).players(row['nonce']))
        assert sum(m == 'sendPhoto' for m, _ in env.photos) == 1
    asyncio.run(run())


def test_legacy_text_room_original_money_snapshot_no_new_result_photo(env):
    async def run():
        service = svc(env)
        base = env.services.points.balances()
        row = service.create(message(env))
        # Recreate the actual pre-upgrade money/config/message format in isolated DB.
        with economy_write(env.db) as conn:
            p = service.players(row['nonce'])[0]
            service.cash.release(conn, 'niuniu', p['escrow_ref'], p['user_id'], 30)
            cfg = json.loads(row['config_json'])
            cfg['game'] = 'niuniu5-v1'
            conn.execute("UPDATE niuniu_rounds SET config_json=?,card_format='text',photo_state='pending' WHERE nonce=?", (encode(cfg), row['nonce']))
        await env.bot._niuniu_publish(row['nonce'])
        row = service.get(row['nonce'])
        mid = row['card_message_id']
        row = await click(env, row, 'join', 1)
        row = await click(env, row, 'start')
        players = service.players(row['nonce'])
        assert row['state'] == 'settled' and json.loads(row['result_json'])['mode'] == 'win'
        assert sum(p['result_amount'] for p in players) == 20
        assert sorted(env.services.points.balance(u)-base[u] for u in env.uids[:2]) == [-10, 10]
        await env.bot._niuniu_tick()
        assert not env.photos and row['card_message_id'] == mid
        assert any(m == 'editMessageText' and p['message_id'] == mid for m, p in env.tg.calls)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['unknown', 'retry', 'failed'])
def test_initial_photo_delivery_policy_and_exact_edit_failure(env, failure):
    async def run():
        base = env.services.points.balances()
        async def lost(method, fields, files, **kw):
            env.photos.append((method, fields))
            CALL_DELIVERY.set({'state': failure})
        env.bot._call_multipart = lost
        row = await create(env)
        before = len(env.photos)
        await env.bot._niuniu_tick()
        assert len(env.photos) == before
        if failure == 'failed':
            assert svc(env).get(row['nonce'])['state'] == 'cancelled'
            assert env.services.points.balances() == base
        elif failure == 'unknown':
            assert row['card_send_state'] == 'unknown'
        else:
            assert row['card_send_state'] == 'pending'
            env.db.execute('UPDATE niuniu_rounds SET next_publish_at=0 WHERE nonce=?', (row['nonce'],))
            await env.bot._niuniu_tick()
            assert len(env.photos) == before+1
    asyncio.run(run())


def test_real_renderer_previews_and_edit_snapshot_order(env):
    async def run():
        row = await create(env)
        stages = [('initial', copy.deepcopy(row), svc(env).players(row['nonce']))]
        row = await click(env, row, 'join', 1)
        stages.append(('playing', copy.deepcopy(row), svc(env).players(row['nonce'])))
        row = await click(env, row, 'start')
        stages.append(('result', copy.deepcopy(row), svc(env).players(row['nonce'])))
        if os.environ.get('SCRATCH9_ARTIFACTS'):
            path = Path(os.environ['SCRATCH9_ARTIFACTS'])
            for name, snapshot, players in stages:
                (path/f'niuniu-{name}.png').write_bytes(render_room(snapshot, players))
                body, keys = env.bot._niuniu_view(dict(snapshot, _players=players), svc(env))
                html = '<!doctype html><meta charset="utf-8"><title>牛牛真实原卡预览</title><body style="background:#102b35;color:#eddbad;font:16px system-ui;max-width:550px;margin:auto"><img style="width:100%" src="niuniu-'+name+'.png"><div style="white-space:pre-line">'+body+'</div><pre>'+json.dumps([[b['text'] for b in line] for line in keys], ensure_ascii=False)+'</pre><p>静态renderer；非群消息，非真人下注。</p>'
                (path/f'niuniu-{name}.html').write_text(html)
        # A leased older snapshot must not erase the need to render a newer version.
        env.db.execute('UPDATE niuniu_rounds SET revision=revision+1 WHERE nonce=?', (row['nonce'],))
        old = svc(env).begin_publish(row['nonce'])
        env.db.execute('UPDATE niuniu_rounds SET revision=revision+1 WHERE nonce=?', (row['nonce'],))
        assert svc(env).begin_publish(row['nonce']) is None
        assert svc(env).published(old, row['card_message_id'])
        assert svc(env).get(row['nonce'])['rendered_revision'] < svc(env).get(row['nonce'])['revision']
        newer = svc(env).begin_publish(row['nonce'])
        assert newer['revision'] > old['revision']
        assert not svc(env).published(old, row['card_message_id'])
        svc(env).publish_failed(newer, {'state': 'unknown'}, now=time.time())
    asyncio.run(run())


async def feedback(e, row, op, *, index=1, data=None, original=None, actor=None):
    offset = len(e.tg.calls)
    callback_id = f'feedback-{offset}'
    await e.bot._dispatch_update({'callback_query': {
        'id': callback_id, 'data': data or f'nn:{row["nonce"]}:{op}',
        'from': actor or e.actors[index], 'message': original or card(e, row)}})
    calls = e.tg.calls[offset:]
    replies = [p for method, p in calls if method == 'answerCallbackQuery' and p['callback_query_id'] == callback_id]
    assert len(replies) == 1
    assert not any(method in ('sendMessage', 'sendPhoto') for method, _ in calls)
    return replies[0]


@pytest.mark.parametrize('balance', [0, 9])
@pytest.mark.parametrize('stake', [10, 100])
def test_insufficient_join_first_only_visible_alert_no_debit_seat_or_card_change(env, balance, stake):
    async def run():
        row = await create(env, f'/牛牛 {stake}')
        env.services.points.add(env.uids[1], balance-env.services.points.balance(env.uids[1]), 'isolated.balance')
        ledger = env.db.query('SELECT * FROM points_ledger')
        escrows = env.db.query('SELECT * FROM play_escrows')
        players = svc(env).players(row['nonce'])
        original = copy.deepcopy(env.tg.message(GROUP, row['card_message_id']))
        photos = copy.deepcopy(env.photos)
        reply = await feedback(env, row, 'join')
        assert reply['show_alert'] is True and '积分不足，未加入' in reply['text']
        assert f'需 {stake} 积分' in reply['text']
        assert '余额' not in reply['text'] and str(env.uids[1]) not in reply['text']
        assert env.services.points.balance(env.uids[1]) == balance
        assert env.db.query('SELECT * FROM points_ledger') == ledger
        assert env.db.query('SELECT * FROM play_escrows') == escrows
        assert svc(env).players(row['nonce']) == players
        assert env.tg.message(GROUP, row['card_message_id']) == original and env.photos == photos
    asyncio.run(run())


@pytest.mark.parametrize('case', ['start', 'leave', 'malformed', 'missing', 'wrong_card', 'bot_actor', 'missing_help'])
def test_callback_rejections_once_alert_without_public_message(env, case):
    async def run():
        row = await create(env)
        data = None
        original = card(env, row)
        actor = env.actors[1]
        if case == 'malformed': data = 'nn:broken'
        if case == 'missing': data = 'nn:missing:join'
        if case == 'missing_help': data = 'nnh:missing'
        if case == 'wrong_card': original['message_id'] += 1
        if case == 'bot_actor': actor = {**actor, 'is_bot': True}
        ledger = env.db.query('SELECT * FROM points_ledger')
        reply = await feedback(env, row, case if case in ('start', 'leave') else 'join',
                               index=0 if case == 'start' else 1, data=data, original=original,
                               actor=env.actors[0] if case == 'start' else actor)
        assert reply['show_alert'] is True and reply['text']
        assert '余额' not in reply['text']
        assert env.db.query('SELECT * FROM points_ledger') == ledger
        assert len(svc(env).players(row['nonce'])) == 1
    asyncio.run(run())


def test_normal_join_duplicate_start_and_settled_replay_one_response_same_photo(env):
    async def run():
        row = await create(env)
        mid = row['card_message_id']
        origin = card(env, row)
        initial = env.services.points.balance(env.uids[1])
        for op, index in (('join', 1), ('join', 1), ('start', 0), ('start', 0), ('join', 1)):
            reply = await feedback(env, row, op, index=index, original=origin)
            assert reply['text'] == '' and reply['show_alert'] is False
        current = svc(env).get(row['nonce'])
        assert current['state'] == 'settled' and current['card_message_id'] == mid
        assert len(svc(env).players(row['nonce'])) == 2
        assert abs(env.services.points.balance(env.uids[1])-initial) == row['stake']
        assert [method for method, _ in env.photos] == ['sendPhoto', 'editMessageMedia', 'editMessageMedia']
        assert all(fields['message_id'] == mid for method, fields in env.photos if method == 'editMessageMedia')
        assert '揭晓' in env.tg.text(GROUP, mid) and '余额' not in env.tg.text(GROUP, mid)
    asyncio.run(run())


@pytest.mark.parametrize('sent', [True, False])
def test_help_response_once_preserves_existing_private_delivery_not_group(env, sent, monkeypatch):
    async def run():
        row = await create(env)
        targets = []
        async def private_help(chat, text, **kwargs):
            targets.append(chat)
            return 987 if sent else None
        monkeypatch.setattr(env.bot, 'send_message', private_help)
        ledger = env.db.query('SELECT * FROM points_ledger')
        reply = await feedback(env, row, 'help', data=f'nnh:{row["nonce"]}')
        assert targets == [str(env.actors[1]['id'])]
        assert reply['text'] == ('玩法已发私聊' if sent else '请先打开Bot，再点玩法')
        assert reply['show_alert'] is (not sent)
        assert env.db.query('SELECT * FROM points_ledger') == ledger
        assert len(env.photos) == 1
    asyncio.run(run())


def test_publish_failure_after_success_cannot_answer_same_callback_twice(env, monkeypatch):
    async def run():
        row = await create(env)
        async def publish_failure(nonce):
            raise PlayError('积分服务暂不可用')
        monkeypatch.setattr(env.bot, '_niuniu_publish', publish_failure)
        offset = len(env.tg.calls)
        with pytest.raises(PlayError):
            await env.bot._niuniu_callback(f'nn:{row["nonce"]}:join', card(env, row), env.actors[1], 'publish-failure')
        replies = [p for method, p in env.tg.calls[offset:] if method == 'answerCallbackQuery']
        assert replies == [{'callback_query_id': 'publish-failure', 'text': '', 'show_alert': False}]
        assert len(svc(env).players(row['nonce'])) == 2
    asyncio.run(run())


def test_existing_disabled_room_refunds_banker_once_and_keeps_guest_unseated(env):
    async def run():
        banker_before = env.services.points.balance(env.uids[0])
        row = await create(env)
        origin = card(env, row)
        guest_before = env.services.points.balance(env.uids[1])
        env.services.registry.save('niuniu', enabled=False)
        for _ in range(2):
            reply = await feedback(env, row, 'join', original=origin)
            assert reply == {'callback_query_id': reply['callback_query_id'], 'text': '', 'show_alert': False}
        assert svc(env).get(row['nonce'])['state'] == 'cancelled'
        assert env.services.points.balance(env.uids[0]) == banker_before
        assert env.services.points.balance(env.uids[1]) == guest_before
        assert len(svc(env).players(row['nonce'])) == 1
        assert [method for method, _ in env.photos] == ['sendPhoto', 'editMessageMedia']
    asyncio.run(run())
