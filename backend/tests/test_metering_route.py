"""Node credential on the retired measured route, and the guard's policy pull.

Measured byte reports are retired (docs/TRAFFIC_RETIREMENT.md): the route is
still node-authenticated and acknowledges old envelopes without crediting them,
and meterd now only pulls the authoritative access policy.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest
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


def test_measured_route_requires_node_credential_and_acks() -> None:
    tag = user_tag("u1")
    env = {
        "node": "mock-a",
        "boot_id": uuid.uuid4().hex,
        "seq": 1,
        "observed_at": 1_700_000_000.0,
        "nft_ok": True,
        "unit": "kernel_outbound_ip_bytes",
        "samples": [{
            "conn_id": uuid.uuid4().hex, "generation": 1, "utag": tag,
            "family": "inet", "local_ip": "127.0.0.1", "local_port": 443,
            "remote_ip": "10.0.0.2", "remote_port": 41001,
            "counter_bytes": 100, "counter_packets": 1,
            "observed_at": 1_700_000_000.0, "closed": False,
            "coverage": "observed",
        }],
    }
    with TestClient(app) as client:
        assert client.post("/api/edge/mock-a/measured", json=env).status_code == 401
        assert client.post("/api/edge/mock-a/measured", json=env,
                           headers={"Authorization": "Bearer wrong"}).status_code == 401
        creds = client.get("/api/nodes/mock-a/report-token",
                           headers=_basic()).json()["report_token"]
        headers = {"Authorization": f"Bearer {creds}"}
        for _ in range(2):
            response = client.post("/api/edge/mock-a/measured", json=env, headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True and body["retired"] is True
            assert body["accepted"] is False
            assert body["ack"] == {"boot_id": env["boot_id"], "seq": 1}
            assert "credited" not in body
            assert body["policy"]["snapshot"] is True
            assert body["blocked_tags"] == body["policy"]["blocked_tags"]
        # A credential is scoped to its own node.
        wrong = client.post("/api/edge/mock-b/measured", json=env, headers=headers)
        assert wrong.status_code == 401


def test_meterd_pulls_policy_with_node_credential_and_applies_it(tmp_path: Path) -> None:
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append({"path": self.path, "auth": self.headers.get("Authorization")})
            if self.path == "/redirect/api/edge/n1/policy":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.end_headers()
                return
            body = json.dumps({"ok": True, "traffic_tracking": False,
                               "policy": {"snapshot": True, "rev": 1,
                                          "blocked_tags": ["blockedtag"]}}).encode()
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
        mod = _load_meterd()
        body = mod.fetch_policy(panel, "n1", "secret-token")
        assert received[0] == {"path": "/api/edge/n1/policy", "auth": "Bearer secret-token"}
        guard = mod.AccessPolicy(str(tmp_path / "access.json"), str(tmp_path / "deny.map"),
                                 mod.DenyPublisher(str(tmp_path / "deny.map")))
        assert guard.apply(body)["ok"] is True
        assert guard.blocked == {"blockedtag"}
        assert '"blockedtag" 1;' in (tmp_path / "deny.map").read_text()
        # The node credential is never forwarded to a redirect target.
        with pytest.raises(HTTPError):
            mod.fetch_policy(panel + "/redirect", "n1", "secret-token")
        assert len(received) == 2
    finally:
        httpd.shutdown()
