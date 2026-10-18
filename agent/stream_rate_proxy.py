"""Loopback streaming relay: one aggregate, paced byte budget per signed user tag.

The ONLY supported ingress is nginx's already verified /s/* location. nginx must
*overwrite* X-Mediadeck-Rate-Key and X-Mediadeck-Rate with its own variables,
not trust inbound client headers. Run exactly ONE process for ALL pools on a
node, or each pool would have a separate budget. No filesystem access here.
"""
from __future__ import annotations

import argparse
import http.client
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Panel bandwidth uses decimal MB/s (120000 kbps = 15000000 B/s).
# Whitelist defaults to 35 MB/s; signed card/individual increases may exceed it.
DEFAULT_RATE = 15_000_000
CHUNK = 16 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection"}
IDENTITY = {"x-mediadeck-rate-key", "x-mediadeck-rate"}


class AggregateLimiter:
    """One reservation per body chunk, shared by all ports and all threads.

    No per-request token bucket: each chunk is assigned the next departure
    time. A busy member's concurrent streams interleave at CHUNK granularity;
    an idle stream cannot reserve capacity or waste another stream's share.
    A lower-cap signed URL temporarily lowers the common budget (never raises
    an existing stream's entitlement). No idle credit / seek burst accrues.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.members: dict[str, dict] = {}

    def join(self, key: str, rate: int) -> object:
        token = object()
        with self.lock:
            entry = self.members.setdefault(key, {"rates": {}, "next": time.monotonic()})
            entry["rates"][token] = rate
        return token

    def leave(self, key: str, token: object) -> None:
        with self.lock:
            entry = self.members[key]
            del entry["rates"][token]
            if not entry["rates"]:
                del self.members[key]

    def reserve(self, key: str, size: int) -> None:
        with self.lock:
            entry = self.members[key]
            rate = min(entry["rates"].values())
            now = time.monotonic()
            departure = max(now, entry["next"])
            entry["next"] = departure + size / rate
        delay = departure - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def quantum(self, key: str) -> int:
        with self.lock:
            return min(CHUNK, max(1, min(self.members[key]["rates"].values()) // 100))


def parse_rate(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value):
        raise ValueError("invalid rate")
    amount = int(value)
    # Old signed r=0 links use the ordinary baseline. Positive r has already
    # been verified by nginx's HMAC gate and is the effective entitlement,
    # including a purchased card or individual increase above 35 MB/s.
    return amount or DEFAULT_RATE


class RateServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bind: tuple[str, int], routes: dict[str, int]):
        super().__init__(bind, RateHandler)
        self.routes = routes
        self.limiter = AggregateLimiter()


class RateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.relay()

    def do_HEAD(self) -> None:
        self.relay()

    def relay(self) -> None:
        # Identity must be supplied by the trusted ingress on EVERY request.
        # Prefixes are selected before stripping; two pools share one limiter.
        key = self.headers.get("X-Mediadeck-Rate-Key", "")
        raw_rate = self.headers.get("X-Mediadeck-Rate", "")
        path = self.path.split("?", 1)[0]
        prefix = next((p for p in self.server.routes if path.startswith(p)), None)
        if (not prefix or not key or len(key) > 128 or
                not re.fullmatch(r"[0-9a-f]{10}|[0-9a-fA-F:.]+", key)):
            self.send_error(403)
            return
        try:
            rate = parse_rate(raw_rate)
        except ValueError:
            self.send_error(403)
            return
        reader_port = self.server.routes[prefix]
        # Video HEAD/GET has no request body. Never advertise a body length
        # to the reader without forwarding its bytes.
        if self.headers.get("Content-Length", "0") != "0" or "Transfer-Encoding" in self.headers:
            self.send_error(400)
            return
        # Socket timeout is PER blocking send/recv, not a lifetime limit on
        # a multi-hour response. nginx's existing upstream idle timeout is
        # 30s; the reader gets 35s, without timing out during pacing sleeps.
        upstream = http.client.HTTPConnection("127.0.0.1", reader_port, timeout=35)
        token = None
        sent_headers = False
        try:
            upstream.putrequest(self.command, "/" + self.path[len(prefix):],
                                skip_host=True, skip_accept_encoding=True)
            # Do not propagate internal identity or hop-by-hop headers to the reader.
            connection_fields = {h.strip().lower() for h in
                                 self.headers.get("Connection", "").split(",")}
            for name, value in self.headers.items():
                if name.lower() not in HOP | IDENTITY | connection_fields | {"host", "content-length"}:
                    upstream.putheader(name, value)
            upstream.putheader("Host", f"127.0.0.1:{reader_port}")
            upstream.putheader("Connection", "close")
            upstream.endheaders()
            reply = upstream.getresponse()
            no_body = self.command == "HEAD" or reply.status in (204, 304) or reply.status < 200
            length = reply.getheader("Content-Length")
            # http.client decodes upstream chunked framing; re-frame it on
            # egress when no length is known, preserving 206/HEAD semantics.
            chunked = not no_body and length is None
            self.send_response_only(reply.status, reply.reason)
            for name, value in reply.getheaders():
                if name.lower() not in HOP | {"content-length"}:
                    self.send_header(name, value)
            if length is not None:
                self.send_header("Content-Length", length)
            elif chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            self.close_connection = True
            if no_body:
                return
            token = self.server.limiter.join(key, rate)
            while True:
                data = reply.read(self.server.limiter.quantum(key))
                if not data:
                    break
                self.server.limiter.reserve(key, len(data))
                if chunked:
                    self.wfile.write(f"{len(data):x}\r\n".encode("ascii"))
                self.wfile.write(data)
                if chunked:
                    self.wfile.write(b"\r\n")
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (OSError, http.client.HTTPException, ValueError, BrokenPipeError):
            if not sent_headers:
                try:
                    self.send_error(502)
                except OSError:
                    pass
            self.close_connection = True
        finally:
            if token is not None:
                self.server.limiter.leave(key, token)
            upstream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9820)
    parser.add_argument("--pool", action="append", required=True,
                        help="/pool-prefix/:reader-port (repeat for every pool)")
    args = parser.parse_args()
    routes = {}
    for spec in args.pool:
        prefix, sep, port = spec.rpartition(":")
        if not sep or not prefix.startswith("/") or not prefix.endswith("/"):
            parser.error("invalid pool (expected /prefix/:port)")
        if prefix in routes:
            parser.error("duplicate pool")
        routes[prefix] = int(port)
    RateServer(("127.0.0.1", args.port), routes).serve_forever()


if __name__ == "__main__":
    main()
