"""Bounded, coalesced transport work for packet/niuniu cards, not money work."""
from __future__ import annotations

import asyncio

MAX_PENDING = 256
MAX_WORKERS = 2


def _state(bot):
    loop = asyncio.get_running_loop()
    state = getattr(bot, '_game_ui', None)
    if state is None or state['loop'] is not loop:
        state = {'loop': loop, 'pending': {}, 'busy': set(), 'workers': set(),
                 'render_slots': asyncio.Semaphore(2), 'menu_locks': {}}
        bot._game_ui = state
    return state


def defer_ui(bot, key, work):
    state = _state(bot)
    state['workers'].difference_update([task for task in state['workers'] if task.done()])
    if key not in state['pending'] and len(state['pending']) >= MAX_PENDING:
        return False  # Committed dirty versions/outboxes remain for recovery ticks.
    state['pending'][key] = work
    target = min(MAX_WORKERS, len(state['pending']))
    while len(state['workers']) < target:
        task = asyncio.create_task(_worker(bot, state))
        state['workers'].add(task)
        bot._in_flight.add(task)
        task.add_done_callback(state['workers'].discard)
        task.add_done_callback(bot._in_flight.discard)
    return True


async def _worker(bot, state):
    while True:
        # Card work takes priority over ancillary command-menu synchronization.
        keys = sorted(state['pending'], key=lambda k: k[0] == 'commands')
        key = next((k for k in keys if k not in state['busy']), None)
        if key is None:return
        work = state['pending'].pop(key)
        state['busy'].add(key)
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - durable versions/outboxes stay retryable
            bot._last_error = '游戏卡片同步待重试'
        finally:
            state['busy'].discard(key)


async def drain_ui(bot):
    """Await only this bounded work, for deterministic isolated handler tests."""
    state = _state(bot)
    while state['workers']:
        await asyncio.gather(*list(state['workers']))
        await asyncio.sleep(0)


async def render_image(bot, renderer, *args):
    async with _state(bot)['render_slots']:
        task = asyncio.create_task(asyncio.to_thread(renderer, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not release a slot while its non-cancellable thread still runs.
            await task
            raise


async def sync_game_commands(bot, chat, user, language, group):
    state = _state(bot)
    async with state['menu_locks'].setdefault(str(chat), asyncio.Lock()):
        # The existing method rechecks current authorization and cache state.
        await bot._sync_chat_commands(chat, user, language, group=group)
