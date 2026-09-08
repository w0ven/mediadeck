#!/usr/bin/env python3
"""Node metering daemon — register HTTP, report loop, deny list.

Stdlib only. Does not enable nft until ``--enable-nft`` (or METERD_ENABLE=1).
Importing this file never talks to nft or the panel.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from flowmeter import DenyPublisher, FlowMeter

USER_AGENT = "mediadeck-meterd/1.0"


def _post(url: str, token: str, payload: dict, timeout: float = 30.0) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class Meterd:
    def __init__(self, meter: FlowMeter, *, panel: str = "", node: str = "",
                 token: str = "", interval: float = 15.0,
                 publisher: DenyPublisher | None = None) -> None:
        self.meter = meter
        self.panel = panel.rstrip("/")
        self.node = node or meter.node
        self.token = token
        self.interval = max(5.0, float(interval))
        self.publisher = publisher
        self._applied_rev: int | None = None
        self._pending_rev: int | None = None
        self._stop = threading.Event()

    def handle_register(self, qs: dict[str, list[str]],
                        headers: dict[str, str]) -> tuple[int, dict]:
        local_ip = (qs.get("lip") or qs.get("local_ip") or [""])[0]
        local_port = (qs.get("lp") or qs.get("local_port") or [""])[0]
        remote_ip = (qs.get("a") or qs.get("rip") or qs.get("remote_ip") or [""])[0]
        remote_port = (qs.get("p") or qs.get("rp") or qs.get("remote_port") or [""])[0]
        utag = (qs.get("u") or qs.get("utag") or [""])[0]
        cid = (qs.get("cid") or qs.get("connection") or [""])[0]
        if not local_ip:
            local_ip = headers.get("X-Mediadeck-Local-Addr", "")
        if not local_port:
            local_port = headers.get("X-Mediadeck-Local-Port", "")
        if not remote_ip:
            remote_ip = headers.get("X-Real-IP") or headers.get("X-Forwarded-For", "")
        if not remote_port:
            remote_port = headers.get("X-Mediadeck-Remote-Port", "")
        if not utag:
            utag = headers.get("X-Mediadeck-Utag", "")
        try:
            result = self.meter.register(
                local_ip, int(local_port), remote_ip, int(remote_port), utag,
                nginx_cid=cid or None)
        except (TypeError, ValueError) as exc:
            return 400, {"ok": False, "allow": False, "reason": f"bad_tuple:{exc}"}
        if result.get("allow"):
            return 204, result
        if result.get("reason") == "blocked":
            return 403, result
        if result.get("reason") == "not_enabled":
            # Agent up but nft not armed: fail-open, do not invent a ban.
            return 204, result
        return 403, result

    def publish_deny(self, *, force: bool = False) -> dict:
        """Write the deny map and optionally nginx -t/reload. Never claims loaded on failure."""
        tags = self.meter.blocked_tags()
        if self.publisher is None:
            self.meter.write_deny_map()
            return {"ok": True, "changed": True, "reloaded": False, "error": None}
        return self.publisher.publish(tags, force=force)

    def apply_remote_policy(self, last: dict) -> dict:
        """Apply a panel policy reply. Returns publish result; applied only if publish ok."""
        policy = (last or {}).get("policy") or {}
        rev = policy.get("rev")
        if last.get("ok") and policy.get("snapshot"):
            self.meter.apply_policy(
                policy.get("blocked_tags") or last.get("blocked_tags") or [],
                terminate=True, snapshot=True)
            self._pending_rev = int(rev) if rev is not None else None
        elif last.get("ok"):
            self.meter.apply_policy(
                last.get("blocked_tags") or [],
                unblock_tags=last.get("unblock_tags") or [],
                terminate=True, snapshot=False)
            self._pending_rev = None
        else:
            return {"ok": False, "error": "policy_not_ok"}
        published = self.publish_deny()
        if published.get("ok") and self._pending_rev is not None:
            self._applied_rev = self._pending_rev
        elif not published.get("ok"):
            # Keep previous applied_rev. Next successful report retries.
            published["applied"] = False
        return published

    def report_once(self) -> dict:
        env = self.meter.collect()
        env["policy_applied_rev"] = self._applied_rev
        if not self.panel or not self.token:
            return {"ok": False, "reason": "no_panel", "envelope": env}
        url = f"{self.panel}/api/edge/{self.node}/measured"
        pending = self.meter.pending()
        last = {"ok": False}
        for item in pending:
            item["policy_applied_rev"] = self._applied_rev
            try:
                last = _post(url, self.token, item)
            except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                return {"ok": False, "reason": f"post:{type(exc).__name__}",
                        "error": str(exc)[:200]}
            ack = (last or {}).get("ack") or {}
            if last.get("ok") and ack.get("boot_id") and ack.get("seq") is not None:
                self.meter.ack(str(ack["boot_id"]), int(ack["seq"]))
            if last.get("ok"):
                published = self.apply_remote_policy(last)
                last = dict(last)
                last["deny_publish"] = published
        return last

    def loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.report_once()
            except Exception as exc:  # noqa: BLE001
                print(f"report failed: {type(exc).__name__}: {exc}", flush=True)

    def stop(self) -> None:
        self._stop.set()


def serve(meterd: Meterd, bind: str, port: int) -> ThreadingHTTPServer:
    daemon = meterd

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") in ("", "/healthz"):
                body = json.dumps({
                    "ok": True, "enabled": daemon.meter.enabled,
                    "boot_id": daemon.meter.boot_id,
                    "blocked": daemon.meter.blocked_tags(),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path.startswith("/register"):
                qs = parse_qs(parsed.query)
                headers = {k: v for k, v in self.headers.items()}
                code, result = daemon.handle_register(qs, headers)
                if code == 204:
                    self.send_response(204)
                    self.end_headers()
                    return
                body = json.dumps(result).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args) -> None:
            pass

    class ReuseServer(ThreadingHTTPServer):
        allow_reuse_address = True

    server = ReuseServer((bind, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", default=os.environ.get("MEDIADECK_PANEL", ""))
    parser.add_argument("--node", default=os.environ.get("MEDIADECK_NODE", "local"))
    parser.add_argument("--token-file", default=os.environ.get("MEDIADECK_TOKEN_FILE", ""))
    parser.add_argument("--persist", default=os.environ.get(
        "MEDIADECK_METER_DB", "/var/lib/mediadeck/flowmeter.db"))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9801)
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--enable-nft", action="store_true",
                        default=os.environ.get("METERD_ENABLE") == "1")
    parser.add_argument("--deny-map", default=os.environ.get(
        "MEDIADECK_DENY_MAP", "/var/lib/mediadeck/deny.map"))
    parser.add_argument("--nginx-bin", default=os.environ.get("MEDIADECK_NGINX_BIN", ""))
    parser.add_argument("--nginx-conf", default=os.environ.get("MEDIADECK_NGINX_CONF", ""))
    parser.add_argument("--nginx-prefix", default=os.environ.get("MEDIADECK_NGINX_PREFIX", ""))
    args = parser.parse_args()
    token = ""
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
    meter = FlowMeter(args.persist, node=args.node, enabled=args.enable_nft,
                      deny_map_path=args.deny_map)
    publisher = None
    if args.nginx_bin:
        publisher = DenyPublisher(
            args.deny_map, nginx_bin=args.nginx_bin,
            nginx_conf=args.nginx_conf or None,
            nginx_prefix=args.nginx_prefix or None)
    meter.write_deny_map()
    daemon = Meterd(meter, panel=args.panel, node=args.node, token=token,
                    interval=args.interval, publisher=publisher)
    serve(daemon, args.bind, args.port)
    print(f"meterd on {args.bind}:{args.port} nft={meter.enabled}", flush=True)
    try:
        daemon.loop()
    except KeyboardInterrupt:
        daemon.stop()
    return 0


# Imported as a sibling of flowmeter.py when deployed as a single-dir agent.
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
