#!/usr/bin/env python3
"""Legacy meterd service name, now only a playback access-policy guard.

No FlowMeter, byte sampling, report spool, nft calls or traffic database writes.
Signature verification and aggregate bandwidth pacing remain in their own agents.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class DenyPublisher:
    """Atomic write + optional nginx -t/reload of the dedicated deny map.

    Reload is off unless ``nginx_bin`` is set. Same body hash is a no-op.
    A failed -t restores the previous map. A failed reload leaves the new
    file (it is already valid) and reports not-applied so the next cycle
    retries the signal.
    """

    def __init__(self, path: str, *, nginx_bin: str | None = None,
                 nginx_conf: str | None = None, nginx_prefix: str | None = None
                 ) -> None:
        self.path = path
        self.nginx_bin = nginx_bin
        self.nginx_conf = nginx_conf
        self.nginx_prefix = nginx_prefix
        self.last_hash: str | None = None
        self.last_error: str | None = None
        self.reload_count = 0

    @staticmethod
    def render(tags: list[str] | set[str]) -> str:
        cleaned = sorted({str(t).strip() for t in tags if str(t).strip()})
        return "".join(f'"{tag}" 1;\n' for tag in cleaned) or "# none\n"

    @staticmethod
    def digest(body: str) -> str:
        return hashlib.sha256(body.encode()).hexdigest()

    @staticmethod
    def write_file(path: str, body: str) -> None:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)

    def _nginx_cmd(self, *extra: str) -> list[str]:
        cmd = [self.nginx_bin or "nginx"]
        if self.nginx_prefix:
            cmd.extend(["-p", self.nginx_prefix])
        if self.nginx_conf:
            cmd.extend(["-c", self.nginx_conf])
        cmd.extend(extra)
        return cmd

    def publish(self, tags: list[str], *, force: bool = False) -> dict[str, Any]:
        body = self.render(tags)
        digest = self.digest(body)
        if not force and self.last_hash == digest:
            return {"ok": True, "changed": False, "reloaded": False,
                    "hash": digest, "error": None, "reload_count": self.reload_count}
        previous = None
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                previous = fh.read()
        try:
            self.write_file(self.path, body)
        except OSError as exc:
            self.last_error = f"write:{exc}"
            return {"ok": False, "changed": False, "reloaded": False,
                    "hash": digest, "error": self.last_error,
                    "reload_count": self.reload_count}
        if not self.nginx_bin:
            self.last_hash = digest
            self.last_error = None
            return {"ok": True, "changed": True, "reloaded": False,
                    "hash": digest, "error": None, "reload_count": self.reload_count}
        check = subprocess.run(self._nginx_cmd("-t"), capture_output=True,
                               text=True, timeout=10, check=False)
        if check.returncode != 0:
            if previous is not None:
                self.write_file(self.path, previous)
            else:
                try:
                    os.remove(self.path)
                except OSError:
                    pass
            err = (check.stderr or check.stdout or "nginx -t failed")[-400:]
            self.last_error = f"nginx_t:{err}"
            return {"ok": False, "changed": False, "reloaded": False,
                    "hash": digest, "error": self.last_error,
                    "reload_count": self.reload_count}
        reload = subprocess.run(self._nginx_cmd("-s", "reload"),
                                capture_output=True, text=True, timeout=10,
                                check=False)
        if reload.returncode != 0:
            err = (reload.stderr or reload.stdout or "nginx reload failed")[-400:]
            self.last_error = f"nginx_reload:{err}"
            return {"ok": False, "changed": True, "reloaded": False,
                    "hash": digest, "error": self.last_error,
                    "reload_count": self.reload_count}
        self.last_hash = digest
        self.last_error = None
        self.reload_count += 1
        return {"ok": True, "changed": True, "reloaded": True,
                "hash": digest, "error": None, "reload_count": self.reload_count}

class AccessPolicy:
    """Only access identities/deny state. No byte counters, nft, or traffic DB writes."""
    def __init__(self, state_path, deny_path, publisher=None, legacy_path=None):
        self.path, self.deny_path, self.publisher = state_path, deny_path, publisher
        self.lock = threading.RLock()
        self.blocked, self.connections = set(), {}
        if os.path.exists(state_path):
            with open(state_path, encoding='utf-8') as f:
                data = json.load(f)
            self.blocked = set(data['blocked'])
            self.connections = {tuple(r['key']): r['tag'] for r in data['connections']}
        else:
            if os.path.exists(deny_path):
                with open(deny_path, encoding='utf-8') as f:
                    self.blocked = set(re.findall(r'^"([A-Za-z0-9_-]+)"\s+1;', f.read(), re.M))
            # Preserve the old register/kill safety on the first transition. Read identities only.
            if legacy_path and os.path.exists(legacy_path):
                import sqlite3
                from pathlib import Path
                db = sqlite3.connect(Path(legacy_path).resolve().as_uri()+'?mode=ro')
                try:
                    db.execute('PRAGMA query_only=ON')
                    for lip, lp, rip, rp, tag, cid in db.execute(
                            'SELECT local_ip,local_port,remote_ip,remote_port,utag,nginx_cid FROM conns WHERE closed_at IS NULL'):
                        self.connections[(lip, str(lp), rip, str(rp), str(cid or ''))] = tag
                finally:
                    db.close()

    def _save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = self.path+'.tmp'
        fd = os.open(tmp, os.O_CREAT|os.O_WRONLY|os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'blocked':sorted(self.blocked), 'connections':[
                {'key':list(key), 'tag':tag} for key,tag in self.connections.items()]}, f)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def terminate(key):
        lip, lp, rip, rp = key[:4]
        def address(ip, port):
            return f'[{ip}]:{port}' if ':' in ip else f'{ip}:{port}'
        subprocess.run(['ss','-K','src',address(lip,lp),'dst',address(rip,rp)],
                       capture_output=True, timeout=5, check=False)

    def register(self, qs):
        tag = str((qs.get('u') or [''])[0])
        if tag and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', tag):
            return 400
        try:
            lip = str(ipaddress.ip_address(qs['lip'][0]))
            rip = str(ipaddress.ip_address(qs['a'][0]))
            lp, rp = int(qs['lp'][0]), int(qs['p'][0])
            if not 0 < lp < 65536 or not 0 < rp < 65536:
                return 400
            key = (lip, str(lp), rip, str(rp), str((qs.get('cid') or [''])[0]))
        except (KeyError, ValueError, IndexError):
            return 400
        with self.lock:
            if tag in self.blocked:
                return 403
            previous = self.connections.get(key)
            if previous is not None and previous != tag:
                self.terminate(key)
                return 403
            # nginx connection ID differentiates reuse of an address/port tuple.
            for old in [old for old in self.connections if old[:4] == key[:4] and old != key]:
                del self.connections[old]
            self.connections[key] = tag
            self._save()
        return 204

    def prune_connections(self):
        """Keep only live socket identities; never request TCP byte counters."""
        with self.lock:
            result = subprocess.run(['ss', '-Htn', 'state', 'established'],
                                    capture_output=True, text=True, timeout=5, check=False)
            if result.returncode:
                return
            live = set()
            for line in result.stdout.splitlines():
                endpoints = [p for p in line.split() if ':' in p]
                if len(endpoints) < 2:
                    continue
                try:
                    pair = []
                    for value in endpoints[-2:]:
                        host, port = value.rsplit(':', 1)
                        pair.extend((str(ipaddress.ip_address(host.strip('[]'))), str(int(port))))
                    live.add(tuple(pair))
                except ValueError:
                    continue
            self.connections = {key: tag for key, tag in self.connections.items() if key[:4] in live}
            self._save()

    def apply(self, body):
        policy = body.get('policy') or {}
        if not body.get('ok') or policy.get('snapshot') is not True:
            raise ValueError('authoritative_snapshot_required')
        tags = policy.get('blocked_tags')
        if not isinstance(tags,list) or any(not isinstance(t,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',t) for t in tags):
            raise ValueError('invalid_policy')
        with self.lock:
            wanted = set(tags)
            for key, tag in list(self.connections.items()):
                if tag in wanted:
                    self.terminate(key)
            self.blocked = wanted
            self._save()
            return self.publisher.publish(sorted(wanted)) if self.publisher else {'ok': True}


def serve(policy, bind, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                self.send_response(403); self.end_headers(); return
            route = urlparse(self.path)
            if route.path.rstrip('/') in ('', '/healthz'):
                body = json.dumps({'ok':True, 'mode':'access_policy_only', 'traffic_tracking':False}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            elif route.path == '/register':
                code = policy.register(parse_qs(route.query, keep_blank_values=True))
                self.send_response(code); self.send_header('Content-Length','0'); self.end_headers()
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'policy redirect refused', headers, fp)


def fetch_policy(panel, node, token):
    request = urllib.request.Request(panel.rstrip('/')+'/api/edge/'+urllib.parse.quote(node,safe='')+'/policy',
                                     headers={'Authorization':'Bearer '+token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=15) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description='Access-policy guard; traffic accounting retired')
    parser.add_argument('--panel', default=os.environ.get('MEDIADECK_PANEL',''))
    parser.add_argument('--node', default=os.environ.get('MEDIADECK_NODE','local'))
    parser.add_argument('--token-file', default=os.environ.get('MEDIADECK_TOKEN_FILE',''))
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9801)
    parser.add_argument('--interval', type=float, default=15)
    parser.add_argument('--deny-map', default='/var/lib/mediadeck/deny.map')
    parser.add_argument('--state-file', default='/var/lib/mediadeck/access-policy.json')
    parser.add_argument('--nginx-bin', default='')
    parser.add_argument('--nginx-conf', default='')
    parser.add_argument('--nginx-prefix', default='')
    parser.add_argument('--persist', default='/var/lib/mediadeck/flowmeter.db', help='Read old connection identities once, never write the traffic DB')
    parser.add_argument('--enable-nft', action='store_true', help='Retired compatibility argument; never enables nft')
    args = parser.parse_args()
    with open(args.token_file, encoding='utf-8') as f:
        token = f.read().strip()
    publisher = DenyPublisher(args.deny_map, nginx_bin=args.nginx_bin or None,
                              nginx_conf=args.nginx_conf or None, nginx_prefix=args.nginx_prefix or None)
    policy = AccessPolicy(args.state_file, args.deny_map, publisher, args.persist)
    serve(policy, args.bind, args.port)
    print('Access policy guard running; byte accounting/reporting disabled.', flush=True)
    try:
        while True:
            try:
                policy.prune_connections()
                policy.apply(fetch_policy(args.panel,args.node,token))
            except Exception as exc:
                print('Access policy refresh failed: '+type(exc).__name__, flush=True)
            time.sleep(max(5,args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
