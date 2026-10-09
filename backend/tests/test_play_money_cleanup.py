"""Real ledger/transactions and received check-in handlers. No live business writes."""
import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from test_checkin_interaction import economy_env, interaction_env, message  # noqa: F401
from test_economy import proof
from test_tg_interaction_context import GROUP, VIEWER, click, command

from app.core.db import Database
from app.modules.checkin_cleanup import CleanupService
from app.modules.economy_rules import economy_write
from app.modules.play_money import CashBook
from app.modules.points import PointsService
from app.modules.telegram import TelegramBot


@pytest.fixture
def env(request):
    e = request.getfixturevalue('economy_env')
    e.services.registry.save('checkin_cleanup', enabled=True)
    e.services.registry.get('checkin_cleanup').ctx.telegram = e.bot
    return e


def states(db):
    return db.query("SELECT * FROM play_jobs WHERE kind='checkin.delete' ORDER BY id")


@pytest.mark.parametrize('outcome', ['success', 'repeat', 'progress', 'unbound', 'exception', 'disabled'])
def test_actual_group_command_all_feedback_clean_at_60_not_before(env, monkeypatch, outcome):
    clock = [time.time() + 0.375]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    if outcome in ('success', 'repeat'):
        proof(env.db, 'u1', now=clock[0])
    if outcome == 'repeat':
        asyncio.run(env.bot._dispatch_update({'message': message(chat=VIEWER)}))
    if outcome == 'exception':
        def fail(*args): raise ValueError('隔离失败')
        monkeypatch.setattr(env.services.checkin, 'checkin', fail)
    if outcome == 'disabled':
        env.services.registry.save('checkin', enabled=False)
    inbound = message(chat=GROUP, thread=31, user=999 if outcome == 'unbound' else VIEWER)
    asyncio.run(env.bot._dispatch_update({'message': inbound}))
    jobs = states(env.db)
    assert len(jobs) == 2 and {json.loads(j['payload_json'])['source'] for j in jobs} == {'command', 'feedback'}
    for j in jobs:
        p = json.loads(j['payload_json'])
        assert p['chat_id'] == GROUP and p['thread_id'] == 31
        assert j['due_at'] == clock[0] + 60
    old_ledger = copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
    env.tg.calls.clear()
    assert asyncio.run(env.bot._drain_checkin_deletes(now=clock[0] + 59.999)) == 0
    assert not env.tg.calls
    assert asyncio.run(env.bot._drain_checkin_deletes(now=clock[0] + 60)) == 2
    assert all(j['state'] == 'deleted' for j in states(env.db))
    deleted = [p for m, p in env.tg.calls if m == 'deleteMessage']
    assert len(deleted) == 2
    assert {p['message_id'] for p in deleted} == {json.loads(j['payload_json'])['message_id'] for j in jobs}
    assert all(p['chat_id'] == GROUP for p in deleted)
    assert asyncio.run(env.bot._drain_checkin_deletes(now=clock[0] + 90)) == 0
    assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == old_ledger


def test_private_and_unrelated_group_messages_never_queued(env):
    asyncio.run(env.bot._dispatch_update({'message': message(chat=VIEWER)}))
    asyncio.run(env.bot._dispatch_update({'message': message('/me', chat=GROUP)}))
    assert states(env.db) == []


def test_group_callback_only_feedback_not_original_menu(env):
    async def run():
        mid = await command(env, '/me', chat=GROUP, user=VIEWER, thread=24)
        before = copy.deepcopy(env.tg.message(GROUP, mid))
        await click(env, 'checkin', mid, chat=GROUP, user=VIEWER, thread=24)
        jobs = states(env.db)
        assert len(jobs) == 1
        assert json.loads(jobs[0]['payload_json'])['message_id'] != mid
        assert json.loads(jobs[0]['payload_json'])['source'] == 'feedback'
        await env.bot._drain_checkin_deletes(now=jobs[0]['due_at'])
        assert env.tg.message(GROUP, mid) == before
    asyncio.run(run())


