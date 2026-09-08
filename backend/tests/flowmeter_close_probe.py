"""Inner unshare -n: collect() closes a gone socket so the 4-tuple can be reused."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))


def load_flow():
    spec = importlib.util.spec_from_file_location("flowmeter", ROOT / "agent" / "flowmeter.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    import subprocess
    subprocess.run(["ip", "link", "set", "lo", "up"], check=False)
    flow = load_flow()
    tmp = tempfile.mkdtemp(prefix="fm-close-")
    meter = flow.FlowMeter(os.path.join(tmp, "n.db"), node="n1", enabled=True)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 18080))
    srv.listen(1)
    cli = socket.socket()
    cli.bind(("127.0.0.1", 41001))
    ready = threading.Event()

    def accept():
        conn, _ = srv.accept()
        ready.set()
        try:
            conn.recv(8)
        except OSError:
            pass
        time.sleep(0.4)
        conn.close()

    threading.Thread(target=accept, daemon=True).start()
    cli.connect(("127.0.0.1", 18080))
    first = meter.register("127.0.0.1", 18080, "127.0.0.1", 41001, "alice")
    cli.sendall(b"GO")
    ready.wait(timeout=2)
    cli.close()
    time.sleep(0.6)
    env = meter.collect()  # daemon path; no mark_closed
    closed = any(s.get("closed") and s.get("conn_id") == first["conn_id"]
                 for s in env.get("samples") or [])
    second = meter.register("127.0.0.1", 18080, "127.0.0.1", 41001, "bob")
    report = {
        "first": first,
        "second": second,
        "closed_by_collect": closed,
        "new_conn": second.get("conn_id") != first.get("conn_id"),
        "allowed": bool(second.get("allow")),
        "not_mixed": second.get("reason") != "mixed_identity",
    }
    report["ok"] = all([
        report["closed_by_collect"], report["new_conn"],
        report["allowed"], report["not_mixed"],
    ])
    meter.disable(delete_table=True)
    meter.close()
    srv.close()
    json.dump(report, sys.stdout)
    print()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
