"""Mount health module.

Storage is the most failure-prone layer of a cloud-backed media stack: FUSE
mounts go stale, a union mount silently loses its permission options, and
ffprobe/ffmpeg wedge in uninterruptible sleep holding a mount hostage.

Like the pipeline module, the panel never touches production paths itself: a
host-local collector writes a sanitized snapshot and this module serves it.

Snapshot schema:
{
  "generated_at": "...",
  "mounts": [
    {"label": "media-main", "kind": "fuse.rclone", "alive": true,
     "readdir_ms": 12.4, "entries": 9, "stuck_processes": 0,
     "cache_bytes": 123, "cache_limit_bytes": 456,
     "fs_free_bytes": 789, "fs_total_bytes": 1000, "options": ["ro"]}
  ],
  "alerts": [{"level": "warn", "message": "..."}]
}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class MountsReader:
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
                   for key in ("mounts", "alerts")):
                return {"available": False, "reason": "invalid snapshot collection"}
        except (OSError, ValueError) as exc:
            return {"available": False, "reason": f"unreadable snapshot: {type(exc).__name__}"}
        return {"available": True, "snapshot_age_seconds": age, "stale": age > 600, "data": data}


class MockMounts:
    def snapshot(self) -> dict[str, Any]:
        return {
            "available": True,
            "snapshot_age_seconds": 20.0,
            "stale": False,
            "data": {
                "generated_at": "mock",
                "mounts": [
                    {"label": "media-main", "kind": "fuse.rclone", "alive": True,
                     "readdir_ms": 8.1, "entries": 9, "stuck_processes": 0,
                     "cache_bytes": 120 * 2**30, "cache_limit_bytes": 400 * 2**30,
                     "fs_free_bytes": 900 * 2**30, "fs_total_bytes": 2000 * 2**30,
                     "options": ["ro"]},
                    {"label": "media-union", "kind": "fuse.mergerfs", "alive": True,
                     "readdir_ms": 3.4, "entries": 9, "stuck_processes": 0,
                     "cache_bytes": None, "cache_limit_bytes": None,
                     "fs_free_bytes": None, "fs_total_bytes": None,
                     "options": ["ro", "allow_other"]},
                    {"label": "media-secondary", "kind": "fuse.rclone", "alive": False,
                     "readdir_ms": None, "entries": 0, "stuck_processes": 2,
                     "cache_bytes": 40 * 2**30, "cache_limit_bytes": 400 * 2**30,
                     "fs_free_bytes": 900 * 2**30, "fs_total_bytes": 2000 * 2**30,
                     "options": ["ro"]},
                ],
                "alerts": [{"level": "warn", "message": "media-secondary: readdir probe timed out"}],
            },
        }
