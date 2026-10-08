"""Real Bot handlers, original cards, SQLite money, concurrency and recovery."""
import asyncio
import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import ADMIN, GROUP

from app.core.db import Database
from app.modules.blackwhite import BlackwhiteService
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.play_money import CashBook, PlayError
from app.modules.points import PointsService


@pytest.fixture
def env(request):
    e = request.getfixturevalue('economy_env')
    e.services.registry.save('blackwhite', enabled=True)
    e.services.registry.get('blackwhite').ctx.telegram = e.bot
    actors = [{'id': ADMIN, 'is_bot': False, 'first_name': 'admin'}]
    for i in range(6):
        tg = 910 + i
        uid = f'bw-user-{i}'
        e.members.upsert(uid, f'private-login-{i}', {'group_id': 'standard'})
        e.members.bind_telegram(uid, str(tg))
        actors.append({'id': tg, 'is_bot': False, 'first_name': f'<小{i}&>'})
    e.actors = actors
    e.uids = [e.members.find_by_telegram(str(a['id']))['emby_user_id'] for a in actors]
    for uid in e.uids:
        e.services.points.add(uid, 1000, 'isolated.fixture')
    return e


def command_message(env, mid=601, text='/黑白板 50', thread=25):
    return {'from': env.actors[0], 'chat': {'id': GROUP, 'type': 'supergroup'},
            'message_id': mid, 'message_thread_id': thread, 'text': text}


async def create(env, stake=50, mid=601, thread=25):
    await env.bot._dispatch_update({'message': command_message(env, mid, f'/黑白板 {stake}', thread)})
    return env.db.one("SELECT * FROM play_rounds WHERE kind='blackwhite' ORDER BY created_at DESC LIMIT 1")


def card(env, row):
    body = copy.deepcopy(env.tg.message(GROUP, row['card_message_id']))
    body.update(chat={'id': GROUP, 'type': 'supergroup'}, from_={})
    body['from'] = {'id': 123, 'is_bot': True}
    body['message_id'] = row['card_message_id']
    body['reply_to_message'] = {'message_id': row['command_message_id']}
    body.pop('from_', None)
    return body


async def choose(env, row, index, color, message=None):
    data = f'bw:{row["nonce"]}:{color}'
    await env.bot._dispatch_update({'callback_query': {'id': f'cb-{index}', 'data': data,
        'message': message or card(env, row), 'from': env.actors[index]}})
    return env.bot._blackwhite_service().get(row['nonce'])


def total(env):
    return (sum(env.services.points.balances().values())
            + (env.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0)
            + (env.db.one('SELECT SUM(amount) n FROM play_funds')['n'] or 0))


@pytest.mark.parametrize('colors', [
    ['black', 'white', 'white', 'white', 'white'],
    ['black', 'black', 'white', 'white', 'white'],
    ['white', 'white', 'black', 'black', 'black'],
    ['white'] * 5, ['black'] * 5,
])
def test_actual_full_table_secret_then_public_results_and_real_admin_debit(env, colors):
    initial = total(env)
    balances = {uid: env.services.points.balance(uid) for uid in env.uids}
    assert env.bot.is_admin(env.members.find_by_telegram(str(ADMIN)))
    async def run():
        row = await create(env)
        original = row['card_message_id']
        waiting_sample = ''
        for i, color in enumerate(colors):
            row = await choose(env, row, i, color)
            body = env.tg.text(GROUP, original)
            assert 'private-login' not in body and '当前余额' not in body and 'actor_user_id' not in body
            if i < 4:
                assert row['state'] == 'lobby' and f'{i+1}/5' in body
                assert '胜' not in body and '· 黑板 ·' not in body and '· 白板 ·' not in body
                assert env.services.points.balance(env.uids[i]) == balances[env.uids[i]] - 50
            assert row['card_message_id'] == original
            if i == 1:
                waiting_sample = body
        assert row['state'] == 'settled'
        assert row['revision'] == row['rendered_revision']
        assert len([1 for m, _ in env.tg.calls if m == 'sendMessage']) == 1
        assert env.db.one('SELECT amount FROM play_funds WHERE kind=? AND ref=?', ('blackwhite', row['nonce']))['amount'] == 0
        body = env.tg.text(GROUP, original)
        assert '&lt;小0&amp;&gt;' in body
        if len(set(colors)) == 1:
            assert '全额退回' in body
            assert all(env.services.points.balance(uid) == balances[uid] for uid in env.uids)
        else:
            assert sum(p['result_amount'] for p in env.bot._blackwhite_service().players(row['nonce'])) == 250
            assert '未中奖' in body and '· 0 积分' not in body
        saved = env.services.points.ledger(env.uids[0])
        # Replay the original pre-settlement buttons cannot debit or switch choice.
        old = card(env, row)
        old['reply_markup'] = {'inline_keyboard': [[{'callback_data': f'bw:{row["nonce"]}:{colors[0]}'}]]}
        await choose(env, row, 0, colors[0], old)
        assert env.services.points.ledger(env.uids[0]) == saved
        if os.environ.get('GAMES_MARKET_ARTIFACTS') and len(set(colors)) > 1:
            p = Path(os.environ['GAMES_MARKET_ARTIFACTS']) / 'blackwhite-effect.json'
            p.write_text(json.dumps({'waiting': waiting_sample, 'settled': body, 'original_message_id_unchanged': True}, ensure_ascii=False, indent=2)+'\n')
    asyncio.run(run())
    assert total(env) == initial


