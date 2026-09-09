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
