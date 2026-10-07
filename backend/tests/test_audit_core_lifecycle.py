"""ASGI shutdown must join writers before closing the operational database."""
from fastapi.testclient import TestClient

from app.main import app


def test_shutdown_joins_background_writers_and_closes_database(monkeypatch):
    closed = []
    with TestClient(app):
        usage = app.state.usage_task
        probe = app.state.probe_task
        prime = app.state.intake_prime_task
        bot = app.state.telegram
        plugins = app.state.plugins
        db = app.state.db
        real_close = db.close

        def close():
            assert usage.done() and probe.done() and prime.done()
            assert bot._task is None
            assert plugins._task is None
            closed.append(True)
            real_close()

        monkeypatch.setattr(db, "close", close)
    assert closed == [True]


def test_probe_wake_completion_never_swallows_shutdown_cancellation():
    import asyncio
    from types import SimpleNamespace

    from app.modules.scheduler import Scheduler

    async def run():
        state = SimpleNamespace(_wake=asyncio.Event())
        state._wake.set()
        task = asyncio.create_task(Scheduler.wait_for_change(state, 10))
        # Real wait_for's inner Event.wait completes before the outer task
        # resumes. This ordering loses cancellation on Python 3.11.2.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # timeout() need not suspend on an already-completed wake: completing
        # before cancel is equally valid. An outstanding cancellation may
        # never be swallowed by a still-running probe loop.
        assert task.cancelled() or task.cancelling() == 0

    asyncio.run(run())


def test_probe_wait_retains_normal_wake_timeout_and_external_cancel():
    import asyncio
    from types import SimpleNamespace

    from app.modules.scheduler import Scheduler

    async def run():
        state = SimpleNamespace(_wake=asyncio.Event())
        state._wake.set()
        await Scheduler.wait_for_change(state, 1)
        assert not state._wake.is_set()
        await Scheduler.wait_for_change(state, 0.001)
        assert not state._wake.is_set()
        task = asyncio.create_task(Scheduler.wait_for_change(state, 10))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()

    asyncio.run(run())