def test_choice_locked_and_insufficient_are_atomic(env):
    async def run():
        row = await create(env)
        await choose(env, row, 0, 'white')
        saved = env.services.points.ledger(env.uids[0])
        await choose(env, row, 0, 'black')
        assert env.services.points.ledger(env.uids[0]) == saved
        assert env.bot._blackwhite_service().players(row['nonce'])[0]['choice'] == 'white'
        env.services.points.add(env.uids[1], -env.services.points.balance(env.uids[1]), 'isolated.fixture')
        await choose(env, row, 1, 'black')
        assert len(env.bot._blackwhite_service().players(row['nonce'])) == 1
        assert env.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 1
        alerts = [p.get('text', '') for m, p in env.tg.calls if m == 'answerCallbackQuery']
        assert any('不能更改' in a for a in alerts) and any('积分不足' in a for a in alerts)
    asyncio.run(run())


@pytest.mark.parametrize('changed', ['chat', 'thread', 'mid', 'sender', 'buttons', 'forward'])
def test_callback_original_context_not_spoofable(env, changed):
    async def run():
        row = await create(env)
        forged = card(env, row)
        if changed == 'chat': forged['chat']['id'] -= 1
        elif changed == 'thread': forged['message_thread_id'] += 1
        elif changed == 'mid': forged['message_id'] += 1
        elif changed == 'sender': forged['from']['id'] += 1
        elif changed == 'buttons': forged['reply_markup'] = {}
        else: forged['forward_origin'] = {'type': 'user'}
        await choose(env, row, 0, 'black', forged)
        assert not env.bot._blackwhite_service().players(row['nonce'])
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['unbound', 'bot', 'disabled', 'missing_emby', 'expired', 'rebound'])
def test_invalid_actor_never_participates(env, invalid):
    async def run():
        row = await create(env)
        if invalid == 'unbound': env.actors[1]['id'] = 88888
        elif invalid == 'bot': env.actors[1]['is_bot'] = True
        elif invalid == 'disabled': env.members.upsert(env.uids[1], 'private-login-0', {'status': 'suspended'})
        elif invalid == 'missing_emby': env.db.execute('UPDATE members SET emby_missing_since=? WHERE emby_user_id=?', (time.time(), env.uids[1]))
        elif invalid == 'expired': env.members.set_overrides(env.uids[1], {'expires_at_override': time.time()-1})
        else:
            await choose(env, row, 1, 'white')
            env.members.upsert('new-binding', 'never-public', {'group_id': 'standard'})
            env.members.bind_telegram('new-binding', str(env.actors[1]['id']))
            env.services.points.add('new-binding', 1000, 'isolated.fixture')
        count = len(env.bot._blackwhite_service().players(row['nonce']))
        await choose(env, row, 1, 'black')
        assert len(env.bot._blackwhite_service().players(row['nonce'])) == count
    asyncio.run(run())


