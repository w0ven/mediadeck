"""Generated front doors must leave Emby's non-playback APIs untouched.

Both engines run as disposable loopback processes, using the actual FastAPI
panel and a recording Emby origin. MEDIADECK_NGINX_BINARY can point at an
extracted package; no installed/running nginx service is needed.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest
from test_external_entries import (
    client,  # noqa: F401 - imported pytest fixture
    entry_headers,
)

from app.modules.provisioning import emby_frontend_snippet

NGINX = os.environ.get("MEDIADECK_NGINX_BINARY") or shutil.which("nginx")
CADDY = shutil.which("caddy")
ENGINES = [
    pytest.param("caddy", marks=pytest.mark.skipif(not CADDY, reason="Caddy is unavailable")),
    pytest.param("nginx", marks=pytest.mark.skipif(not NGINX, reason="nginx is unavailable")),
]
PREFIXES = ("/emby/Videos", "/emby/videos", "/Videos", "/videos")
QUERY = "Language=eng&label=a%2Bb%20c&tag=first&tag=second&empty="
BODY = b'{"Language":"eng","Data":"line one\\nline two"}\n'


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(params=ENGINES)
def origin_proxy(client, tmp_path, request):  # noqa: F811 - imported pytest fixture
    engine = request.param
    hits = {"panel": [], "origin": []}
    control = {"panel_status": None}

    def handler(role):
        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def dispatch(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                hits[role].append({
                    "method": self.command, "path": self.path, "body": body,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                status, response_body, response_headers = 204, b"", {}
                if role == "panel":
                    if control["panel_status"] is not None:
                        status = control["panel_status"]
                    else:
                        result = client.request(self.command, self.path, content=body,
                                                headers=dict(self.headers), follow_redirects=False)
                        status, response_body = result.status_code, result.content
                        response_headers = result.headers
                hits[role][-1]["status"] = status
                self.send_response(status)
                for name, value in response_headers.items():
                    if name.lower() not in {"content-length", "transfer-encoding", "connection"}:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(response_body)

            do_GET = do_HEAD = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = dispatch

        return Upstream

    ports = {}
    for role in ("panel", "origin"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(role))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        ports[role] = server.server_port

        def close_server(server=server, thread=thread):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        request.addfinalizer(close_server)

    port = _port()
    snippet = emby_frontend_snippet(
        f"http://127.0.0.1:{ports['panel']}", "https://emby.example.com", engine,
        f"http://127.0.0.1:{ports['origin']}",
    )
    if engine == "caddy":
        config = "{\n admin off\n auto_https off\n}\n" + snippet.replace(
            "emby.example.com {", f"http://emby.example.com:{port} {{\n    bind 127.0.0.1")
        config_path = tmp_path / "Caddyfile"
        command = [CADDY, "run", "--config", str(config_path), "--adapter", "caddyfile"]
    else:
        config = f"""pid {tmp_path}/nginx.pid;