def test_membership_gate_failure_feedback_is_also_cleaned(env, monkeypatch):
    async def denied(chat, tg, **kw):
        await env.bot.send_message(chat, '🔒 请先加入群组', reply_to_message_id=kw.get('reply_to_message_id'))
        return False
    monkeypatch.setattr(env.bot.membership, 'gate', denied)
    asyncio.run(env.bot._dispatch_update({'message': message(chat=GROUP, thread=42)}))
    assert len(states(env.db)) == 2
    assert env.db.one('SELECT COUNT(*) n FROM checkins')['n'] == 0


@pytest.mark.parametrize('description,code,expected', [
    ('Bad Request: message to delete not found', 400, 'gone'),
    ('Forbidden: bot is not an administrator', 403, 'blocked'),
    ('Bad Request: message can\'t be deleted', 400, 'blocked'),
    ('Too Many Requests', 429, 'queued'),
    ('Internal server error', 500, 'queued'),
])
def test_native_delete_transport_failure_never_claims_success(env, monkeypatch, description, code, expected):
    service = CleanupService(env.db)
    service.enqueue('123', GROUP, 3, 721, 'command', VIEWER, now=time.time() - 61)
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            code, json={'ok': False, 'error_code': code, 'description': description, 'parameters': {'retry_after': 70}})))
        async def native_client(): return client
        monkeypatch.setattr(env.bot, '_client', native_client)
        async def native(method, payload=None, timeout=20):
            return await TelegramBot._call(env.bot, method, payload, timeout)
        monkeypatch.setattr(env.bot, '_call', native)
        await env.bot._drain_checkin_deletes()
        await client.aclose()
    attempt_at = time.time()
    asyncio.run(run())
    job = states(env.db)[0]
    assert job['state'] == expected and job['state'] != 'deleted'
    last = env.services.registry.last_result('checkin_cleanup')
    assert last is not None
    if expected == 'blocked':
        assert service.summary()['权限或归属待处理'] == 1 and not last['ok']
    if code == 429:
        assert job['due_at'] >= attempt_at + 70


def test_permission_fixed_manual_plugin_retry_exact_target(env, monkeypatch):
    service = CleanupService(env.db)
    j = service.enqueue('123', GROUP, 0, 721, 'command', VIEWER, now=time.time() - 61)
    claimed = service.claim('123')
    service.finish(claimed, 'blocked', '权限不足未删')
    assert asyncio.run(env.bot._drain_checkin_deletes()) == 0
    result = asyncio.run(env.services.registry.get('checkin_cleanup').run({}))
    assert result['已删除'] == 1 and states(env.db)[0]['id'] == j['id']
    assert any(m == 'deleteMessage' and p == {'chat_id': GROUP, 'message_id': 721} for m, p in env.tg.calls)


def test_queue_reopen_expired_lease_idempotence_and_bot_ownership(tmp_path):
    path = tmp_path / 'jobs.db'
    db = Database(path)
    s = CleanupService(db)
    j = s.enqueue('123', GROUP, 15, 88, 'command', VIEWER, now=100.5)
    assert s.enqueue('123', GROUP, 15, 88, 'command', VIEWER, now=120)['id'] == j['id']
    assert s.claim('123', now=160.499) is None
    first = s.claim('123', now=160.5)
    db.close()
    db = Database(path)
    s = CleanupService(db)
    assert s.claim('123', now=180) is None
    second = s.claim('123', now=205.5)
    assert second['id'] == first['id'] and second['lease_token'] != first['lease_token']
    s.finish(first, 'deleted', now=206)  # stale worker must not replace the newer lease
    assert states(db)[0]['state'] == 'running'
    s.finish(second, 'deleted', now=206)
    assert s.claim('123', now=210) is None
    s.enqueue('999', GROUP, 15, 89, 'feedback', VIEWER, now=100)
    assert s.claim('123', now=300)['skip']
    assert states(db)[1]['state'] == 'blocked'
    db.close()


