"""Pipeline overview module.

The panel never touches production paths directly.  A host-local collector (run
by the operator, outside this repo's scope) writes a sanitized JSON snapshot to
PIPELINE_SNAPSHOT_PATH; this module only reads and serves it.

Snapshot schema (all fields optional, unknown fields ignored):
{
  "generated_at": "2026-01-01T00:00:00Z",
  "queues": [
    {"name": "staging", "items": 12, "bytes": 123456, "oldest_age_seconds": 3600}
  ],
  "quota": [
    {"identity": "uploader-1", "state": "ok|limited", "limited_since": "..."}
  ],
  "fallback": {"items": 3, "bytes": 999, "capacity_bytes": 400000000000},
  "alerts": [{"level": "warn", "message": "..."}]
}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class PipelineReader:
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
                   for key in ("queues", "quota", "alerts")):
                return {"available": False, "reason": "invalid snapshot collection"}
            if "fallback" in data and not isinstance(data["fallback"], dict):
                return {"available": False, "reason": "invalid snapshot fallback"}
        except (OSError, ValueError) as exc:
            return {"available": False, "reason": f"unreadable snapshot: {type(exc).__name__}"}
        return {
            "available": True,
            "snapshot_age_seconds": age,
            "stale": age > 300,
            "data": data,
        }


class MockPipeline:
    def snapshot(self) -> dict[str, Any]:
        return {
            "available": True,
            "snapshot_age_seconds": 12.0,
            "stale": False,
            "data": {
                "generated_at": "mock",
                "queues": [
                    {"name": "staging", "items": 14, "bytes": 39 * 2**30,
                     "oldest_age_seconds": 4 * 3600},
                    {"name": "upload-lanes", "items": 18, "bytes": 81 * 2**30,
                     "oldest_age_seconds": 9 * 3600},
                ],
                "quota": [
                    {"identity": "uploader-1", "state": "ok"},
                    {"identity": "uploader-2", "state": "limited",
                     "limited_since": "mock-time"},
                ],
                "fallback": {"items": 8, "bytes": 35 * 2**30,
                             "capacity_bytes": 400 * 2**30},
                "alerts": [{"level": "warn", "message": "demo alert"}],
            },
        }
