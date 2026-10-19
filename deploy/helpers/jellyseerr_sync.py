#!/usr/bin/env python3
"""Explicit legacy-integration applicability; absence is NEVER retirement.

Install at the existing caller's helper path. Operator state is local, not a
repository fixture. Exit 3 is reserved for --applicability-only not_applicable;
normal not_applicable invocation exits 0 but never reports synchronization.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

JOBS = ("jellyfin-recently-added-scan", "availability-sync")
DEFAULT_CONFIG = "/etc/mediadeck/jellyseerr-sync.json"


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or config.get("mode") not in ("active", "not_applicable"):
        raise ValueError("explicit integration mode required")
    if (config["mode"] == "not_applicable"
            and any(not isinstance(config.get(k), str) or not config[k].strip()
                    for k in ("reason", "successor"))):
        raise ValueError("retirement reason and successor required")
    return config


def evaluate(config: dict, *, applicability_only: bool = False) -> tuple[dict, int]:
    if config["mode"] == "not_applicable":
        return {"status": "not_applicable", "reason": config["reason"],
                "successor": config["successor"], "attempted_jobs": 0}, 3 if applicability_only else 0
    if applicability_only:
        return {"status": "applicable", "attempted_jobs": 0}, 0
    base = config["base_url"].rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("invalid integration origin")
    settings = json.loads(Path(config["settings_path"]).read_text())
    key = settings["main"]["apiKey"]
    if not isinstance(key, str) or not key:
        raise ValueError("integration credential unavailable")
    jobs = []
    for job in JOBS:
        try:
            request = urllib.request.Request(
                base + "/api/v1/settings/jobs/" + job + "/run", data=b"{}",
                headers={"X-Api-Key": key, "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=30) as response:
                ok = 200 <= response.status < 300
            jobs.append({"job": job, "accepted": ok})
        except Exception as exc:  # noqa: BLE001 - transport text may carry credentials
            jobs.append({"job": job, "accepted": False, "error_class": type(exc).__name__})
    ok = all(job["accepted"] for job in jobs)
    return {"status": "sync_requested" if ok else "failed", "attempted_jobs": len(jobs),
            "jobs": jobs}, 0 if ok else 1


def write_receipt(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".integration-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump({**result, "observed_at": time.time()}, output)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--applicability-only", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(Path(args.config))
        try:
            result, code = evaluate(config, applicability_only=args.applicability_only)
        except Exception as exc:  # noqa: BLE001 - preserve a failed durable receipt
            result, code = {"status": "error", "error_class": type(exc).__name__}, 1
        if config.get("receipt_file"):
            write_receipt(Path(config["receipt_file"]), result)
    except Exception as exc:  # noqa: BLE001 - never print credential-bearing errors
        result, code = {"status": "error", "error_class": type(exc).__name__}, 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