error_log {tmp_path}/nginx-error.log error;
events {{ worker_connections 128; }}
http {{
    access_log off;
    client_body_temp_path {tmp_path}/client-body;
    proxy_temp_path {tmp_path}/proxy-temp;
    fastcgi_temp_path {tmp_path}/fastcgi-temp;
    uwsgi_temp_path {tmp_path}/uwsgi-temp;
    scgi_temp_path {tmp_path}/scgi-temp;
    server {{
        listen 127.0.0.1:{port};
        server_name emby.example.com;
        {snippet}
    }}
}}
"""
        config_path = tmp_path / "nginx.conf"
        command = [NGINX, "-p", str(tmp_path) + "/", "-c", str(config_path),
                   "-g", "daemon off; master_process off;"]
    config_path.write_text(config)
    if engine == "nginx":
        subprocess.run([NGINX, "-t", "-q", "-p", str(tmp_path) + "/", "-c", str(config_path)],
                       capture_output=True, check=True, timeout=10)
    with (tmp_path / "proxy.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                assert process.poll() is None, f"isolated {engine} exited during startup"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail(f"isolated {engine} did not start within eight seconds")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                              follow_redirects=False, timeout=5) as http:
                yield http, hits, control
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _headers():
    return {
        **entry_headers(), "Host": "emby.example.com",
        "Content-Type": "application/json", "Cookie": "demo-session=scope-test",
        "Authorization": 'MediaBrowser Client="ScopeTest", Token="client-emby-token"',
        "X-Scope-Trace": "scope-test",
    }


def _assert_origin(http, hits, method, path, body=BODY):
    before_panel, before_origin = len(hits["panel"]), len(hits["origin"])
    headers = _headers()
    response = http.request(method, path, headers=headers, content=body)
    assert response.status_code == 204, {
        "response": response.status_code,
        "upstreams": {role: [hit["status"] for hit in records] for role, records in hits.items()},
    }
    assert len(hits["panel"]) == before_panel, "non-playback request must not reach the panel"
    assert len(hits["origin"]) == before_origin + 1
    received = hits["origin"][-1]
    assert received["method"] == method
    assert received["path"] == path
    assert received["body"] == body
    for name in ("Authorization", "X-Emby-Token", "Content-Type", "Cookie", "X-Scope-Trace"):
        assert received["headers"][name.lower()] == headers[name]
    assert "x-mediadeck-entry" not in received["headers"]
    assert "x-mediadeck-entry-key" not in received["headers"]
    return response


def test_subtitle_post_delete_bypass_panel_with_method_body_query_and_auth(origin_proxy):
    http, hits, _ = origin_proxy
    for prefix in PREFIXES:
        _assert_origin(http, hits, "POST", f"{prefix}/item42/Subtitles?{QUERY}")
        _assert_origin(http, hits, "DELETE", f"{prefix}/item42/Subtitles/2?{QUERY}")


def test_non_get_head_stream_requests_bypass_panel(origin_proxy):
    http, hits, _ = origin_proxy
    for prefix in PREFIXES:
        for method in ("POST", "DELETE", "PUT", "PATCH", "OPTIONS"):
            for endpoint in ("original.mkv", "stream"):
                _assert_origin(http, hits, method, f"{prefix}/item42/{endpoint}?{QUERY}")


def test_large_upload_body_survives_direct_and_named_origin_routes(origin_proxy):
    http, hits, _ = origin_proxy
    body = json.dumps({
        "Language": "eng", "Format": "srt",
        "Data": base64.b64encode(b"synthetic subtitle line\r\n" * 4000).decode(),
    }).encode()
    # Exceed nginx's memory body buffer: both ordinary Emby routing and the
    # pre-panel method guard must preserve the complete upload, not just tiny
    # JSON requests which fit in a single read.
    for endpoint in ("Subtitles", "stream.mkv"):
        _assert_origin(http, hits, "POST", f"/emby/Videos/item42/{endpoint}?{QUERY}", body=body)


def test_other_video_endpoints_bypass_panel(origin_proxy):
    http, hits, _ = origin_proxy
    for prefix in PREFIXES:
        for endpoint in ("Subtitles", "Subtitles/2/Stream.srt", "AdditionalParts", "master.m3u8",
                         "hls/segment1.ts", "streaming", "original.mkv/nested", "stream.fake.mkv"):
            for method in ("GET", "HEAD"):
                _assert_origin(http, hits, method, f"{prefix}/item42/{endpoint}?{QUERY}", body=b"")
    # Unsupported prefix casing must not be captured into FastAPI's 404 either.
    _assert_origin(http, hits, "GET", f"/Emby/ViDeOs/item42/original.mkv?{QUERY}", body=b"")


def test_four_prefixes_dispatch_only_get_head_on_playback_endpoints(origin_proxy):
    http, hits, _ = origin_proxy
    for prefix in PREFIXES:
        for method in ("GET", "HEAD"):
            for tail in ("original.mkv", "stream.mkv?Static=true", "stream?Static=1"):
                for registered in (False, True):
                    headers = _headers()
                    if not registered:
                        headers.pop("X-Mediadeck-Entry")
                        headers.pop("X-Mediadeck-Entry-Key")
                    path = f"{prefix}/item42/{tail}"
                    response = http.request(method, path, headers=headers)
                    assert response.status_code == 302
                    target = urlsplit(response.headers["location"])
                    if registered:
                        assert target.hostname == "friend-a.example.com"
                        assert target.path.startswith("/_n/")
                    else:
                        assert target.hostname in {"edge-a.example.com", "edge-b.example.com"}
                        assert target.path.startswith("/s/")
                    assert "r=" in target.query and "k=" in target.query
                    assert hits["panel"][-1]["method"] == method
                    assert hits["panel"][-1]["path"] == path
                    assert "no-store" in response.headers["cache-control"]
    assert len(hits["panel"]) == 48
    assert hits["origin"] == []


def test_no_blanket_405_fallback(origin_proxy):
    http, hits, control = origin_proxy
    control["panel_status"] = 405
    response = http.get("/emby/videos/item42/original.mkv", headers=_headers())
    assert response.status_code == 405
    assert len(hits["panel"]) == 1 and hits["origin"] == []


def test_recognised_stream_transcode_retains_original_fallback(origin_proxy):
    http, hits, _ = origin_proxy
    path = f"/Videos/item42/stream.mkv?{QUERY}"
    response = http.get(path, headers=_headers())  # no Static=true -> Emby remux
    assert response.status_code == 204
    assert len(hits["panel"]) == len(hits["origin"]) == 1
    assert hits["origin"][0]["method"] == "GET"
    assert hits["origin"][0]["path"] == path
    assert hits["origin"][0]["body"] == b""
    assert "x-mediadeck-entry-key" not in hits["origin"][0]["headers"]
