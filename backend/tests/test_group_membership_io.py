"""Slow SQLite work must not stall the Bot/ASGI loop or outlive its owner."""
import asyncio
import threading
import time

import httpx
import pytest
from test_group_membership import CHANNEL, GROUP, VIEWER, leave, scan
from test_group_membership import env as membership_env  # noqa: F401
from test_tg_interaction_context import env as context_env  # noqa: F401

from app.main import app


@pytest.fixture
def env(request):
    return request.getfixturevalue('membership_env')


class Slow:
    def __init__(self, original, predicate):
        self.original, self.predicate = original, predicate
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread = None

    def __call__(self, *args, **kwargs):
        if self.thread is None and self.predicate(*args, **kwargs):
            self.thread = threading.get_ident()
            self.entered.set()
            self.release.wait(2)
        return self.original(*args, **kwargs)


async def assert_responsive(operation, slow):
    loop_thread = threading.get_ident()
    start = time.monotonic()
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(slow.entered.wait, 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            assert (await client.get('/healthz')).status_code == 200
        assert slow.thread != loop_thread, 'SQLite work ran on the ASGI/Bot loop'
        assert not task.done(), 'health only responded after the blocking work ended'
        assert time.monotonic() - start < 1.5
    finally:
        slow.release.set()
        await task


@pytest.mark.parametrize('stage', ['scan_meta', 'event_meta_read', 'event_meta_write', 'authority', 'local_delete'])
def test_slow_membership_sql_does_not_block_health(env, monkeypatch, stage):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    if stage in ('event_meta_read', 'authority'):
        method = 'one'
        predicate = (lambda sql, params=(): 'event:' in str(params)) if stage == 'event_meta_read' else (
            lambda sql, params=(): 'SELECT tg_user_id,tg_bound_at' in sql)
    else:
        method = 'execute'
        predicate = {'scan_meta': lambda sql, params=(): 'INSERT INTO meta' in sql and ':latest' in str(params),
                     'event_meta_write': lambda sql, params=(): 'INSERT INTO meta' in sql and 'event:' in str(params),
                     'local_delete': lambda sql, params=(): 'DELETE FROM members' in sql}[stage]
    slow = Slow(getattr(env.db, method), predicate)
    monkeypatch.setattr(env.db, method, slow)
    operation = env.bot.membership.handle_update(leave()) if stage.startswith('event') else scan(env)
    asyncio.run(assert_responsive(operation, slow))


def test_shared_db_lock_wait_does_not_stall_health(env):
    held, release = threading.Event(), threading.Event()
    def holder():
        with env.db._lock:
            held.set()
            release.wait(2)
    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(1)
    async def run():
        start = time.monotonic()
        task = asyncio.create_task(scan(env))
        try:
            await asyncio.sleep(.02)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
                assert (await client.get('/healthz')).status_code == 200
            assert time.monotonic() - start < .5
            assert not task.done()
        finally:
            release.set()
            await task
    try:
        asyncio.run(run())
    finally:
        release.set()
        thread.join(3)


def test_cancel_waits_for_writer_and_prevents_overlapping_scan(env, monkeypatch):
    slow = Slow(env.db.execute, lambda sql, params=(): 'INSERT INTO meta' in sql and ':latest' in str(params))
    monkeypatch.setattr(env.db, 'execute', slow)
    async def run():
        first = env.bot.membership.start_scan()
        assert await asyncio.to_thread(slow.entered.wait, 3)
        stopping = asyncio.create_task(env.bot.stop())
        try:
            await asyncio.sleep(.03)
            assert not stopping.done() and not env.bot.membership._scan_task.done()
            assert env.bot.membership.start_scan()['id'] == first['id']
        finally:
            slow.release.set()
            await stopping
        saved = env.bot.membership._load('latest')
        assert not saved['running'] and saved['cancelled']
    asyncio.run(run())


def test_new_join_invalidates_before_slow_persistence_and_final_disk_order_is_newest(env, monkeypatch):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    slow = Slow(env.db.execute, lambda sql, params=(): 'INSERT INTO meta' in sql and 'event:' in str(params))
    monkeypatch.setattr(env.db, 'execute', slow)
    async def run():
        leaving = asyncio.create_task(env.bot.membership.handle_update(leave(update_id=30)))
        assert await asyncio.to_thread(slow.entered.wait, 3)
        previous = env.bot.membership._member_epochs.get(str(VIEWER), 0)
        joined = leave(status='member', update_id=31)
        joining = asyncio.create_task(env.bot.membership.handle_update(joined))
        try:
            await asyncio.sleep(.03)
            assert env.bot.membership._member_epochs[str(VIEWER)] > previous
            assert not env.deleted
        finally:
            slow.release.set()
            await asyncio.gather(leaving, joining)
        assert env.bot.membership._load(f'event:{GROUP}:{VIEWER}')[1:] == [31, 'present']
        assert not env.deleted
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_final_db_wait_cannot_hide_new_join_or_cancel(env, monkeypatch, cancel):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    service = env.bot.membership
    original = service._current
    calls = 0
    def final_check(*args):
        nonlocal calls
        calls += 1
        return calls == 3  # precheck, execute_delete authorize, post-query final check
    slow = Slow(original, final_check)
    monkeypatch.setattr(service, '_current', slow)
    async def run():
        task = asyncio.create_task(service.inspect(env.members.get('u1'), source='manual', fingerprint=service.fingerprint()))
        assert await asyncio.to_thread(slow.entered.wait, 3)
        try:
            if cancel:
                task.cancel()
                await asyncio.sleep(.01)
                task.cancel()  # repeated shutdown still owns the worker
                await asyncio.sleep(.01)
                assert not task.done()
            else:
                await service.handle_update(leave(status='member', update_id=91))
            assert not env.deleted
        finally:
            slow.release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)['action'] == 'cancelled'
        assert not env.deleted and env.members.get('u1')
    asyncio.run(run())


