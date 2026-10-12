"""Per-round virtual timer; Telegram is simulated, no wall-clock waiting or network."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from test_dice import create, message, svc
from test_dice_banker import bw_env, economy_env, interaction_env  # noqa: F401
from test_dice_banker import env as banker_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.modules import bot_dice, bot_dice_group, dice, dice_caption_view
from app.modules.dice_caption_view import pages
from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def env(request):
    return request.getfixturevalue('banker_env')


class Timer:
    def __init__(self, monkeypatch):
        self.clock = time.time()
        self.waiters = asyncio.Queue()
        clock = SimpleNamespace(time=lambda: self.clock)
        monkeypatch.setattr(dice, 'time', clock)
        monkeypatch.setattr(dice_caption_view, 'time', clock)
        monkeypatch.setattr(bot_dice_group, 'time', clock)
        monkeypatch.setattr(bot_dice, 'time', clock)
        monkeypatch.setattr(bot_dice_group, 'asyncio', SimpleNamespace(create_task=asyncio.create_task, sleep=self.sleep))

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        await self.waiters.put((delay, future))
        await future

    async def step(self, extra=0):
        delay, future = await asyncio.wait_for(self.waiters.get(), 1)
        while future.done():
            delay, future = await asyncio.wait_for(self.waiters.get(), 1)
        self.clock += delay+extra
        future.set_result(None)
        for _ in range(4):
            await asyncio.sleep(0)
        return delay


def edits(env):
    return [p for method, p in env.tg.calls if method == 'editMessageCaption']


def test_silent_round_ticks_deadline_then_stops_without_sending(env, monkeypatch):
    async def run():
        timer = Timer(monkeypatch)
        row = await create(env)
        task = env.bot._dice_group_tasks[row['nonce']]
        assert '剩余 01:30' in env.tg.text(GROUP, row['card_mid'])
        for _ in range(45):
            await timer.step()
            if task.done():
                break
        assert task.done()
        assert svc(env).get(row['nonce'])['state'] == 'locked'
        assert edits(env)[0]['caption'].find('剩余 01:28') >= 0
        assert '已封盘 · 等待开奖' in edits(env)[-1]['caption']
        assert '剩余 00:00' in edits(env)[-1]['caption']
        assert all(p['message_id'] == row['card_mid'] for p in edits(env))
        assert len(edits(env)) == 45
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 1
        assert not [m for m, _ in env.tg.calls if m == 'sendMessage']
        assert not env.bot._dice_group_tasks
    asyncio.run(run())


def test_worker_recovers_timer_without_dirty_revision_and_keeps_page(env, monkeypatch):
    async def run():
        timer = Timer(monkeypatch)
        row = await create(env)
        for i in range(19):
            svc(env).group_bet(message(env, '/大 1', index=1, mid=8100+i), '大', 1)
        env.db.execute('UPDATE dice_rounds SET group_page=1,group_due=0')
        task = env.bot._dice_group_tasks[row['nonce']]
        for _ in range(10):
            env.bot._dice_group_schedule(row['nonce'])
        assert env.bot._dice_group_tasks[row['nonce']] is task
        await timer.step()
        body = edits(env)[-1]['caption']
        assert '#11</code>' in body and '#19</code>' not in body
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not env.bot._dice_group_tasks
        await env.bot._dice_tick()
        restored = env.bot._dice_group_tasks[row['nonce']]
        assert restored is not task
        await timer.step()
        assert '#11</code>' in edits(env)[-1]['caption']
        assert svc(env).get(row['nonce'])['group_page'] == 1
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 1
        # Terminal rounds cannot retain a periodic timer.
        env.db.execute("UPDATE dice_rounds SET state='empty'")
        await timer.step()
        assert restored.done()
    asyncio.run(run())


def test_retry_after_and_network_delay_are_not_sleep_based_countdown(env, monkeypatch):
    async def run():
        timer = Timer(monkeypatch)
        row = await create(env)
        real = env.bot._call
        attempts = []
        async def limited(method, payload=None, **kw):
            if method == 'editMessageCaption':
                attempts.append((timer.clock, payload['caption']))
                if len(attempts) == 1:
                    CALL_DELIVERY.set({'state': 'retry', 'retry_after': 30})
                    return None
            return await real(method, payload, **kw)
        env.bot._call = limited
        await timer.step()
        for _ in range(5):
            await env.bot._dice_group_refresh(row['nonce'])
            env.bot._dice_group_schedule(row['nonce'])
        assert len(attempts) == 1
        assert await timer.step(extra=4) >= 30
        assert len(attempts) == 2 and attempts[1][0]-attempts[0][0] >= 34
        assert '剩余 00:54' in attempts[1][1]  # actual deadline, not 2 seconds subtracted
        task = env.bot._dice_group_tasks[row['nonce']]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_timer_respects_retry_at_deadline_then_finishes_closed_edit(env, monkeypatch):
    async def run():
        timer = Timer(monkeypatch)
        env.services.registry.save('dice', config={'window_seconds': 30})
        row = await create(env)
        real = env.bot._call
        count = 0
        async def limited(method, payload=None, **kw):
            nonlocal count
            if method == 'editMessageCaption':
                count += 1
                if count == 1:
                    CALL_DELIVERY.set({'state': 'retry', 'retry_after': 60})
                    return None
            return await real(method, payload, **kw)
        env.bot._call = limited
        await timer.step()
        await timer.step()  # wake at deadline to close, but do not violate retry_after
        assert svc(env).get(row['nonce'])['state'] == 'locked' and count == 1
        task = env.bot._dice_group_tasks[row['nonce']]
        await timer.step()
        assert count == 2 and task.done()
        assert '已封盘' in edits(env)[-1]['caption']
    asyncio.run(run())


def test_countdown_fixed_width_keeps_caption_pages_stable(env):
    async def run():
        row = await create(env)
        for i in range(19):
            svc(env).group_bet(message(env, '/大 1', index=1, mid=8100+i), '大', 1)
        row, snapshot = svc(env).group_snapshot(row['nonce'])
        start = pages(row, snapshot, now=row['created'])
        end = pages(row, snapshot, now=row['deadline']-0.01)
        assert len(start) == len(end)
        for before, after in zip(start, end, strict=True):
            assert before.replace('01:30', '00:01') == after
        assert json.loads(row['rules'])['window'] == 90
    asyncio.run(run())


def test_pending_open_retry_renders_fresh_caption_not_persisted_countdown(env, monkeypatch):
    async def run():
        timer = Timer(monkeypatch)
        real = env.bot._call_multipart
        captions = []
        async def limited(method, payload, files, **kw):
            captions.append(payload['caption'])
            if len(captions) == 1:
                CALL_DELIVERY.set({'state': 'retry', 'retry_after': 10})
                return None
            return await real(method, payload, files, **kw)
        env.bot._call_multipart = limited
        row = await create(env)
        assert row['card_mid'] is None
        timer.clock += 10
        await env.bot._dice_deliver(row['nonce'])
        assert '剩余 01:30' in captions[0] and '剩余 01:20' in captions[1]
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 1
        assert svc(env).get(row['nonce'])['card_mid']
    asyncio.run(run())
