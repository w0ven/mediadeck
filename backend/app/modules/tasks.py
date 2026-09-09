"""Scheduled-task health module.

Host cron/guard jobs are the least visible layer of a media stack: they only
surface when something has already failed. The panel never reads crontab or
production logs itself. A host-local collector writes a sanitized snapshot
and this module serves it.

Snapshot schema:
{
  "generated_at": "...",
  "tasks": [
    {"name": "generic-label", "schedule": "*/5 * * * *", "enabled": true,
     "last_run": 1756400000.0, "last_status": "ok|failed|unknown",
     "last_duration_ms": 1234, "exit_code": 0,
     "failure_streak": 0, "last_error": ""}
  ],
  "alerts": [{"level": "warn|bad", "message": "..."}]
}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class TasksReader:
    def __init__(self, snapshot_path: str) -> None:
        self._path = Path(snapshot_path) if snapshot_path else None

    def snapshot(self) -> dict[str, Any]:
        if self._path is None:
            return {"available": False, "reason": "snapshot not configured or missing"}
        try:
            if not self._path.is_file():
                return {"available": False, "reason": "snapshot not configured or missing"}
            data = json.loads(self._path.read_text(encoding="utf-8"))
            age = round(max(0, time.time() - self._path.stat().st_mtime), 1)
            if not isinstance(data, dict):
                return {"available": False, "reason": "invalid snapshot: expected object"}
            if any(key in data and (not isinstance(data[key], list)
                                    or any(not isinstance(row, dict) for row in data[key]))
                   for key in ("tasks", "alerts")):
                return {"available": False, "reason": "invalid snapshot collection"}
        except (OSError, ValueError) as exc:
            return {
                "available": False,
                "reason": f"unreadable snapshot: {type(exc).__name__}",
            }
        return {
            "available": True,
            "snapshot_age_seconds": age,
            "stale": age > 600,
            "data": data,
        }


class MockTasks:
    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "available": True,
            "snapshot_age_seconds": 18.0,
            "stale": False,
            "data": {
                "generated_at": "mock",
                "tasks": [
                    {
                        "name": "snapshot-collector",
                        "schedule": "*/5 * * * *",
                        "enabled": True,
                        "last_run": now - 120,
                        "last_status": "ok",
                        "last_duration_ms": 840,
                        "exit_code": 0,
                        "failure_streak": 0,
                        "last_error": "",
                    },
                    {
                        "name": "health-probe",
                        "schedule": "*/2 * * * *",
                        "enabled": True,
                        "last_run": now - 90,
                        "last_status": "ok",
                        "last_duration_ms": 210,
                        "exit_code": 0,
                        "failure_streak": 0,
                        "last_error": "",
                    },
                    {
                        "name": "quota-guard",
                        "schedule": "*/10 * * * *",
                        "enabled": True,
                        "last_run": now - 600,
                        "last_status": "failed",
                        "last_duration_ms": 15000,
                        "exit_code": 1,
                        "failure_streak": 4,
                        "last_error": "probe timed out",
                    },
                    {
                        "name": "import-worker",
                        "schedule": "0 * * * *",
                        "enabled": True,
                        "last_run": None,
                        "last_status": "unknown",
                        "last_duration_ms": None,
                        "exit_code": None,
                        "failure_streak": 0,
                        "last_error": "",
                    },
                    {
                        "name": "cache-reaper",
                        "schedule": "30 3 * * *",
                        "enabled": True,
                        "last_run": now - 6 * 3600,
                        "last_status": "ok",
                        "last_duration_ms": 4200,
                        "exit_code": 0,
                        "failure_streak": 0,
                        "last_error": "",
                    },
                    {
                        "name": "nightly-audit",
                        "schedule": "0 4 * * *",
                        "enabled": False,
                        "last_run": now - 2 * 86400,
                        "last_status": "ok",
                        "last_duration_ms": 980,
                        "exit_code": 0,
                        "failure_streak": 0,
                        "last_error": "",
                    },
                ],
                "alerts": [
                    {
                        "level": "bad",
                        "message": "quota-guard: failed 4 times in a row",
                    },
                ],
            },
        }
