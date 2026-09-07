"""Inner process: run inside `unshare -n` against the real FlowMeter API."""
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

AGENT = Path(__file__).resolve().parents[2] / "agent" / "flowmeter.py"
SERVER_PORT = 18080
PORT_A = 41001
PORT_B = 41002
PAYLOAD = 6 * 1024 * 1024
CHUNK = 16 * 1024
PACE = 0.01


def load_meter():
    spec = importlib.util.spec_from_file_location("flowmeter", AGENT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


class Server:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self._sock = socket.socket(family, socket.SOCK_STREAM)
        if family == socket.AF_INET6:
            self._sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(8)
        self._stop = threading.Event()
        self.sent: dict[int, int] = {}
        self.aborts: dict[int, str] = {}

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
                conn, addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()

    def _serve(self, conn, addr):
        peer = addr[1]
        sent = 0
        try:
            conn.settimeout(20)
            conn.recv(64)
            blob = b"M" * CHUNK
            while sent < PAYLOAD and not self._stop.is_set():
                n = min(CHUNK, PAYLOAD - sent)
                conn.sendall(blob[:n])
                sent += n
                time.sleep(PACE)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            self.aborts[peer] = type(exc).__name__
        finally:
            self.sent[peer] = sent
            try:
                conn.close()
            except OSError:
                pass


class Client:
    def __init__(self, host: str, bind_port: int, server_port: int):
        self.host = host
        self.bind_port = bind_port
        self.server_port = server_port
        self.got = 0
        self.error = None
        self.finished = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.bind_port))
            sock.settimeout(20)
            sock.connect((self.host, self.server_port))
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


