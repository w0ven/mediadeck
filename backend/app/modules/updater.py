"""Self-update module.

Deployment contract (DEVELOPMENT.md): the host runs a git checkout of a
release tag.  This module lets the operator, from the web panel:
  - see the currently deployed version (git describe)
  - check the origin for a newer release tag (semver-sorted v0.x.y)
  - trigger an update: detached helper script checks out the tag, reinstalls
    deps and restarts the service, so the API process itself can die safely.

The panel process never mutates the working tree in-process; all git work is
subprocess-based and the actual switch happens in a detached shell so the
restart does not kill the updater mid-flight.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any

SEMVER_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _run(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, "process unavailable or timed out"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def semver_key(tag: str) -> tuple[int, int, int] | None:
    if not isinstance(tag, str):
        return None
    match = SEMVER_TAG.fullmatch(tag)
    if not match:
        return None
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


def latest_tag(tags: list[str]) -> str | None:
    valid = [(semver_key(t), t) for t in tags]
    valid = [(k, t) for k, t in valid if k is not None]
    if not valid:
        return None
    return max(valid)[1]


class Updater:
    def __init__(self, repo_root: str, service_name: str = "mediadeck") -> None:
        self._root = Path(repo_root)
        self._service = service_name
        self._update_lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    def version(self) -> dict[str, Any]:
        code, desc = _run(["git", "describe", "--tags", "--always", "--dirty"], self._root)
        code2, commit = _run(["git", "rev-parse", "--short", "HEAD"], self._root)
        return {
            "version": desc if code == 0 else "unknown",
            "commit": commit if code2 == 0 else "unknown",
        }

    def check(self) -> dict[str, Any]:
        code, out = _run(["git", "ls-remote", "--tags", "--refs", "origin"], self._root, timeout=60)
        if code != 0:
            return {"ok": False, "error": "cannot reach origin"}
        tags = [line.split("refs/tags/")[-1] for line in out.splitlines() if "refs/tags/" in line]
        newest = latest_tag(tags)
        current = self.version()
        cur_key = semver_key(current["version"].split("-")[0])
        new_key = semver_key(newest) if newest else None
        return {
            "ok": True,
            "current": current["version"],
            "latest": newest,
            "update_available": bool(new_key and (cur_key is None or new_key > cur_key)),
        }

    def update(self, target: str | None = None) -> dict[str, Any]:
        with self._update_lock:
            return self._update(target)

    def _update(self, target: str | None) -> dict[str, Any]:
        if self._process is not None and self._process.poll() is None:
            return {"started": False, "error": "update already running"}
        info = self.check()
        if not info.get("ok"):
            return {"started": False, "error": info.get("error", "check failed")}
        tag = target or info.get("latest")
        if not tag or semver_key(tag) is None:
            return {"started": False, "error": "no valid release tag"}
        if not info.get("update_available") and target is None:
            return {"started": False, "error": "already up to date",
                    "current": info.get("current")}
        code, dirty = _run(["git", "status", "--porcelain"], self._root)
        if code != 0 or dirty:
            return {"started": False, "error": "working tree is not clean or unavailable"}
        script = (
            f"cd {shlex.quote(str(self._root))} && git fetch --tags origin && "
            f"git checkout {shlex.quote(tag)} && "
            f"cd backend && .venv/bin/pip install -q -e . && "
            f"systemctl restart {shlex.quote(self._service)}"
        )
        try:
            self._process = subprocess.Popen(
                ["/bin/sh", "-c", f"sleep 1 && {script}"], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return {"started": False, "error": "cannot start update helper"}
        return {"started": True, "target": tag}


class MockUpdater:
    def version(self) -> dict[str, Any]:
        return {"version": "v0.0.0-mock", "commit": "mock000"}

    def check(self) -> dict[str, Any]:
        return {"ok": True, "current": "v0.0.0-mock", "latest": "v0.0.1",
                "update_available": True}

    def update(self, target: str | None = None) -> dict[str, Any]:
        return {"started": True, "target": target or "v0.0.1"}
