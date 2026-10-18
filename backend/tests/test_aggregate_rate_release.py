"""Isolated HTTP checks for the shared streaming rate relay."""
from __future__ import annotations

import concurrent.futures
import http.client
import importlib.util
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "agent" / "stream_rate_proxy.py"
spec = importlib.util.spec_from_file_location("stream_rate_proxy", MODULE)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
PAYLOAD = bytes(range(256)) * (12 * 1024 * 1024 // 256)


class Reader(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self.reply()

    def do_GET(self):
        self.reply()

    def reply(self):
        if not self.path.startswith("/video?"):
            self.send_error(404)
            return
        start, end = 0, len(PAYLOAD) - 1
        if "Range" in self.headers:
            start, end = map(int, self.headers["Range"][6:].split("-"))
        body = PAYLOAD[start:end + 1]
        self.send_response(206 if "Range" in self.headers else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if "Range" in self.headers:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass


@pytest.fixture
def relay():
    readers = [ThreadingHTTPServer(("127.0.0.1", 0), Reader) for _ in range(2)]
    server = proxy.RateServer(("127.0.0.1", 0),
                              {f"/{i}/": reader.server_port for i, reader in enumerate(readers)})
    servers = [*readers, server]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    yield server
    for s in servers:
        s.shutdown()
        s.server_close()
    for thread in threads:
        thread.join(timeout=3)


def fetch(relay, pool=0, *, rate=15_000_000, user="abcdef0123", method="GET", span=None):
    headers = {"X-Mediadeck-Rate-Key": user, "X-Mediadeck-Rate": str(rate)}
    if span is not None:
        headers["Range"] = f"bytes={span[0]}-{span[1]}"
    conn = http.client.HTTPConnection("127.0.0.1", relay.server_port, timeout=12)
    try:
        conn.request(method, f"/{pool}/video?e=123", headers=headers)
        reply = conn.getresponse()
        return reply.status, reply.getheader("Content-Range"), reply.read()
    finally:
        conn.close()


def test_head_range_and_untrusted_identity_rejected(relay):
    assert fetch(relay, method="HEAD", span=(20, 49)) == (206, f"bytes 20-49/{len(PAYLOAD)}", b"")
    assert fetch(relay, span=(20, 49)) == (206, f"bytes 20-49/{len(PAYLOAD)}", PAYLOAD[20:50])
    conn = http.client.HTTPConnection("127.0.0.1", relay.server_port)
    try:
        conn.request("GET", "/0/video?e=123")
        assert conn.getresponse().status == 403
    finally:
        conn.close()


@pytest.mark.parametrize("rate", [15_000_000, 35_000_000, 40_000_000])
def test_parallel_pools_share_final_member_rate(relay, rate):
    assert proxy.DEFAULT_RATE == 15_000_000
    assert proxy.parse_rate("0") == 15_000_000
    assert proxy.parse_rate(str(rate)) == rate
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        start = time.monotonic()
        jobs = [workers.submit(fetch, relay, pool=i, rate=rate) for i in range(2)]
        result = [job.result(timeout=12) for job in jobs]
        elapsed = time.monotonic() - start
    assert all(status == 200 and body == PAYLOAD for status, _, body in result)
    expected = 2 * len(PAYLOAD) / rate
    assert expected * .93 <= elapsed < expected * 2 + .5, (rate, elapsed)


def test_distinct_accounts_have_independent_budget(relay):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        start = time.monotonic()
        jobs = [workers.submit(fetch, relay, pool=i, user=f"abcdef012{i}") for i in range(2)]
        assert all(job.result(timeout=12)[0] == 200 for job in jobs)
        elapsed = time.monotonic() - start
    assert elapsed < 2 * len(PAYLOAD) / 15_000_000 * .95, elapsed