def wait_got(client: Client, minimum: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.got >= minimum:
            return True
        if client.finished.is_set():
            return client.got >= minimum
        time.sleep(0.02)
    return client.got >= minimum


def counter_for(env, port):
    for sample in env.get("samples", []):
        if int(sample["remote_port"]) == port:
            return int(sample["counter_bytes"])
    return None


def main() -> int:
    subprocess.run(["ip", "link", "set", "lo", "up"], check=False)
    module = load_meter()
    tmp = tempfile.mkdtemp(prefix="flowmeter-")
    report: dict = {
        "ns": os.readlink("/proc/self/ns/net"),
        "tables_before": run(["nft", "list", "tables"])[1].strip().splitlines(),
    }

    meter = module.FlowMeter(os.path.join(tmp, "fm.db"), node="probe", enabled=False)
    meter.enable()
    report["enabled"] = meter.enabled

    # Unregistered control server/client on a third port — must stay 0 in nft.
    unreg_port = 41099
    # Registered A/B.
    srv = Server("127.0.0.1", SERVER_PORT)
    srv.start()
    time.sleep(0.05)

    ra = meter.register("127.0.0.1", SERVER_PORT, "127.0.0.1", PORT_A, "alice")
    rb = meter.register("127.0.0.1", SERVER_PORT, "127.0.0.1", PORT_B, "bob")
    report["register"] = {"A": ra, "B": rb}
    if not (ra.get("allow") and rb.get("allow")):
        report["ok"] = False
        report["error"] = "register did not allow"
        json.dump(report, sys.stdout, indent=2)
        print()
        meter.disable(delete_table=True)
        return 1

    ca = Client("127.0.0.1", PORT_A, SERVER_PORT)
    cb = Client("127.0.0.1", PORT_B, SERVER_PORT)
    ca.start()
    cb.start()
    if not (wait_got(ca, 160_000, 6) and wait_got(cb, 160_000, 6)):
        report["ok"] = False
        report["error"] = "clients did not receive data"
        report["got"] = {"A": ca.got, "B": cb.got}
        json.dump(report, sys.stdout, indent=2)
        print()
        meter.disable(delete_table=True)
        return 1

    mid = meter.collect()
    report["mid"] = {
        "A_app": ca.got, "B_app": cb.got,
        "A_nft": counter_for(mid, PORT_A),
        "B_nft": counter_for(mid, PORT_B),
        "nft_ok": mid["nft_ok"],
        "sample_ports": [s["remote_port"] for s in mid["samples"]],
    }

    # Unregistered transfer: connect without register.
    cu = Client("127.0.0.1", unreg_port, SERVER_PORT)
    cu.start()
    wait_got(cu, 80_000, 4)
    after_unreg = meter.collect()
    report["unregistered"] = {
        "app": cu.got,
        "ports_in_samples": [s["remote_port"] for s in after_unreg["samples"]],
        "unreg_counted": any(int(s["remote_port"]) == unreg_port
                             for s in after_unreg["samples"]),
    }

    # Kill only B.
    kill = meter.terminate("127.0.0.1", SERVER_PORT, "127.0.0.1", PORT_B)
    cb.finished.wait(timeout=4)
    time.sleep(0.7)
    after_kill = meter.collect()
    a_nft_after_kill = counter_for(after_kill, PORT_A)
    b_nft_after_kill = counter_for(after_kill, PORT_B)
    report["kill_B"] = {
        "ss": {"code": kill["code"], "out": kill["out"][:400]},
        "B_finished": cb.finished.is_set(),
        "B_error": cb.error,
        "A_app": ca.got, "B_app": cb.got,
        "A_nft": a_nft_after_kill, "B_nft": b_nft_after_kill,
        "A_grew": (a_nft_after_kill or 0) > (report["mid"]["A_nft"] or 0),
        "B_stopped_app": cb.finished.is_set() and cb.got < PAYLOAD,
    }

    # Close A, counters retained.
    meter.terminate("127.0.0.1", SERVER_PORT, "127.0.0.1", PORT_A)
    ca.finished.wait(timeout=4)
    time.sleep(0.3)
    after_close = meter.collect()
    report["after_close"] = {
        "A_nft": counter_for(after_close, PORT_A),
        "B_nft": counter_for(after_close, PORT_B),
        "B_frozen": counter_for(after_close, PORT_B) == b_nft_after_kill,
        "A_present": counter_for(after_close, PORT_A) is not None,
    }

    # IPv6
    v6_ok = False
    v6_detail: dict = {}
    try:
        srv6 = Server("::1", 18081)
        srv6.start()
        time.sleep(0.05)
        r6 = meter.register("::1", 18081, "::1", 41011, "v6user")
        c6 = Client("::1", 41011, 18081)
        c6.start()
        wait_got(c6, 80_000, 5)
        env6 = meter.collect()
        v6_nft = None
        for sample in env6["samples"]:
            if sample.get("family") == "inet6" and int(sample["remote_port"]) == 41011:
                v6_nft = sample["counter_bytes"]
        v6_ok = bool(r6.get("allow")) and c6.got > 0 and (v6_nft or 0) > 0
        v6_detail = {"register": r6, "app": c6.got, "nft": v6_nft, "allow": r6.get("allow")}
        meter.terminate("::1", 18081, "::1", 41011)
        c6.finished.wait(timeout=2)
        srv6.stop()
    except OSError as exc:
        v6_detail = {"error": f"{type(exc).__name__}: {exc}"}
    report["ipv6"] = {"ok": v6_ok, **v6_detail}

    # Tuple reuse: same 4-tuple after close → new conn_id; generation starts 1.
    reuse = meter.register("127.0.0.1", SERVER_PORT, "127.0.0.1", PORT_A, "alice")
    report["reuse"] = {
        "new_conn_id": reuse.get("conn_id"),
        "old_conn_id": ra.get("conn_id"),
        "different": reuse.get("conn_id") != ra.get("conn_id"),
        "allow": reuse.get("allow"),
    }

    # Restart: new boot, pending still has old envelopes.
    path = os.path.join(tmp, "fm.db")
    old_boot = meter.boot_id
    pending_before = len(meter.pending())
    meter.close()
    meter2 = module.FlowMeter(path, node="probe")
    meter2.enable()
    report["restart"] = {
        "old_boot": old_boot,
        "new_boot": meter2.boot_id,
        "boot_changed": meter2.boot_id != old_boot,
        "pending": len(meter2.pending()),
        "pending_kept": len(meter2.pending()) >= pending_before,
    }
    # Ack one, then collect under new boot — seq starts again.
    if meter2.pending():
        first = meter2.pending()[0]
        meter2.ack(first["boot_id"], first["seq"])
    env_new = meter2.collect()
    report["restart"]["new_seq"] = env_new["seq"]
    report["restart"]["new_boot_on_collect"] = env_new["boot_id"] == meter2.boot_id

    verdict = {
        "register_allow": bool(ra.get("allow") and rb.get("allow")),
        "mid_both_counted": bool((report["mid"]["A_nft"] or 0) > 0 and (report["mid"]["B_nft"] or 0) > 0),
        "unreg_not_counted": not report["unregistered"]["unreg_counted"],
        "kill_B_only": bool(report["kill_B"]["B_stopped_app"] and report["kill_B"]["A_grew"]),
        "close_retains": bool(report["after_close"]["A_present"] and report["after_close"]["B_frozen"]),
        "ipv6": v6_ok,
        "tuple_reuse_new_id": bool(report["reuse"]["different"]),
        "restart_new_boot_keeps_pending": bool(
            report["restart"]["boot_changed"] and report["restart"]["pending_kept"]),
    }
    report["verdict"] = verdict
    report["ok"] = all(verdict.values())

    meter2.disable(delete_table=True)
    meter2.close()
    srv.stop()
    report["tables_after"] = run(["nft", "list", "tables"])[1].strip().splitlines()
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
