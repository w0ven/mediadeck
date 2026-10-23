"""Host hang watchdog: restart only after repeated unanswered probes."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/watchdog/deck_watchdog.py"
spec = importlib.util.spec_from_file_location("deck_watchdog", SCRIPT)
wd = importlib.util.module_from_spec(spec)
sys.modules.setdefault("deck_watchdog", wd)  # dataclasses resolve annotations via the module
spec.loader.exec_module(wd)


class Fake(wd.Watchdog):
    def __init__(self, cfg, answers, state=None, now=10_000.0):
        self.now = now
        super().__init__(cfg, clock=lambda: self.now, monotonic=lambda: self.now)
        self.answers = list(answers)
        self.state = state or {"ActiveState": "active", "SubState": "running",
                               "ActiveEnterTimestampMonotonic": "1000000", "Job": ""}
        self.restart_calls = 0
        self.lines = []

    def log(self, message):
        self.lines.append(message)

    def unit_state(self):
        return dict(self.state)

    def probe(self):
        return self.answers.pop(0) if self.answers else ""

    def restart(self):
        self.restart_calls += 1
        return 0


def cfg(tmp_path, **kw):
    return wd.Config(flag=str(tmp_path / "flag"), threshold=4, grace=60, max_restarts=3, **kw)


def test_needs_n_consecutive_unanswered_probes(tmp_path):
    dog = Fake(cfg(tmp_path), ["timeout"] * 3 + [""] + ["timeout"] * 3)
    assert [dog.step() for _ in range(7)] == ["fail"] * 3 + ["ok"] + ["fail"] * 3
    assert dog.restart_calls == 0
    dog.answers = ["timeout"]
    assert dog.step() == "restarted" and dog.restart_calls == 1
    assert any("RESTARTING" in line for line in dog.lines)


def test_healthy_service_is_never_restarted(tmp_path):
    dog = Fake(cfg(tmp_path), [""] * 100)
    assert {dog.step() for _ in range(100)} == {"ok"} and dog.restart_calls == 0


@pytest.mark.parametrize("state", [
    {"ActiveState": "inactive", "SubState": "dead"},          # manual stop
    {"ActiveState": "deactivating", "SubState": "stop-sigterm"},
    {"ActiveState": "activating", "SubState": "start"},
    {"ActiveState": "failed", "SubState": "failed"},
    {"ActiveState": "active", "SubState": "running", "Job": "4711"},  # restart queued
])
def test_inactive_or_busy_unit_is_left_alone(tmp_path, state):
    dog = Fake(cfg(tmp_path), ["timeout"] * 20, state={"ActiveEnterTimestampMonotonic": "1", **state})
    assert {dog.step() for _ in range(10)} == {"skip"} and dog.restart_calls == 0


def test_startup_grace(tmp_path):
    dog = Fake(cfg(tmp_path), ["timeout"] * 20)
    dog.state["ActiveEnterTimestampMonotonic"] = str(int((dog.now - 30) * 1e6))
    assert {dog.step() for _ in range(6)} == {"skip"} and dog.restart_calls == 0
    dog.now += 31
    assert [dog.step() for _ in range(4)] == ["fail", "fail", "fail", "restarted"]


def test_maintenance_flag_pauses_and_expires(tmp_path):
    c = cfg(tmp_path, flag_max_age=7200)
    dog = Fake(c, ["timeout"] * 40)
    Path(c.flag).write_text(str(dog.now + 600))
    assert {dog.step() for _ in range(10)} == {"skip"}
    assert dog.failures == 0 and dog.restart_calls == 0
    assert any("paused: maintenance flag present" in line for line in dog.lines)
    dog.now += 601  # explicit expiry passed: watching again
    assert [dog.step() for _ in range(4)][-1] == "restarted"
    # An empty flag expires FLAG_MAX_AGE after its mtime, never lasting forever.
    Path(c.flag).write_text("")
    os.utime(c.flag, (dog.now, dog.now))
    assert dog.step() == "skip"
    dog.now += 7201
    assert dog.step() == "fail"


def test_restart_limit_per_hour(tmp_path):
    dog = Fake(cfg(tmp_path), ["timeout"] * 100)
    results = [dog.step() for _ in range(16)]
    assert results.count("restarted") == 3 and results[-1] == "limited"
    assert any("needs an operator" in line for line in dog.lines)
    dog.now += 3601
    assert [dog.step() for _ in range(4)][-1] == "restarted"


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass


def _hung_listener():
    # Accepts connections (kernel backlog) but never answers, like a stuck loop.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    return sock, sock.getsockname()[1]


def _run_script(tmp_path, url, seconds, state):
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake_systemctl.py"
    fake.write_text(
        "import json, sys\n"
        f"calls = {str(calls)!r}\n"
        "open(calls, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'show':\n"
        f"    for k, v in json.loads({json.dumps(state)!r}).items(): print(f'{{k}}={{v}}')\n")
    log = tmp_path / "watchdog.log"
    env = dict(os.environ, DECK_WATCHDOG_URL=url, DECK_WATCHDOG_INTERVAL="0.2",
               DECK_WATCHDOG_TIMEOUT="0.3", DECK_WATCHDOG_THRESHOLD="3", DECK_WATCHDOG_GRACE="0",
               DECK_WATCHDOG_FLAG=str(tmp_path / "flag"), DECK_WATCHDOG_LOG=str(log),
               DECK_WATCHDOG_SYSTEMCTL=f"{sys.executable} {fake}")
    proc = subprocess.Popen([sys.executable, str(SCRIPT)], env=env, stdout=subprocess.PIPE, text=True)
    time.sleep(seconds)
    proc.terminate()
    out, _ = proc.communicate(timeout=10)
    restarts = [line for line in calls.read_text().splitlines() if line.startswith("restart")]
    return out, restarts, log.read_text() if log.exists() else ""


ACTIVE = {"ActiveState": "active", "SubState": "running", "ActiveEnterTimestampMonotonic": "1", "Job": ""}


def test_real_process_restarts_hung_service_once(tmp_path):
    sock, port = _hung_listener()
    try:
        out, restarts, log = _run_script(tmp_path, f"http://127.0.0.1:{port}/healthz", 2.0, ACTIVE)
    finally:
        sock.close()
    assert restarts and restarts[0] == "restart mediadeck.service"
    assert "health probe 1/3 failed: no answer" in out and "RESTARTING mediadeck.service" in out
    assert "RESTARTING mediadeck.service" in log  # also kept in the host log file


def test_real_process_leaves_healthy_service_alone(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out, restarts, _ = _run_script(tmp_path, f"http://127.0.0.1:{server.server_port}/healthz", 1.5, ACTIVE)
    finally:
        server.shutdown()
        server.server_close()
    assert restarts == [] and "failed" not in out


def test_real_process_respects_manual_stop(tmp_path):
    stopped = {"ActiveState": "inactive", "SubState": "dead", "ActiveEnterTimestampMonotonic": "0", "Job": ""}
    out, restarts, _ = _run_script(tmp_path, "http://127.0.0.1:9/healthz", 1.5, stopped)
    assert restarts == [] and "paused: unit inactive/dead" in out
