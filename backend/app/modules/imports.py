"""Import lane module.

Unified abstraction over the stack's media importers (cloud-drive pullers,
drive-link imports).  The panel tracks import *jobs*; the actual transfer is
executed by host-side workers which report state through the adapter.

Job lifecycle: queued -> running -> done | failed
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.errors import UpstreamError


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobKind(StrEnum):
    CLOUD_DRIVE = "cloud-drive"   # e.g. cloud storage folder pull
    DRIVE_LINK = "drive-link"     # e.g. share-link import


@dataclass
class ImportJob:
    kind: JobKind
    source_ref: str                       # opaque reference (link/folder id)
    category: str = ""                    # target library category label
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: JobState = JobState.QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: float = 0.0                 # 0..1
    items_total: int = 0
    items_done: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "source_ref": self.source_ref,
            "category": self.category,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": round(self.progress, 3),
            "items_total": self.items_total,
            "items_done": self.items_done,
            "error": self.error,
        }


class ImportManager:
    """In-memory job registry. Live mode delegates execution to host workers
    via an executor adapter; mock mode simulates progress on read."""

    def __init__(self, executor: Any | None = None) -> None:
        self._jobs: dict[str, ImportJob] = {}
        self._executor = executor

    def submit(self, kind: JobKind, source_ref: str, category: str = "") -> ImportJob:
        source_ref = source_ref.strip()
        if not source_ref:
            raise ValueError("empty source_ref")
        if self._executor is None:
            raise ValueError("import executor not configured")
        job = ImportJob(kind=kind, source_ref=source_ref, category=category.strip())
        self._jobs[job.id] = job
        try:
            self._executor.start(job)
        except Exception as exc:  # noqa: BLE001 - retain a visible failed job
            job.state = JobState.FAILED
            job.error = f"executor start failed: {type(exc).__name__}"
            job.updated_at = time.time()
        return job

    def _refresh(self, job: ImportJob) -> None:
        if self._executor is None or job.state in (JobState.DONE, JobState.FAILED):
            return
        try:
            self._executor.refresh(job)
        except Exception as exc:  # noqa: BLE001 - retry polling on the next read
            job.error = f"executor refresh failed: {type(exc).__name__}"
        else:
            if job.error.startswith("executor refresh failed:"):
                job.error = ""

    def get(self, job_id: str) -> ImportJob | None:
        job = self._jobs.get(job_id)
        if job:
            self._refresh(job)
        return job

    def list(self, state: str | None = None, limit: int = 100) -> list[ImportJob]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        for job in jobs:
            self._refresh(job)
        if state:
            jobs = [j for j in jobs if j.state.value == state]
        return jobs[: max(1, min(limit, 500))]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.state in (JobState.DONE, JobState.FAILED):
            return False
        if self._executor is not None:
            try:
                self._executor.cancel(job)
            except Exception as exc:  # noqa: BLE001 - never claim a failed cancel succeeded
                job.error = f"executor cancel failed: {type(exc).__name__}"
                raise UpstreamError(job.error) from None
        job.state = JobState.FAILED
        job.error = "cancelled by operator"
        job.updated_at = time.time()
        return True


class MockExecutor:
    """Simulates import execution: jobs advance ~20%/refresh and finish."""

    def start(self, job: ImportJob) -> None:
        job.state = JobState.RUNNING
        job.items_total = 5
        job.updated_at = time.time()

    def refresh(self, job: ImportJob) -> None:
        if job.state is not JobState.RUNNING:
            return
        job.items_done = min(job.items_total, job.items_done + 1)
        job.progress = job.items_done / max(job.items_total, 1)
        job.updated_at = time.time()
        if job.items_done >= job.items_total:
            job.state = JobState.DONE

    def cancel(self, job: ImportJob) -> None:
        return
