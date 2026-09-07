"""Inner unshare -n process: two-node measured path + live restart billing."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "agent"))

from app.core.db import Database  # noqa: E402
from app.modules.metering import MeasuredMeteringService  # noqa: E402
from app.modules.signing import user_tag  # noqa: E402


def load_flow():
    spec = importlib.util.spec_from_file_location("flowmeter", ROOT / "agent" / "flowmeter.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


class Server:
    def __init__(self, port: int):
        self.port = port
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept(self):
        self._sock.settimeout(0.4)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        blob = b"M" * 16384
        try:
            conn.settimeout(20)
            conn.recv(64)
            while not self._stop.is_set():
                conn.sendall(blob)
                time.sleep(0.008)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


class Client:
    def __init__(self, bind_port: int, server_port: int):
        self.bind_port = bind_port
        self.server_port = server_port
        self.got = 0
        self.error = None
        self.finished = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        sock = socket.socket()
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.bind_port))
            sock.settimeout(20)
            sock.connect(("127.0.0.1", self.server_port))
            sock.sendall(b"GO\n")
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                self.got += len(data)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self.finished.set()


def wait_got(client: Client, n: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.got >= n:
            return True
        if client.finished.is_set():
            return client.got >= n
        time.sleep(0.02)
    return client.got >= n


def credit(svc, meter):
    env = meter.collect()
    env["node"] = meter.node
    result = svc.ingest(env)
    ack = result.get("ack") or {}
    if ack.get("boot_id") is not None and ack.get("seq") is not None:
        meter.ack(str(ack["boot_id"]), int(ack["seq"]))
    return result, env


def main() -> int:
    subprocess.run(["ip", "link", "set", "lo", "up"], check=False)
    flow = load_flow()
    tmp = tempfile.mkdtemp(prefix="meter-live-")
    report: dict = {"ns": os.readlink("/proc/self/ns/net")}
    tag_a = user_tag("alice")
    tag_b = user_tag("bob")
    svc = MeasuredMeteringService(
        Database(os.path.join(tmp, "panel.db")),
        tag_to_user=lambda: {tag_a: "alice", tag_b: "bob"},
        expected_nodes=lambda: ["n1", "n2"],
    )

    m1 = flow.FlowMeter(os.path.join(tmp, "n1.db"), node="n1", enabled=True)
    m2 = flow.FlowMeter(os.path.join(tmp, "n2.db"), node="n2", enabled=True)
    s1 = Server(18080)
    s2 = Server(18090)
    s1.start()
    s2.start()
    time.sleep(0.05)

    # alice on both nodes, bob on n1 only
    r_a1 = m1.register("127.0.0.1", 18080, "127.0.0.1", 41001, tag_a)
    r_a2 = m2.register("127.0.0.1", 18090, "127.0.0.1", 41002, tag_a)
    r_b = m1.register("127.0.0.1", 18080, "127.0.0.1", 41003, tag_b)
    report["register"] = {"a1": r_a1, "a2": r_a2, "b": r_b}
    ca1 = Client(41001, 18080)
    ca2 = Client(41002, 18090)
    cb = Client(41003, 18080)
    ca1.start()
    ca2.start()
    cb.start()
    if not (wait_got(ca1, 80_000, 5) and wait_got(ca2, 80_000, 5) and wait_got(cb, 80_000, 5)):
        report["ok"] = False
        report["error"] = "warmup failed"
        json.dump(report, sys.stdout, indent=2)
        print()
        return 1

    users = {"alice": tag_a, "bob": tag_b}
    quota = 0

    def billed(user_id: str, node: str) -> int:
        row = svc._db.one(
            "SELECT SUM(bytes) AS b FROM measured_usage_monthly "
            "WHERE emby_user_id=? AND node=?", (user_id, node))
        return int((row or {}).get("b") or 0)

    def tag_counters(env, tag: str) -> int:
        return sum(int(s["counter_bytes"]) for s in env.get("samples") or []
                   if s.get("utag") == tag)

    def enforce(meters, limit: int) -> list[str]:
        """Block whoever the ledger says is over quota. Not a hardcoded user."""
        blocked = []
        for uid, tag in users.items():
            used = svc.snapshot(uid)["measured_used_bytes"] or 0
            if used >= limit:
                blocked.append(tag)
        for meter in meters:
            meter.apply_policy(blocked, terminate=True)
        return blocked

    credit(svc, m1)
    credit(svc, m2)
    used_mid = svc.snapshot("alice")["measured_used_bytes"]
    report["mid_alice"] = used_mid
    report["mid_bob"] = svc.snapshot("bob")["measured_used_bytes"]

    # Live restart of n1 while alice's n1 TCP is still transferring.
    path = os.path.join(tmp, "n1.db")
    old_boot = m1.boot_id
    old_conn = r_a1["conn_id"]
    env_unacked = m1.collect()
    report["unacked_boot"] = env_unacked.get("boot_id")
    report["unacked_seq"] = env_unacked.get("seq")
    m1.close()
    time.sleep(0.2)
    m1b = flow.FlowMeter(path, node="n1", enabled=True)
    pending_copy = []
    for pending in m1b.pending():
        pending["node"] = "n1"
        pending_copy.append(pending)
        first = svc.ingest(pending)
        dup = svc.ingest(pending)
        if not dup.get("duplicate"):
            report["ok"] = False
            report["error"] = "pending replay was not duplicate"
            json.dump(report, sys.stdout, indent=2)
            print()
            return 1
        ack = first.get("ack") or {}
        m1b.ack(str(ack.get("boot_id") or pending.get("boot_id")),
                int(ack.get("seq") if ack.get("seq") is not None else pending.get("seq")))
    _ing, env_new = credit(svc, m1b)
    alice_n1 = billed("alice", "n1")
    alice_counter = tag_counters(env_new, tag_a)
    same_conn = any(s.get("conn_id") == old_conn and s.get("utag") == tag_a
                    for s in env_new.get("samples") or [])
    # Single live conn, no reset: billed total on n1 MUST equal the latest
    # absolute counter for that tag, not a second full curve after new boot.
    totals_match_counter = same_conn and alice_n1 == alice_counter
    stable = alice_n1
    for pending in pending_copy:
        svc.ingest(pending)
    svc.ingest(env_new)
    svc.ingest(env_new)
    unchanged = billed("alice", "n1") == stable
    report["restart_live"] = {
        "old_boot": old_boot,
        "new_boot": m1b.boot_id,
        "boot_changed": m1b.boot_id != old_boot,
        "same_conn_id": same_conn,
        "alice_n1_billed": alice_n1,
        "alice_n1_counter": alice_counter,
        "totals_match_counter": totals_match_counter,
        "replay_unchanged": unchanged,
        "not_doubled": totals_match_counter and unchanged,
    }

    used_before_kill = svc.snapshot("alice")["measured_used_bytes"] or 0
    bob_before = svc.snapshot("bob")["measured_used_bytes"] or 0
    # Alice is on two nodes, bob on one: midpoint separates them.
    quota = (used_before_kill + bob_before) // 2
    blocked = enforce([m1b, m2], quota)
    ca1.finished.wait(timeout=3)
    ca2.finished.wait(timeout=3)
    bob_got_after = cb.got
    deadline = time.time() + 1.5
    while time.time() < deadline and not cb.finished.is_set():
        if cb.got > bob_got_after:
            break
        time.sleep(0.05)
    bob_grew = cb.got > bob_got_after and not cb.finished.is_set()
    refused = m1b.register("127.0.0.1", 18080, "127.0.0.1", 41011, tag_a)
    still_bob = m1b.register("127.0.0.1", 18080, "127.0.0.1", 41012, tag_b)
    report["interrupt"] = {
        "quota": quota,
        "blocked_tags": blocked,
        "alice_used": used_before_kill,
        "bob_used": bob_before,
        "alice_over_quota": used_before_kill >= quota > 0,
        "bob_under_quota": bob_before < quota,
        "alice_stopped": ca1.finished.is_set() and ca2.finished.is_set(),
        "bob_continued": bob_grew and not cb.finished.is_set(),
        "old_url_refused": refused.get("reason") == "blocked" and not refused.get("allow"),
        "bob_still_allowed": bool(still_bob.get("allow")),
        "empty_policy_keeps_block": tag_a in m1b.apply_policy([], terminate=False)["blocked"],
    }

    credit(svc, m1b)
    credit(svc, m2)
    report["final"] = {
        "alice": svc.snapshot("alice")["measured_used_bytes"],
        "bob": svc.snapshot("bob")["measured_used_bytes"],
        "coverage": svc.snapshot("alice")["coverage"],
    }

    verdict = {
        "two_node_alice": bool((used_mid or 0) > 0),
        "restart_not_doubled": bool(report["restart_live"]["not_doubled"]),
        "quota_chose_alice": bool(report["interrupt"]["alice_over_quota"]
                                  and report["interrupt"]["bob_under_quota"]
                                  and tag_a in report["interrupt"]["blocked_tags"]
                                  and tag_b not in report["interrupt"]["blocked_tags"]),
        "alice_stopped": report["interrupt"]["alice_stopped"],
        "bob_continued": report["interrupt"]["bob_continued"],
        "old_url_refused": report["interrupt"]["old_url_refused"],
        "fail_open_keeps_known_block": report["interrupt"]["empty_policy_keeps_block"],
    }
    report["verdict"] = verdict
    report["ok"] = all(verdict.values())

    m1b.disable(delete_table=True)
    m2.disable(delete_table=True)
    m1b.close()
    m2.close()
    s1.stop()
    s2.stop()
    report["tables_after"] = run(["nft", "list", "tables"])[1].strip().splitlines()
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
