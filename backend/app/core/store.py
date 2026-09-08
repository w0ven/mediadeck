"""Runtime settings store.

Operator-editable configuration lives here, not in ``.env``.  mediadeck is a
product: connecting Emby, adding a streaming node or changing dispatch policy
must be doable from the UI by someone who has no shell access, and must take
effect without a service restart.

Layout: a single JSON document written atomically (tmp file + ``os.replace``,
mode 600) into the data directory, which is gitignored.  Environment variables
are read exactly once, to bootstrap the first run, so existing .env-based
deployments migrate silently on upgrade.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app.core.errors import ConfigError

SCHEMA_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "emby": {
        "enabled": False,
        "url": "",
        "api_key": "",
        "verify_ssl": True,
        "timeout_seconds": 15,
    },
    "nodes": [],
    "updated_at": 0.0,
}


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


class SettingsStore:
    """Thread-safe JSON-backed settings document."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = _clone(DEFAULTS)
        self._loaded_from_disk = False
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def loaded_from_disk(self) -> bool:
        """False when no settings file existed yet (first run / bootstrap)."""
        return self._loaded_from_disk

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # An existing unreadable/corrupt file is not a first run. Refuse
            # bootstrap rather than silently overwriting operator settings.
            raise ConfigError("设置文件无法读取或已损坏；请检查权限或从备份恢复") from None
        if not isinstance(raw, dict):
            raise ConfigError("设置文件必须为 JSON 对象；请检查文件或从备份恢复")
        merged = _clone(DEFAULTS)
        for key, value in raw.items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = value
        merged["version"] = SCHEMA_VERSION
        self._data = merged
        self._loaded_from_disk = True

    def save(self) -> None:
        with self._lock:
            snapshot = _clone(self._data)
            snapshot["updated_at"] = time.time()
            payload = json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".settings-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._path)
                self._data = snapshot
                self._loaded_from_disk = True
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    # -- access --------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return _clone(self._data.get(key, default))

    def set(self, key: str, value: Any, *, persist: bool = True) -> None:
        with self._lock:
            previous = self._data
            self._data = {**previous, key: _clone(value)}
            if persist:
                try:
                    self.save()
                except BaseException:
                    # Readers share this lock, so a failed commit is never
                    # observable as a successfully changed runtime setting.
                    self._data = previous
                    raise

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def set_section(self, name: str, value: dict[str, Any], *, persist: bool = True) -> None:
        self.set(name, value, persist=persist)

    def document(self) -> dict[str, Any]:
        with self._lock:
            return _clone(self._data)