def test_timeout_and_plugin_off_refund_without_revealing_choices(env, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    initial = total(env)
    async def run():
        row = await create(env)
        for i in range(3): await choose(env, row, i, 'black' if i == 0 else 'white')
        assert total(env) == initial
        clock[0] += 600
        await env.bot._blackwhite_tick()
        await env.bot._blackwhite_tick()
        current = env.bot._blackwhite_service().get(row['nonce'])
        assert current['state'] == 'expired'
        body = env.tg.text(GROUP, row['card_message_id'])
        assert '已过期' in body and '全额退回' in body
        assert '· 黑板 ·' not in body and '· 白板 ·' not in body
        row2 = await create(env, mid=602)
        await choose(env, row2, 0, 'black')
        env.services.registry.save('blackwhite', enabled=False)
        await env.bot._blackwhite_tick()
        assert env.bot._blackwhite_service().get(row2['nonce'])['state'] == 'cancelled'
    asyncio.run(run())
    assert total(env) == initial


def local_service(env, db):
    return BlackwhiteService(db, MemberService(db, GroupService(db)), PointsService(db),
        lambda: env.services.registry.config('blackwhite'), lambda: env.services.registry.enabled('blackwhite'),
        lambda chat: str(chat.get('id')) == str(GROUP), '123')


def test_sqlite_last_slot_competition_and_fair_odd_pool(env):
    async def prepare():
        row = await create(env, stake=11)
        for i in range(4): await choose(env, row, i, 'black' if i == 0 else 'white')
        return row
    row = asyncio.run(prepare())
    original = total(env)
    message = card(env, row)
    dbs = [Database(env.db.path) for _ in range(2)]
    def join(i):
        try:
            local_service(env, dbs[i]).join(row['nonce'], env.actors[4+i], message, 'black')
            return True
        except PlayError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(join, range(2))) == 1
    players = env.bot._blackwhite_service().players(row['nonce'])
    winners = [p for p in players if p['choice'] == 'black']
    assert len(players) == 5 and sorted(p['result_amount'] for p in winners) == [27, 28]
    assert total(env) == original
    for db in dbs: db.close()


