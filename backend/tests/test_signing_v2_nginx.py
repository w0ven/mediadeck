"""Real TLS nginx -> existing probe -> optional real meterd, inside unshare -n.

No generated installer is run. Every process, nft table and temporary config is
owned by the isolated child; no host firewall or production endpoint is used.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import NodePool, StreamNode
from app.modules.entries import PlaybackEntry, entry_target
from app.modules.entry_proxy import friend_config
from app.modules.provisioning import nginx_site, signing_config
from app.modules.signing import sign_url, user_tag

KEY = "synthetic-v2-nginx-secret"
TAG = user_tag("test-viewer")
BODY = b"0123456789ABCDEF"


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(number, process):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        assert process.poll() is None, "isolated process exited before listen"
        try:
            with socket.create_connection(("127.0.0.1", number), timeout=0.1):
                return
        except OSError:
            time.sleep(0.03)
    raise AssertionError("isolated process did not listen")


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def request(number, target, context=None, method="GET", headers=None):
    conn = (http.client.HTTPSConnection("127.0.0.1", number, context=context, timeout=5)
            if context else http.client.HTTPConnection("127.0.0.1", number, timeout=5))
    try:
        conn.request(method, target, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        conn.close()


def exercise(root, args):
    root.mkdir()
    nginx_bin = shutil.which("nginx")
    node_port, probe_port, meter_port, friend_port = port(), port(), port(), port()
    pool_main, pool_other = root / "media main", root / "archive"
    pool_main.mkdir()
    pool_other.mkdir()
    for relative in ("demo.mkv", "中文 file ?.mkv", "percent%2F#name.mkv", "sub/leaf.mkv"):
        file = pool_main / relative
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(BODY)
    (pool_other / "archive.mkv").write_bytes(BODY)
    cert, keyfile = root / "cert.pem", root / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(keyfile), "-out", str(cert), "-subj", "/CN=node.example.com",
                    "-addext", "subjectAltName=DNS:node.example.com,IP:127.0.0.1"],
                   check=True, capture_output=True, timeout=15)
    context = ssl.create_default_context(cafile=str(cert))
    node = StreamNode(name="node-a", base_url=f"https://node.example.com:{node_port}",
                      probe_url=f"http://127.0.0.1:{probe_port}/load", sign_secret=KEY,
                      sign_arg_digest=args[0], sign_arg_expires=args[1], pools=[
                          NodePool(name="main", emby_prefix="/media", url_prefix="/s/main", node_path=str(pool_main)),
                          NodePool(name="gd3", emby_prefix="/archive", url_prefix="/s/gd3", node_path=str(pool_other))])
    config_path, meter_db, deny = root / "signing.json", root / "meter.db", root / "deny.map"
    deny.write_text("# none\n")
    probe_process = meter_process = nginx_process = None
    logs = []

    def spawn(command, name):
        log = (root / (name + ".log")).open("wb")
        logs.append(log)
        return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)

    def start_probe(metering):
        config = json.loads(signing_config(node, metering=metering))
        config["metering_port"] = meter_port
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        proc = spawn([sys.executable, str(ROOT / "agent/loadprobe.py"), "--bind", "127.0.0.1",
                      "--port", str(probe_port), "--iface", "lo", "--speed-log", "",
                      "--signing-config", str(config_path)], "probe")
        wait_port(probe_port, proc)
        return proc

    def target(path="/s/main/demo.mkv", rate=0, tag=TAG, ttl=3600, now=None):
        url = sign_url(node.base_url, path, KEY, ttl, *args, rate_bps=rate, utag=tag, now=now)
        parsed = urlsplit(url)
        return parsed.path + "?" + parsed.query

    def conn_count():
        with sqlite3.connect(meter_db) as db:
            return db.execute("SELECT COUNT(*) FROM conns").fetchone()[0]

    try:
        meter_process = spawn([sys.executable, str(ROOT / "agent/meterd.py"), "--bind", "127.0.0.1",
                               "--port", str(meter_port), "--persist", str(meter_db),
                               "--deny-map", str(deny), "--interval", "3600", "--enable-nft"], "meter")
        wait_port(meter_port, meter_process)
        probe_process = start_probe(False)
        text = nginx_site(node)
        begin = text.index("server {\n    listen 80;")
        end = text.index("server {\n    # HTTP/1.1", begin)
        text = text[:begin] + text[end:]
        text = text.replace("listen 443 ssl;", f"listen 127.0.0.1:{node_port} ssl;").replace("listen [::]:443 ssl;", "")
        text = text.replace("/etc/letsencrypt/live/node.example.com/fullchain.pem", str(cert)).replace(
            "/etc/letsencrypt/live/node.example.com/privkey.pem", str(keyfile))
        text = text.replace("/var/lib/mediadeck/deny.map", str(deny)).replace(
            "/var/log/nginx/mediadeck-speed.log", str(root / "speed.log"))
        text = text.replace("127.0.0.1:9800", f"127.0.0.1:{probe_port}").replace(
            "127.0.0.1:9801", f"127.0.0.1:{meter_port}")
        entry = {"id": "friend-a", "origin": "https://friend.example.com", "proxy_key": "x" * 43}
        friend = friend_config(entry, "https://unused.example.com", [node], "nginx")
        friend = friend.replace("listen 443 ssl;", f"listen 127.0.0.1:{friend_port};").replace("listen [::]:443 ssl;", "")
        friend = friend.replace("http2 on;", "").replace(f"server node.example.com:{node_port};", f"server 127.0.0.1:{node_port};")
        friend = friend.replace("https://unused.example.com", f"https://127.0.0.1:{node_port}")
        friend = friend.replace("/etc/ssl/certs/ca-certificates.crt", str(cert))
        friend = friend.replace("/etc/letsencrypt/live/friend.example.com/fullchain.pem", str(cert)).replace(
            "/etc/letsencrypt/live/friend.example.com/privkey.pem", str(keyfile))
        nginx_conf = root / "nginx.conf"
        nginx_conf.write_text(f"daemon off;\nuser root;\npid {root}/nginx.pid;\nerror_log {root}/nginx.log;\nevents {{}}\nhttp {{\n{text}\n{friend}\n}}\n")
        subprocess.run([nginx_bin, "-t", "-p", str(root), "-c", str(nginx_conf)], check=True, capture_output=True, timeout=10)
        nginx_process = spawn([nginx_bin, "-p", str(root), "-c", str(nginx_conf)], "nginx-out")
        wait_port(node_port, nginx_process)
        good = target()
        valid_paths = ["/s/main/demo.mkv", "/s/main/中文 file ?.mkv", "/s/main/percent%2F#name.mkv",
                       "/s/main/sub//leaf.mkv", "/s/gd3/archive.mkv"]
        for path in valid_paths:
            raw = target(path)
            assert request(node_port, raw, context)[:2] == (200, BODY), path
            wrapped = entry_target(node.base_url + raw, node.name, PlaybackEntry("friend-a", entry["origin"]))
            p = urlsplit(wrapped)
            assert request(friend_port, p.path + "?" + p.query)[:2] == (200, BODY), path
        assert request(node_port, target("/s/main/sub/leaf.mkv").replace("sub/leaf", "sub%2fleaf"), context)[:2] == (200, BODY)
        assert request(node_port, good, context, "HEAD")[:2] == (200, b"")
        status, body, hdr = request(node_port, good, context, headers={"Range": "bytes=2-5"})
        assert (status, body, hdr.get("Content-Range")) == (206, BODY[2:6], "bytes 2-5/16")
        assert request(node_port, target(rate=125000), context)[:2] == (200, BODY)
        assert request(node_port, target(tag=""), context)[:2] == (200, BODY)
        assert conn_count() == 0, "metering=false must never register or collect"
        tampered = [good[:-1] + ("A" if good[-1] != "A" else "B"),
                    good.replace(f"&{args[1]}=", f"&{args[1]}=1"),
                    good.replace("r=0", "r=125000"), target(rate=125000).replace("r=125000", "r=0"),
                    target(rate=125000).replace("r=125000", "r=125001"), good.replace("u=" + TAG, "u=1234567890"),
                    good.replace("/demo.mkv", "/archive.mkv"), good + "&r=0", good + "&R=0",
                    good + "&u=" + TAG, good + "&" + args[0] + "=invalid", good + "&" + args[1] + "=1",
                    good.replace("r=0", "r=%30"), good.replace("r=0", "%72=0"), good.replace("r=0", "r=00"),
                    good.replace("v2.", "v1."), good.replace("demo.mkv", "sub/../demo.mkv"),
                    good.replace("demo.mkv", "sub/%2e%2e/demo.mkv"), target(now=time.time() - 7200),
                    target("/s/main/percent%2F#name.mkv").replace("%252F", "%2F")]
        expires = int(time.time()) + 3600
        for extra in ("", "0" + TAG):
            digest = base64.urlsafe_b64encode(hashlib.md5(f"{expires}/s/main/demo.mkv{extra} {KEY}".encode()).digest()).decode().rstrip("=")
            tampered.append(f"/s/main/demo.mkv?r=0&u={TAG}&{args[1]}={expires}&{args[0]}={digest}")
        for raw in tampered:
            assert request(node_port, raw, context)[0] in (400, 403), raw.split("?")[0]
        assert request(node_port, good, context, "POST")[0] == 403
        assert request(node_port, tampered[0], context, headers={
            "X-Mediadeck-Target": good, "X-Mediadeck-Path": "/s/main/demo.mkv",
            "X-Mediadeck-Method": "GET"})[0] == 403
        stop(probe_process)
        probe_process = start_probe(True)
        assert request(node_port, good, context)[:2] == (200, BODY)
        assert conn_count() > 0, "metering=true must register the authenticated tuple"
        before = conn_count()
        for raw in tampered:
            assert request(node_port, raw, context)[0] in (400, 403)
        assert conn_count() == before, "invalid signatures must not reach meterd"
        with sqlite3.connect(meter_db) as db:
            db.execute("INSERT INTO denied VALUES(?,?)", (TAG, time.time()))
        assert request(node_port, good, context)[0] == 403
        stop(meter_process)
        assert request(node_port, good, context)[:2] == (200, BODY), "optional meter outage only fails open after valid signature"
        for raw in tampered:
            assert request(node_port, raw, context)[0] in (400, 403)
        stop(probe_process)
        assert request(node_port, good, context)[0] >= 500, "verifier outage must never serve media"
        # Missing private configuration is also an independent security failure.
        config_path.unlink()
        probe_process = spawn([sys.executable, str(ROOT / "agent/loadprobe.py"), "--bind", "127.0.0.1",
                               "--port", str(probe_port), "--iface", "lo", "--speed-log", "",
                               "--signing-config", str(config_path)], "probe-missing")
        wait_port(probe_port, probe_process)
        assert request(node_port, good, context)[0] >= 500
        return {"parameters": list(args), "valid_paths": len(valid_paths), "tamper_cases": len(tampered),
                "metering_off_on_unavailable": True, "verifier_fail_closed": True, "transparent_proxy": True}
    finally:
        stop(nginx_process)
        stop(probe_process)
        stop(meter_process)
        for log in logs:
            log.close()
        subprocess.run(["nft", "delete", "table", "inet", "mediadeck_meter"], capture_output=True, check=False)


def test_real_nginx_v2_and_optional_metering_in_isolated_netns(tmp_path):
    if any(not shutil.which(tool) for tool in ("nginx", "openssl", "nft", "unshare", "ip")):
        pytest.skip("isolated nginx/kernel test tools unavailable")
    support = subprocess.run(["unshare", "-n", "true"], capture_output=True, check=False)
    if support.returncode:
        pytest.skip("network namespaces unavailable")
    before = subprocess.check_output(["nft", "list", "tables"], text=True)
    process = subprocess.Popen(["unshare", "-n", sys.executable, __file__, str(tmp_path), os.readlink("/proc/self/ns/net")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=75)
    except subprocess.TimeoutExpired:
        # Kill the entire self-owned session, not just the parent: otherwise
        # daemons would keep the isolated namespace alive after a timeout.
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    finally:
        assert subprocess.check_output(["nft", "list", "tables"], text=True) == before
    assert process.returncode == 0, stdout + stderr
    evidence = json.loads(stdout)
    assert evidence["tables_after"] == [] and len(evidence["cases"]) == 2


if __name__ == "__main__":
    assert len(sys.argv) == 3 and os.readlink("/proc/self/ns/net") != sys.argv[2], "never run kernel writes in host namespace"
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    reports = [exercise(Path(sys.argv[1]) / name, args) for name, args in (
        ("short-args", ("k", "e")), ("long-args", ("md5", "expires")))]
    tables = subprocess.check_output(["nft", "list", "tables"], text=True).strip().splitlines()
    print(json.dumps({"cases": reports, "tables_after": tables}))
