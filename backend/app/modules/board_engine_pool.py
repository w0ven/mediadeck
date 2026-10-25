"""One process-wide budget for BOTH optional board engines (single app worker).

Acquire before Popen and release only after the child has been reaped. The
absolute job deadline includes executor queueing, slot queueing and thinking.
No caller on the HTTP/event-loop path waits here: only robot worker threads do.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

MAX_ENGINE_PROCESSES = 2
_SLOTS = threading.BoundedSemaphore(MAX_ENGINE_PROCESSES)
_JOB_DEADLINE = ContextVar('board_engine_deadline', default=None)


@contextmanager
def job_budget(deadline):
    token = _JOB_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _JOB_DEADLINE.reset(token)


@contextmanager
def engine_slot(timeout):
    """Yield an absolute deadline, or time out WITHOUT starting a process."""
    deadline = time.monotonic() + timeout
    job_deadline = _JOB_DEADLINE.get()
    if job_deadline is not None:
        deadline = min(deadline, job_deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _SLOTS.acquire(timeout=remaining):
        raise TimeoutError('board engine queue deadline')
    try:
        # An acquired slot may become free just as our budget expires.
        if time.monotonic() >= deadline:
            raise TimeoutError('board engine queue deadline')
        yield deadline
    finally:
        _SLOTS.release()
