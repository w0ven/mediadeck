"""Retention for device audit rows only.

Device events (``device.*``: refusals, kicks, auto-logouts, removals) are
high-volume operational noise once they are old; a refusal loop once wrote
millions of them. They are kept for RETENTION_DAYS and then deleted in small
batches so no single write transaction holds the database for long.

Every other action (points, economy, membership, invitations, settings, ...)
is a record of account history and is never touched here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

PREFIX = "device."
RETENTION_DAYS = 90
BATCH = 2000
FIRST_RUN_DELAY = 300.0  # seconds after startup
INTERVAL = 86400.0       # daily
PAUSE = 0.05             # between batches, lets other writers in

# uvicorn's error logger is the one that reaches the service journal at INFO.
log = logging.getLogger("uvicorn.error")


def purge_batch(db: Any, cutoff: int, batch: int = BATCH) -> int:
    """Delete up to ``batch`` device rows older than ``cutoff`` (epoch seconds)."""
    # substr() is an exact, case-sensitive prefix test; LIKE would also match
    # other casings and treats '_' as a wildcard.
    return db.execute(
        "DELETE FROM audit_log WHERE id IN (SELECT id FROM audit_log "
        "WHERE ts < ? AND substr(action, 1, ?) = ? LIMIT ?)",
        (int(cutoff), len(PREFIX), PREFIX, int(batch)))


async def purge_expired(db: Any, *, now: float | None = None, days: int = RETENTION_DAYS,
                        batch: int = BATCH, pause: float = PAUSE) -> int:
    cutoff = int((time.time() if now is None else now) - days * 86400)
    total = 0
    while True:
        deleted = await asyncio.to_thread(purge_batch, db, cutoff, batch)
        total += deleted
        if deleted < batch:
            return total
        await asyncio.sleep(pause)


async def retention_loop(db: Any, *, first_delay: float = FIRST_RUN_DELAY,
                         interval: float = INTERVAL) -> None:
    await asyncio.sleep(first_delay)
    while True:
        started = time.monotonic()
        try:
            deleted = await purge_expired(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry on the next cycle
            log.warning("audit retention: device.* purge failed: %s", type(exc).__name__)
        else:
            log.info("audit retention: deleted %d device.* rows older than %d days in %.1fs",
                     deleted, RETENTION_DAYS, time.monotonic() - started)
        await asyncio.sleep(interval)
