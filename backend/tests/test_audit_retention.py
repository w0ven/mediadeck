"""device.* audit rows expire after 90 days; every other action is kept."""
import asyncio
import logging

from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules import audit_retention

NOW = 1_800_000_000
DAY = 86400


def _seed(db):
    rows = [
        ("device.refused", 100), ("device.kick", 91), ("device.auto_logout", 200),
        ("device.remove", 95), ("device.over-limit-registered", 365),
        ("device.refused", 89), ("device.kick", 10), ("device.remove", 0),
        # Never deleted, however old: account history and look-alikes.
        ("points.group.mint", 400), ("points.packet.user.claim", 400), ("shop.redeem", 400),
        ("member.create", 400), ("member.invite_quota", 400), ("registration.gift", 400),
        ("whitelist.card.expire", 400), ("telegram.prouser", 400),
        ("DEVICE.refused", 400), ("devices.kick", 400), ("device_kick", 400), ("xdevice.kick", 400),
    ]
    for action, age in rows:
        db.execute("INSERT INTO audit_log (ts, actor, action, subject) VALUES (?,?,?,?)",
                   (NOW - age * DAY, "system", action, "s"))


def _actions(db):
    return sorted((r["action"], NOW - r["ts"]) for r in db.query("SELECT action, ts FROM audit_log"))


def test_only_old_device_rows_are_deleted(tmp_path):
    db = Database(tmp_path / "t.db")
    _seed(db)
    deleted = asyncio.run(audit_retention.purge_expired(db, now=NOW, batch=2, pause=0))
    assert deleted == 5
    left = _actions(db)
    assert ("device.refused", 89 * DAY) in left and ("device.kick", 10 * DAY) in left
    assert ("device.remove", 0) in left
    assert not [a for a, age in left if a.startswith("device.") and age > 90 * DAY]
    for kept in ("points.group.mint", "points.packet.user.claim", "shop.redeem", "member.create",
                 "member.invite_quota", "registration.gift", "whitelist.card.expire",
                 "telegram.prouser", "DEVICE.refused", "devices.kick", "device_kick", "xdevice.kick"):
        assert (kept, 400 * DAY) in left
    assert len(left) == 20 - 5
    db.close()


def test_deletes_in_bounded_batches(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    for i in range(25):
        db.execute("INSERT INTO audit_log (ts, action) VALUES (?, 'device.refused')", (NOW - 200 * DAY + i,))
    sizes = []
    real = audit_retention.purge_batch

    def spy(db_, cutoff, batch):
        n = real(db_, cutoff, batch)
        sizes.append(n)
        return n

    monkeypatch.setattr(audit_retention, "purge_batch", spy)
    assert asyncio.run(audit_retention.purge_expired(db, now=NOW, batch=10, pause=0)) == 25
    assert sizes == [10, 10, 5]  # each write transaction removes at most one batch
    assert db.one("SELECT COUNT(*) AS n FROM audit_log")["n"] == 0
    db.close()


def test_loop_runs_and_logs_each_cycle(tmp_path, caplog):
    db = Database(tmp_path / "t.db")
    db.execute("INSERT INTO audit_log (ts, action) VALUES (?, 'device.kick')", (1,))
    db.execute("INSERT INTO audit_log (ts, action) VALUES (?, 'member.create')", (1,))

    async def run():
        task = asyncio.create_task(audit_retention.retention_loop(db, first_delay=0, interval=3600))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if db.one("SELECT COUNT(*) AS n FROM audit_log")["n"] == 1:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(run())
    assert [r["action"] for r in db.query("SELECT action FROM audit_log")] == ["member.create"]
    assert any("audit retention: deleted 1 device.* rows" in r.getMessage() for r in caplog.records)
    db.close()


def test_app_starts_and_stops_the_retention_task():
    with TestClient(app):
        task = app.state.audit_retention_task
        assert not task.done()
    assert task.done()
