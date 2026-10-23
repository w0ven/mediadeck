#!/usr/bin/env python3
"""Restart the Deck service only when it is hung, never during maintenance.

A long-running loop (one process, no per-probe journal noise). Every
INTERVAL seconds it asks systemd for the unit state and then GETs the health
URL with a short timeout. Only probes that get NO answer (timeout, reset,
refused) count; any HTTP answer, even an error status, proves the process is
alive and resets the count. After THRESHOLD consecutive unanswered probes it
runs ``systemctl restart`` once.

Never acts when:
  * a maintenance flag file exists and has not expired (deploys, manual work);
  * the unit is not active/running (manual stop, start/stop in progress, failed);
  * a systemd job for the unit is queued or running;
  * the unit entered active less than GRACE seconds ago;
  * MAX_RESTARTS restarts already happened within the last hour.

Maintenance flag: create the file to pause the watchdog. Its content may be an
expiry as epoch seconds; otherwise it expires FLAG_MAX_AGE seconds after its
mtime, so a forgotten flag cannot disable the watchdog forever.

Configuration (environment, all optional):
  DECK_WATCHDOG_UNIT, DECK_WATCHDOG_URL, DECK_WATCHDOG_FLAG, DECK_WATCHDOG_LOG,
  DECK_WATCHDOG_INTERVAL, DECK_WATCHDOG_TIMEOUT, DECK_WATCHDOG_THRESHOLD,
  DECK_WATCHDOG_GRACE, DECK_WATCHDOG_MAX_RESTARTS, DECK_WATCHDOG_FLAG_MAX_AGE,
  DECK_WATCHDOG_SYSTEMCTL
"""
from __future__ import annotations

import http.client
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Config:
    unit: str = "mediadeck.service"
    url: str = "http://127.0.0.1:8300/healthz"
    flag: str = "/run/mediadeck-maintenance"
    log: str = ""
    interval: float = 15.0
    timeout: float = 5.0
    threshold: int = 4
    grace: float = 60.0
    max_restarts: int = 3
    flag_max_age: float = 7200.0
    systemctl: list[str] = field(default_factory=lambda: ["systemctl"])

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Config:
        c = cls()
        c.unit = env.get("DECK_WATCHDOG_UNIT", c.unit)
        c.url = env.get("DECK_WATCHDOG_URL", c.url)
        c.flag = env.get("DECK_WATCHDOG_FLAG", c.flag)
        c.log = env.get("DECK_WATCHDOG_LOG", c.log)
        c.interval = float(env.get("DECK_WATCHDOG_INTERVAL", c.interval))
        c.timeout = float(env.get("DECK_WATCHDOG_TIMEOUT", c.timeout))
        c.threshold = max(2, int(env.get("DECK_WATCHDOG_THRESHOLD", c.threshold)))
        c.grace = float(env.get("DECK_WATCHDOG_GRACE", c.grace))
        c.max_restarts = max(1, int(env.get("DECK_WATCHDOG_MAX_RESTARTS", c.max_restarts)))
        c.flag_max_age = float(env.get("DECK_WATCHDOG_FLAG_MAX_AGE", c.flag_max_age))
        if env.get("DECK_WATCHDOG_SYSTEMCTL"):
            c.systemctl = shlex.split(env["DECK_WATCHDOG_SYSTEMCTL"])
        return c


