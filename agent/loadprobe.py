#!/usr/bin/env python3
# ruff: noqa: EXE001
# The installer sets executable mode on the downloaded agent.
"""Node connection/media readiness probe and v2 signature verifier.
No interface/socket byte counters, log tails or user speed telemetry.
Legacy --speed-log/--iface options are accepted but never sampled.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import unquote_to_bytes, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

#: How much of the tail of the speed log to replay on startup when rebuilding
#: the peer-address -> user map. Large enough to cover a busy hour, small
#: enough to read instantly on a multi-hundred-MB log.
SEED_BYTES = 4 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "meter redirect refused", headers, fp)


class SigningVerifier:
    """Request-thread verifier; independent of socket/NIC sampling and nft.

    The wire protocol is deliberately reproduced in this stdlib-only agent;
    cross-implementation vectors test it against the panel signer. No backend
    imports, extra deployed files or external crypto packages are required.
    """

    def __init__(self, config: dict) -> None:
        if not isinstance(config, dict) or config.get("version") != 2:
            raise ValueError("v2 signing configuration required")
        self._secret = config.get("secret")
        self.digest_arg = config.get("arg_digest", "k")
        self.expires_arg = config.get("arg_expires", "e")
        names = (self.digest_arg, self.expires_arg, "r", "u")
        if (not isinstance(self._secret, str) or not self._secret
                or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_]+", n) for n in names)
                or len({n.lower() for n in names}) != 4):
            raise ValueError("invalid signing configuration")
        self.metering = config.get("metering", False)
        self.metering_port = config.get("metering_port", 9801)
        if (type(self.metering) is not bool or type(self.metering_port) is not int
                or not 1 <= self.metering_port <= 65535):
            raise ValueError("invalid metering configuration")
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_file(cls, path: str):
        try:
            with open(path, encoding="utf-8") as handle:
                return cls(json.load(handle))
        except (OSError, ValueError, TypeError):
            # The probe may still report load. Its verification endpoint must
            # fail closed until a valid private config is installed/restarted.
            return None

    @staticmethod
    def _header(headers, name: str) -> str:
        if hasattr(headers, "get_all"):
            values = headers.get_all(name) or []
            if len(values) != 1:
                raise ValueError("missing or duplicate verifier header")
            return values[0]
        return headers[name]

    def verify(self, target: str, served_path: str, method: str, now: float | None = None) -> dict | None:
        """Raw request-target plus nginx's served URI must describe one file.

        Query names/values are not URL-decoded: nginx $arg_* uses literal
        bytes. Reject percent-encoded/case aliases and duplicate keys rather
        than authenticate one interpretation and rate-limit another.
        """
        try:
            if (method not in ("GET", "HEAD") or not target.startswith("/")
                    or "#" in target or len(target) > 65536
                    or any(ord(c) < 32 or ord(c) >= 127 for c in target)):
                return None
            raw_path, separator, query = target.partition("?")
            if not separator or re.search(r"%(?![0-9a-fA-F]{2})", raw_path):
                return None
            path = unquote_to_bytes(raw_path).decode("utf-8", "strict")
            if (path != served_path or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or any(p in (".", "..") for p in path.split("/"))):
                return None
            params, seen = {}, set()
            parts = query.split("&")
            if len(parts) > 32:
                return None
            for part in parts:
                key, eq, value = part.partition("=")
                if (not eq or not re.fullmatch(r"[A-Za-z0-9_]+", key)
                        or key.lower() in seen):
                    return None
                seen.add(key.lower())
                params[key] = value
            digest, expiry, rate, utag = (params[self.digest_arg], params[self.expires_arg],
                                        params["r"], params["u"])
            if (not re.fullmatch(r"v2\.[A-Za-z0-9_-]{43}", digest)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,18}", expiry)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,18}", rate)
                    or not re.fullmatch(r"[0-9a-f]{10}|", utag)):
                return None
            expires, rate_bps = int(expiry), int(rate)
            if max(expires, rate_bps) > (1 << 63) - 1:
                return None
            message = json.dumps(["mediadeck.file-url", 2, path, expires, rate_bps, utag],
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            mac = hmac.new(self._secret.encode("utf-8"), message, hashlib.sha256).digest()
            expected = "v2." + base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")
            current = time.time() if now is None else now
            if current >= expires or not hmac.compare_digest(expected, digest):
                return None
            return {"utag": utag, "rate_bps": rate_bps}
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None

    def authorize(self, headers) -> int:
        try:
            # HTTP header decoding is Latin-1; nginx's $uri bytes are UTF-8.
            path = self._header(headers, "X-Mediadeck-Path").encode("latin-1").decode("utf-8")
            result = self.verify(self._header(headers, "X-Mediadeck-Target"), path,
                                 self._header(headers, "X-Mediadeck-Method"))
            if result is None:
                return 403
            if not self.metering:
                return 204
            params = {key: self._header(headers, name) for key, name in (
                ("lip", "X-Mediadeck-Local-Addr"), ("lp", "X-Mediadeck-Local-Port"),
                ("a", "X-Mediadeck-Remote-Addr"), ("p", "X-Mediadeck-Remote-Port"),
                ("cid", "X-Mediadeck-Connection"))}
            params["u"] = result["utag"]  # never an independently asserted tag
        except (ValueError, KeyError, UnicodeError):
            return 403
        request = Request(f"http://127.0.0.1:{self.metering_port}/register?{urlencode(params)}")
        try:
            with self._opener.open(request, timeout=0.5) as response:
                return 204 if response.status in (200, 204) else 403
        except HTTPError as exc:
            # Preserve the existing optional metering transport fail-open,
            # but ONLY after v2 authentication. Deny/mixed identity stay deny.
            return 204 if exc.code in (502, 503, 504) else 403
        except (URLError, OSError, TimeoutError):
            return 204


def default_iface() -> str:
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            for line in fh.readlines()[1:]:
                fields = line.split()
                if fields[1] == "00000000":
                    return fields[0]
    except OSError:
        pass
    return "eth0"




def established_count(ports: set[int]) -> int:
    count = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh.readlines()[1:]:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "01":  # 01 = ESTABLISHED
                        continue
                    local_port = int(fields[1].rsplit(":", 1)[1], 16)
                    if local_port in ports:
                        count += 1
        except (OSError, IndexError, ValueError):
            continue
    return count






class Sampler:
    """Connection capacity only. No byte observations."""

    def __init__(self, iface: str, ports: set[int], speedlog=None) -> None:
        self.iface, self.ports = iface, ports
        self.speedlog = None  # legacy argument ignored; never create a byte sampler
        self.active = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        # (monotonic_ts, tx_bytes) samples covering the trailing window.
        while True:
            active = established_count(self.ports)
            with self._lock:
                self.active = active
            time.sleep(5)

    def snapshot(self) -> dict:
        with self._lock:
            return {'ok': True, 'active_streams': self.active, 'traffic_tracking': False}


class MediaReadiness:
    """Bounded HTTP reads against loopback readers, never FUSE in /load."""

    def __init__(self, config: str, *, start: bool = True):
        self.config = config
        self.ok = not config
        self.checked_at = 0.0
        self.error = "media_probe_pending" if config else ""
        if config and start:
            threading.Thread(target=self._run, daemon=True).start()

    def check(self) -> None:
        try:
            from pathlib import Path
            from urllib.parse import urlsplit
            urls = json.loads(Path(self.config).read_text())
            if not isinstance(urls, list) or not urls:
                raise ValueError("empty media probes")
            for url in urls:
                parsed = urlsplit(url)
                if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                        or parsed.username or parsed.password or parsed.query):
                    raise ValueError("media probe must use credential-free loopback HTTP")
                request = Request(url, headers={"Range": "bytes=0-0"})
                # Never follow redirects: a local endpoint must not make the
                # probe fetch an external URL or authentication endpoint.
                opener = build_opener(ProxyHandler({}), NoRedirect())
                with opener.open(request, timeout=3) as response:
                    if (response.status != 206
                            or not re.fullmatch(r"bytes 0-0/[1-9][0-9]*",
                                                response.headers.get("Content-Range", ""))
                            or len(response.read(2)) != 1):
                        raise ValueError("media byte unavailable")
            self.ok, self.error = True, ""
        except Exception:  # noqa: BLE001 - no media URLs or credentials in diagnostics
            self.ok, self.error = False, "media_read_unavailable"
        self.checked_at = time.time()

    def snapshot(self) -> dict:
        ready = self.ok and (not self.config or time.time() - self.checked_at < 45)
        return {"media_ok": ready, "media_checked_at": self.checked_at,
                "media_error": "" if ready else self.error or "media_probe_stale"}

    def _run(self) -> None:
        while True:
            self.check()
            time.sleep(15)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9800)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--iface", default=default_iface())
    parser.add_argument("--service-ports", default="443,80",
                        help="comma-separated ports counted as streams")
    parser.add_argument("--token", default=os.environ.get("LOADPROBE_TOKEN", ""))
    parser.add_argument("--signing-config", default=os.environ.get(
        "LOADPROBE_SIGNING_CONFIG", "/etc/mediadeck/signing.json"))
    parser.add_argument("--speed-log",
                        default=os.environ.get(
                            "LOADPROBE_SPEED_LOG",
                            "/var/log/nginx/mediadeck-speed.log"),
                        help="nginx mediadeck_speed log; empty disables")
    parser.add_argument("--media-probes", default=os.environ.get("LOADPROBE_MEDIA_PROBES", ""),
                        help="JSON file of loopback reader URLs for real byte readiness")
    args = parser.parse_args()
    readiness = MediaReadiness(args.media_probes)

    ports = {int(p) for p in args.service_ports.split(",") if p.strip()}
    sampler = Sampler(args.iface, ports, None)

    verifier = SigningVerifier.from_file(args.signing_config)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/verify":
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    code = 403
                elif verifier is None:
                    code = 503
                else:
                    code = verifier.authorize(self.headers)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Request-start attribution ping, mirrored by nginx on every media
            # request. Loopback-only in practice (nginx runs on the same box)
            # and carries no secrets: a hashed tag and a socket address.
            if self.path.startswith("/announce"):
                # This credential-free hook is for nginx on this host only.
                # Forwarded headers are not evidence of a local connection.
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    self.send_response(403)
                    self.end_headers()
                    return
                # Old nginx mirrors are acknowledged without collecting identities/bytes.
                self.send_response(204)
                self.end_headers()
                return
            if self.path.rstrip("/") not in ("", "/load", "/healthz"):
                self.send_response(404)
                self.end_headers()
                return
            if args.token:
                auth = self.headers.get("Authorization", "")
                if auth != f"Bearer {args.token}":
                    self.send_response(401)
                    self.end_headers()
                    return
            snapshot = sampler.snapshot()
            media = readiness.snapshot()
            snapshot.update(media)
            if not media["media_ok"]:
                snapshot.update(ok=False, error=media["media_error"])
            body = json.dumps(snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"loadprobe on {args.bind}:{args.port} iface={args.iface} ports={sorted(ports)}")
    server.serve_forever()


if __name__ == "__main__":
    main()
