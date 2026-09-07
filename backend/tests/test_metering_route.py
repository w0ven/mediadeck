"""Panel measured ingest route + meterd report_once against a real HTTP ack."""
from __future__ import annotations

import base64
import importlib.util
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.modules.signing import user_tag

AGENT = Path(__file__).resolve().parents[2] / "agent"


def _basic() -> dict[str, str]:
    token = base64.b64encode(b"admin:change-me").decode()
    return {"Authorization": f"Basic {token}"}


def _load_meterd():
    spec = importlib.util.spec_from_file_location("meterd", AGENT / "meterd.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_flow():
    spec = importlib.util.spec_from_file_location("flowmeter", AGENT / "flowmeter.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_measured_route_requires_node_credential_and_acks() -> None:
    tag = user_tag("u1")
    conn_id = uuid.uuid4().hex
    env = {
        "node": "mock-a",
        "boot_id": uuid.uuid4().hex,
        "seq": 1,
        "observed_at": 1_700_000_000.0,
        "nft_ok": True,
        "unit": "kernel_outbound_ip_bytes",
        "samples": [{
            "conn_id": conn_id, "generation": 1, "utag": tag,
            "family": "inet", "local_ip": "127.0.0.1", "local_port": 443,
            "remote_ip": "10.0.0.2", "remote_port": 41001,
            "counter_bytes": 100, "counter_packets": 1,
            "observed_at": 1_700_000_000.0, "closed": False,
            "coverage": "observed",
        }],
    }
    with TestClient(app) as client:
        assert client.post("/api/edge/mock-a/measured", json=env).status_code == 401
        creds = client.get("/api/nodes/mock-a/report-token",
                           headers=_basic()).json()["report_token"]
        headers = {"Authorization": f"Bearer {creds}"}
        first = client.post("/api/edge/mock-a/measured", json=env, headers=headers)
        assert first.status_code == 200
        body = first.json()
        assert body["ok"] is True
        assert body["credited"] == 100
        assert body["ack"] == {"boot_id": env["boot_id"], "seq": 1}
        assert "blocked_tags" in body
        assert "unblock_tags" in body
        assert body["policy_sent_rev"] == body["policy_rev"]
        acks = app.state.metering.policy_acks()
        row = next(a for a in acks if a["node"] == "mock-a")
        assert row["sent_rev"] == body["policy_rev"]
        assert int(row["applied_rev"] or 0) == 0
        env["policy_applied_rev"] = body["policy_rev"]
        confirmed = client.post("/api/edge/mock-a/measured", json=env, headers=headers)
        assert confirmed.json()["duplicate"] is True
        acks = app.state.metering.policy_acks()
        row = next(a for a in acks if a["node"] == "mock-a")
        assert int(row["applied_rev"]) == body["policy_rev"]
        again = client.post("/api/edge/mock-a/measured", json=env, headers=headers)
        assert again.json()["duplicate"] is True
        assert again.json()["credited"] == 0
        later = dict(env)
        later["boot_id"] = "bootB"
        later["samples"] = [dict(env["samples"][0], counter_bytes=120)]
        out = client.post("/api/edge/mock-a/measured", json=later, headers=headers)
        assert out.json()["credited"] == 20
        wrong = client.post("/api/edge/mock-b/measured", json=env, headers=headers)
        assert wrong.status_code == 401


def test_meterd_report_once_posts_pending_and_acks(tmp_path: Path) -> None:
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode())
            received.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "body": payload})
            body = json.dumps({
                "ok": True,
                "ack": {"boot_id": payload["boot_id"], "seq": payload["seq"]},
                "blocked_tags": ["exhausted"],
                "unblock_tags": [],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    panel = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        flow = _load_flow()
        meter = flow.FlowMeter(str(tmp_path / "n1.db"), node="n1", enabled=False)
        meter.register("127.0.0.1", 443, "10.0.0.2", 41001, "alice")
        meterd = _load_meterd().Meterd(
            meter, panel=panel, node="n1", token="secret-token", interval=5)
        result = meterd.report_once()
        assert result.get("ok") is True
        assert received and received[0]["path"] == "/api/edge/n1/measured"
        assert received[0]["auth"] == "Bearer secret-token"
        assert received[0]["body"]["node"] == "n1"
        assert received[0]["body"]["unit"] == "kernel_outbound_ip_bytes"
        assert meter.pending() == []
        assert "exhausted" in meter.blocked_tags()
        meter.close()
    finally:
        httpd.shutdown()