class Watchdog:
    def __init__(self, cfg: Config, *, clock=time.time, monotonic=time.monotonic) -> None:
        self.cfg = cfg
        self.clock = clock
        self.monotonic = monotonic
        self.failures = 0
        self.restarts: deque[float] = deque()
        self.paused_reason = ""

    # ---- outputs ---------------------------------------------------------
    def log(self, message: str) -> None:
        line = f"deck-watchdog: {message}"
        print(line, flush=True)
        if self.cfg.log:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock()))
            try:
                with open(self.cfg.log, "a", encoding="utf-8") as f:
                    f.write(f"{stamp} {line}\n")
            except OSError:
                pass

    # ---- inputs ----------------------------------------------------------
    def maintenance(self) -> str:
        try:
            st = os.stat(self.cfg.flag)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            return f"maintenance flag unreadable ({exc.__class__.__name__})"
        expiry = st.st_mtime + self.cfg.flag_max_age
        try:
            with open(self.cfg.flag, encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                expiry = float(text.split()[0])
        except (OSError, ValueError):
            pass
        if self.clock() < expiry:
            return "maintenance flag present"
        return ""

    def unit_state(self) -> dict[str, str]:
        out = subprocess.run(
            [*self.cfg.systemctl, "show", self.cfg.unit, "-p", "ActiveState", "-p", "SubState",
             "-p", "ActiveEnterTimestampMonotonic", "-p", "Job"],
            capture_output=True, text=True, timeout=15, check=False).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def probe(self) -> str:
        """'' when the service answered at all, else a short failure reason."""
        try:
            with urllib.request.urlopen(self.cfg.url, timeout=self.cfg.timeout) as response:
                response.read(256)
            return ""
        except (urllib.error.HTTPError, http.client.HTTPException):
            return ""  # any HTTP answer means the process is not hung
        except (TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return f"no answer ({reason.__class__.__name__})"

    def restart(self) -> int:
        return subprocess.run([*self.cfg.systemctl, "restart", self.cfg.unit],
                              capture_output=True, text=True, timeout=180, check=False).returncode

    # ---- decision --------------------------------------------------------
    def skip_reason(self) -> str:
        reason = self.maintenance()
        if reason:
            return reason
        state = self.unit_state()
        if state.get("Job", "").strip() not in ("", "0"):
            return "systemd job in progress"
        if (state.get("ActiveState"), state.get("SubState")) != ("active", "running"):
            return f"unit {state.get('ActiveState', '?')}/{state.get('SubState', '?')}"
        try:
            entered = int(state.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
        except ValueError:
            entered = 0.0
        if entered and self.monotonic() - entered < self.cfg.grace:
            return "startup grace"
        return ""

    def step(self) -> str:
        """Run one cycle; returns the action taken (for tests and logs)."""
        reason = self.skip_reason()
        if reason:
            if reason != self.paused_reason:
                self.log(f"paused: {reason}; failure count reset")
            self.paused_reason = reason
            self.failures = 0
            return "skip"
        if self.paused_reason:
            self.log(f"resumed after: {self.paused_reason}")
            self.paused_reason = ""
        failure = self.probe()
        if not failure:
            if self.failures:
                self.log(f"health answered again after {self.failures} unanswered probe(s)")
            self.failures = 0
            return "ok"
        self.failures += 1
        self.log(f"health probe {self.failures}/{self.cfg.threshold} failed: {failure}")
        if self.failures < self.cfg.threshold:
            return "fail"
        now = self.monotonic()
        while self.restarts and self.restarts[0] <= now - 3600:
            self.restarts.popleft()
        if len(self.restarts) >= self.cfg.max_restarts:
            self.log(f"NOT restarting {self.cfg.unit}: {len(self.restarts)} restarts in the last hour "
                     f"(limit {self.cfg.max_restarts}); needs an operator")
            self.failures = 0
            return "limited"
        self.log(f"RESTARTING {self.cfg.unit}: {self.failures} consecutive unanswered health probes")
        self.restarts.append(now)
        self.failures = 0
        code = self.restart()
        self.log(f"restart of {self.cfg.unit} finished with exit code {code}")
        return "restarted"


def main() -> int:
    cfg = Config.from_env(dict(os.environ))
    dog = Watchdog(cfg)
    dog.log(f"watching {cfg.unit} at {cfg.url} every {cfg.interval:g}s "
            f"(timeout {cfg.timeout:g}s, threshold {cfg.threshold}, grace {cfg.grace:g}s, "
            f"max {cfg.max_restarts}/h, flag {cfg.flag})")
    once = "--once" in sys.argv[1:]
    while True:
        started = time.monotonic()
        try:
            dog.step()
        except Exception as exc:  # noqa: BLE001 - a watchdog must keep watching
            dog.log(f"cycle error: {exc.__class__.__name__}: {exc}")
        if once:
            return 0
        time.sleep(max(0.0, cfg.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