def test_new_bot_instance_resumes_persisted_due_targets(env):
    service = CleanupService(env.db)
    clock = time.time()
    service.enqueue('123', GROUP, 22, 981, 'feedback', VIEWER, now=clock - 61)
    old_ledger = env.db.query('SELECT * FROM points_ledger ORDER BY id')
    path = env.db.path
    env.db.close()
    reopened = Database(path)
    bot = TelegramBot(lambda: env.cfg, env.members, db=reopened, points=PointsService(reopened))
    bot._call = env.tg.call
    assert asyncio.run(bot._drain_checkin_deletes()) == 1
    assert states(reopened)[0]['state'] == 'deleted'
    assert reopened.query('SELECT * FROM points_ledger ORDER BY id') == old_ledger
    assert any(m == 'deleteMessage' and p == {'chat_id': GROUP, 'message_id': 981} for m, p in env.tg.calls)
    reopened.close()


def test_cleanup_config_60_and_unsafe_or_different_targets_refused(env):
    with pytest.raises(ValueError): env.services.registry.save('checkin_cleanup', config={'delay_seconds': 59})
    s = CleanupService(env.db)
    for chat, mid in ((VIEWER, 1), (GROUP, 0), (GROUP, True)):
        with pytest.raises(ValueError): s.enqueue('123', chat, 0, mid, 'command', VIEWER)
    s.enqueue('123', GROUP, 1, 500, 'command', VIEWER)
    with pytest.raises(ValueError): s.enqueue('123', GROUP, 2, 500, 'command', VIEWER)


def test_cash_atomic_escrow_pool_sink_and_ledger_conservation(tmp_path):
    db = Database(tmp_path / 'cash.db')
    points = PointsService(db)
    cash = CashBook(points)
    points.add('u', 1000, 'isolated.fixture')
    with economy_write(db) as tx:
        assert cash.reserve(tx, 'poker', 'g', 'u', 500)
        assert not cash.reserve(tx, 'poker', 'g', 'u', 500)
        cash.consume(tx, 'poker', 'g', 'u', 100, 'poker', 'g')
        cash.payout(tx, 'poker', 'g', 'v', 100)
        cash.release(tx, 'poker', 'g', 'u')
    assert points.balance('u') == 900 and points.balance('v') == 100
    with economy_write(db) as tx:
        cash.reserve(tx, 'poker', 'o', 'u', 105)
        cash.consume(tx, 'poker', 'o', 'u', 5, 'fee', 'o')
        assert cash.release(tx, 'poker', 'o', 'u') == 100
        assert cash.release(tx, 'poker', 'o', 'u') == 0
    total = sum(points.balances().values()) + db.one('SELECT SUM(amount) n FROM play_escrows')['n'] + db.one('SELECT SUM(amount) n FROM play_funds')['n']
    assert total == 1000
    with pytest.raises(RuntimeError), economy_write(db) as tx: cash.payout(tx, 'fee', 'o', 'u', 5)
    before = points.ledger('u')
    with pytest.raises(ValueError), economy_write(db) as tx:
        cash.reserve(tx, 'poker', 'bad', 'u', 500)
        raise ValueError('injected failure after reserve')
    assert points.ledger('u') == before
    assert not db.one("SELECT * FROM play_escrows WHERE ref='bad'")
    db.close()


def test_independent_sqlite_connections_cannot_overreserve(tmp_path):
    path = tmp_path / 'concurrent.db'
    db = Database(path)
    PointsService(db).add('u', 100, 'isolated.fixture')
    db.close()
    dbs = [Database(path) for _ in range(8)]
    def attempt(i):
        local = dbs[i]
        try:
            with economy_write(local) as tx:
                CashBook(PointsService(local)).reserve(tx, 'poker', str(i), 'u', 100)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    assert PointsService(dbs[0]).balance('u') == 0
    assert dbs[0].one('SELECT SUM(amount) n FROM play_escrows')['n'] == 100
    for d in dbs: d.close()


def test_additive_migration_repeat_keeps_existing_columns(tmp_path):
    path = tmp_path / 'preserve.db'
    db = Database(path)
    points = PointsService(db)
    points.add('original', 123, 'isolated.fixture')
    before = db.query('SELECT * FROM points_ledger')
    db.close()
    db = Database(path)
    assert db.query('SELECT * FROM points_ledger') == before
    assert db.one('SELECT COUNT(*) n FROM play_jobs')['n'] == 0
    assert db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 0
    db.close()
