"""A reachable health socket alone must never mark broken media as ready."""
import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.config import NodePool, StreamNode
from app.modules.provisioning import install_script, media_reader_unit, nginx_site


@pytest.fixture
def agent():
    path = Path(__file__).resolve().parents[2] / "agent" / "loadprobe.py"
    spec = importlib.util.spec_from_file_location("readiness_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("status", "content_range", "body", "ready"), [
    (206, "bytes 0-0/123", b"x", True),
    (200, "", b"x", False),
    (206, "bytes 0-0/123", b"", False),
    (206, "bytes 0-1/123", b"xx", False),
    (503, "", b"", False),
    (302, "", b"", False),
])
def test_real_byte_required(agent, tmp_path, status, content_range, body, ready):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["Range"] == "bytes=0-0"
            self.send_response(status)
            self.send_header("Content-Range", content_range)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Location", "http://example.invalid/")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "probes.json"
        config.write_text(json.dumps([f"http://127.0.0.1:{server.server_port}/sample.mkv"]))
        probe = agent.MediaReadiness(str(config), start=False)
        assert probe.snapshot()["media_ok"] is False
        probe.check()
        assert probe.snapshot()["media_ok"] is ready
        if ready:
            probe.checked_at = time.time() - 46
            assert probe.snapshot()["media_ok"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_probe_misconfiguration_fails_closed_without_network(agent, tmp_path):
    config = tmp_path / "probes.json"
    for value in [[], {}, ["https://example.invalid/sample"], ["http://127.0.0.1:9810/x?key=test"]]:
        config.write_text(json.dumps(value))
        probe = agent.MediaReadiness(str(config), start=False)
        probe.check()
        assert probe.snapshot()["media_ok"] is False
        assert probe.snapshot()["media_error"] == "media_read_unavailable"


def test_nginx_never_touches_fuse_and_reader_binds_only_loopback():
    node = StreamNode(name="edge", base_url="https://edge.example.com", probe_url="http://127.0.0.1:9800/load", sign_secret="test-key",
                      pools=[NodePool(name="main", emby_prefix="/media", node_path="/srv/media", url_prefix="/s/main")])
    text = nginx_site(node)
    assert "alias " not in text
    assert "auth_request /_mediadeck/verify;" in text
    assert "proxy_pass http://127.0.0.1:9810/;" in text
    unit = media_reader_unit(node, node.pools[0], 0)
    assert "--addr 127.0.0.1:9810" in unit
    assert "--read-only" in unit
    script = install_script(node, "https://panel.example.com")
    assert script.count("systemctl enable --now mediadeck-reader-edge-main.service") == 1