def test_deadline_join_vs_timer_refunds_once(env):
    row = asyncio.run(create(env))
    asyncio.run(choose(env, row, 0, 'black'))
    before = env.services.points.balance(env.uids[0])
    dbs = [Database(env.db.path) for _ in range(2)]
    service0, service1 = [local_service(env, d) for d in dbs]
    def expire(): service0.expire(row['nonce'], now=row['expires_at'])
    def join():
        with pytest.raises(PlayError): service1.join(row['nonce'], env.actors[1], card(env, row), 'white', now=row['expires_at'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(expire), pool.submit(join)
        a.result(); b.result()
    assert env.services.points.balance(env.uids[0]) == before + 50
    assert len(env.bot._blackwhite_service().players(row['nonce'])) == 1
    assert env.bot._blackwhite_service().get(row['nonce'])['state'] == 'expired'
    for d in dbs: d.close()


def test_reopen_database_resumes_same_secret_and_expiration(env):
    row = asyncio.run(create(env))
    asyncio.run(choose(env, row, 0, 'white'))
    db = Database(env.db.path)
    service = local_service(env, db)
    assert service.get(row['nonce'])['secret'] == row['secret']
    service.expire_due(now=row['expires_at'])
    assert service.get(row['nonce'])['state'] == 'expired'
    service.expire_due(now=row['expires_at']+10)
    assert db.one('SELECT amount FROM play_funds WHERE kind=? AND ref=?', ('blackwhite', row['nonce']))['amount'] == 0
    db.close()


def test_original_card_edit_failure_retries_not_new_send(env):
    async def run():
        row = await create(env)
        env.tg.edit_error = 'ReadTimeout: 请求失败'
        await choose(env, row, 0, 'white')
        current = env.bot._blackwhite_service().get(row['nonce'])
        assert current['rendered_revision'] < current['revision'] and current['publish_error']
        env.tg.edit_error = ''
        env.db.execute('UPDATE play_rounds SET next_publish_at=0 WHERE nonce=?', (row['nonce'],))
        await env.bot._blackwhite_tick()
        current = env.bot._blackwhite_service().get(row['nonce'])
        assert current['rendered_revision'] == current['revision']
        assert len([1 for m, p in env.tg.calls if m == 'sendMessage']) == 1
    asyncio.run(run())


def test_slow_old_progress_never_overwrites_final_card(env, monkeypatch):
    async def run():
        row = await create(env)
        service = env.bot._blackwhite_service()
        original_call = env.bot._call
        started, resume = asyncio.Event(), asyncio.Event()
        async def slow(method, payload=None, **kw):
            if method == 'editMessageText' and '1/5' in payload.get('text', ''):
                started.set()
                await resume.wait()
            return await original_call(method, payload, **kw)
        monkeypatch.setattr(env.bot, '_call', slow)
        msg = card(env, row)
        service.join(row['nonce'], env.actors[0], msg, 'black')
        first = asyncio.create_task(env.bot._blackwhite_publish(row['nonce']))
        await started.wait()
        for i in range(1, 5): service.join(row['nonce'], env.actors[i], msg, 'white')
        final = asyncio.create_task(env.bot._blackwhite_publish(row['nonce']))
        resume.set()
        await asyncio.gather(first, final)
        assert '黑板胜' in env.tg.text(GROUP, row['card_message_id'])
        assert '1/5' not in env.tg.text(GROUP, row['card_message_id'])
        now = service.get(row['nonce'])
        assert now['rendered_revision'] == now['revision']
    asyncio.run(run())


def test_callback_recovers_only_original_unknown_send_and_no_resend(env):
    async def run():
        row = await create(env)
        original = card(env, row)
        env.db.execute("UPDATE play_rounds SET card_message_id=NULL,card_send_state='unknown',rendered_revision=-1 WHERE nonce=?", (row['nonce'],))
        await env.bot._blackwhite_publish(row['nonce'])
        assert len([1 for m, _ in env.tg.calls if m == 'sendMessage']) == 1
        await choose(env, row, 0, 'white', original)
        assert env.bot._blackwhite_service().get(row['nonce'])['card_message_id'] == row['card_message_id']
    asyncio.run(run())


def test_fifth_join_settlement_fault_rolls_back_every_money_effect(env, monkeypatch):
    row = asyncio.run(create(env))
    for i in range(4): asyncio.run(choose(env, row, i, 'black' if i < 2 else 'white'))
    service = env.bot._blackwhite_service()
    before = copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
    original_payout = CashBook.payout
    def fail_after_credit(self, *args, **kwargs):
        original_payout(self, *args, **kwargs)
        raise RuntimeError('injected after payout ledger write')
    monkeypatch.setattr(CashBook, 'payout', fail_after_credit)
    with pytest.raises(RuntimeError): service.join(row['nonce'], env.actors[4], card(env, row), 'white')
    assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
    assert len(service.players(row['nonce'])) == 4
    assert service.get(row['nonce'])['state'] == 'lobby'
    assert env.db.one('SELECT amount FROM play_funds WHERE kind=? AND ref=?', ('blackwhite', row['nonce']))['amount'] == 200
    monkeypatch.setattr(CashBook, 'payout', original_payout)
    service.join(row['nonce'], env.actors[4], card(env, row), 'white')
    assert service.get(row['nonce'])['state'] == 'settled'


def test_cleanup_failure_does_not_stop_game_timeout_refunds(env, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    async def run():
        row = await create(env)
        await choose(env, row, 0, 'white')
        clock[0] += 600
        async def cleanup_error(): raise RuntimeError('isolated deletion worker fault')
        async def one_tick(seconds): raise asyncio.CancelledError
        monkeypatch.setattr(env.bot, '_drain_checkin_deletes', cleanup_error)
        monkeypatch.setattr(asyncio, 'sleep', one_tick)
        with pytest.raises(asyncio.CancelledError): await env.bot._play_worker()
        assert env.bot._blackwhite_service().get(row['nonce'])['state'] == 'expired'
        assert env.db.one('SELECT amount FROM play_funds WHERE kind=? AND ref=?', ('blackwhite', row['nonce']))['amount'] == 0
    asyncio.run(run())


def test_creation_idempotent_config_snapshot_and_help_not_in_main_card(env):
    async def run():
        row = await create(env)
        again = await create(env)
        assert row['nonce'] == again['nonce']
        env.services.registry.save('blackwhite', config={'default_stake': 10, 'max_stake': 20})
        await choose(env, row, 0, 'black')
        assert env.services.points.ledger(env.uids[0])[0]['delta'] == -50
        current = env.bot._blackwhite_service().get(row['nonce'])
        assert json.loads(current['config_json'])['max_stake'] == 500
        main = env.tg.text(GROUP, row['card_message_id'])
        for word in ('不可更改', '管理员', '少数方', '秘密选择', '手续费'): assert word not in main
        await env.bot._dispatch_update({'callback_query': {'id': 'help', 'data': 'bwh:'+row['nonce'], 'message': card(env, row), 'from': env.actors[0]}})
        assert any('少数方' in p.get('text', '') for m, p in env.tg.calls if m == 'answerCallbackQuery')
        with pytest.raises(ValueError): env.services.registry.save('blackwhite', config={'default_stake': 501})
    asyncio.run(run())