def test_cancel_during_event_read_still_persists_accepted_marker(env, monkeypatch):
    slow = Slow(env.db.one, lambda sql, params=(): 'event:' in str(params))
    monkeypatch.setattr(env.db, 'one', slow)
    async def run():
        task = asyncio.create_task(env.bot.membership.handle_update(leave(status='member', update_id=81)))
        assert await asyncio.to_thread(slow.entered.wait, 3)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        slow.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.bot.membership._load(f'event:{GROUP}:{VIEWER}')[1:] == [81, 'present']
        assert not env.deleted
    asyncio.run(run())


def test_stop_during_local_delete_drains_before_owner_finishes(env, monkeypatch):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    slow = Slow(env.db.execute, lambda sql, params=(): 'DELETE FROM members' in sql)
    monkeypatch.setattr(env.db, 'execute', slow)
    async def run():
        env.bot.membership.start_scan()
        assert await asyncio.to_thread(slow.entered.wait, 3)
        stopping = asyncio.create_task(env.bot.stop())
        try:
            await asyncio.sleep(.02)
            assert not stopping.done() and env.deleted == ['u1']
        finally:
            slow.release.set()
            await stopping
        assert not env.members.get('u1')
        assert not env.bot.membership._load('latest')['running']
        assert env.db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action='member.delete'")['n'] == 1
    asyncio.run(run())


def test_failed_initial_meta_write_never_proceeds_to_delete(env, monkeypatch):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    original = env.db.execute
    def failing(sql, params=()):
        if 'INSERT INTO meta' in sql:
            raise OSError('local simulated disk failure')
        return original(sql, params)
    monkeypatch.setattr(env.db, 'execute', failing)
    async def run():
        env.bot.membership.start_scan()
        with pytest.raises(OSError):
            await env.bot.membership._scan_task
        assert not env.deleted
        assert env.bot.membership.status()['error'] == 'OSError'
    asyncio.run(run())
