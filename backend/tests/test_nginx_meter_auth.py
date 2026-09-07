"""Generated nginx config parses, and auth_request register is synchronous."""
from __future__ import annotations

import importlib.util
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from proxy_runtime import NGINX_AVAILABLE, nginx_command, stop_proxy

from app.core.config import NodePool, StreamNode
from app.modules.provisioning import nginx_site

AGENT = Path(__file__).resolve().parents[2] / "agent"


def _load_meterd():
    spec = importlib.util.spec_from_file_location(
        "meterd", AGENT / "meterd.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_meterd_register_http_failopen_and_block(tmp_path: Path) -> None:
    """auth_request talks to meterd over HTTP; blocked is 403, unknown is 204."""
    meterd_mod = _load_meterd()
    flow_spec = importlib.util.spec_from_file_location(
        "flowmeter", AGENT / "flowmeter.py")
    flow = importlib.util.module_from_spec(flow_spec)
    assert flow_spec.loader is not None
    flow_spec.loader.exec_module(flow)
    meter = flow.FlowMeter(str(tmp_path / "fm.db"), node="edge-a", enabled=False)
    meter.apply_policy(["blockedtag"], terminate=False)
    daemon = meterd_mod.Meterd(meter)
    port = _port()
    httpd = meterd_mod.serve(daemon, "127.0.0.1", port)
    try:
        blocked = urllib.request.Request(
            f"http://127.0.0.1:{port}/register?lip=127.0.0.1&lp=443"
            f"&a=10.0.0.2&p=41001&u=blockedtag")
        try:
            urllib.request.urlopen(blocked, timeout=3)
            blocked_code = 200
        except urllib.error.HTTPError as exc:
            blocked_code = exc.code
        assert blocked_code == 403
        ok = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/register?lip=127.0.0.1&lp=443"
            f"&a=10.0.0.2&p=41002&u=othertag", timeout=3)
        assert ok.status == 204
    finally:
        httpd.shutdown()
        meter.close()


def test_generated_site_contains_sync_auth_request_not_as_register() -> None:
    node = StreamNode(
        name="edge-a",
        base_url="https://edge-a.example.com",
        probe_url="http://127.0.0.1:9800/load",
        sign_secret="secret",
        pools=[NodePool(name="p", emby_prefix="/media",
                        node_path="/srv/media", url_prefix="/s")],
    )
    text = nginx_site(node)
    assert "auth_request /_mediadeck/register;" in text
    assert "set $md_u $arg_u;" in text
    assert "set $md_lip $server_addr;" in text
    assert "set $md_lp $server_port;" in text
    assert "set $md_a $remote_addr;" in text
    assert "set $md_p $remote_port;" in text
    assert "lip=$md_lip" in text and "u=$md_u" in text
    # Query on auth_request itself is not expanded on nginx 1.22 and
    # also breaks `location =`.
    assert "auth_request /_mediadeck/register?" not in text
    # Billing stays synchronous; announce is a separate speed ping.
    assert "mirror /_mediadeck/announce;" in text
    assert "include /var/lib/mediadeck/deny.map;" in text
    assert "if ($md_denied)" in text
    assert "set $md_cid $pid.$connection;" in text
    assert "cid=$md_cid" in text


@pytest.mark.skipif(not NGINX_AVAILABLE, reason="nginx binary required")
def test_auth_request_blocks_denied_and_failopens_unknown(tmp_path: Path) -> None:
    meterd_mod = _load_meterd()
    flow_spec = importlib.util.spec_from_file_location(
        "flowmeter", AGENT / "flowmeter.py")
    flow = importlib.util.module_from_spec(flow_spec)
    assert flow_spec.loader is not None
    flow_spec.loader.exec_module(flow)

    meter = flow.FlowMeter(str(tmp_path / "fm.db"), node="edge-a", enabled=False)
    meter.apply_policy(["blockedtag"], terminate=False)
    daemon = meterd_mod.Meterd(meter)
    meter_port = _port()
    httpd = meterd_mod.serve(daemon, "127.0.0.1", meter_port)
    time.sleep(0.1)

    media_port = _port()
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "file.bin").write_bytes(b"hello-media")
    conf_dir = tmp_path / "ngx"
    conf_dir.mkdir()
    (conf_dir / "nginx.conf").write_text(f"""
daemon off;
master_process off;
error_log {conf_dir}/error.log;
pid {conf_dir}/nginx.pid;
events {{}}
http {{
    access_log {conf_dir}/access.log;
    server {{
        listen 127.0.0.1:{media_port};
        location /s/ {{
            set $md_u $arg_u;
            set $md_lip $server_addr;
            set $md_lp $server_port;
            set $md_a $remote_addr;
            set $md_p $remote_port;
            auth_request /_mediadeck/register;
            alias {media_root}/;
        }}
        location = /_mediadeck/register {{
            internal;
            access_log {conf_dir}/auth.log;
            proxy_pass http://127.0.0.1:{meter_port}/register?lip=$md_lip&lp=$md_lp&a=$md_a&p=$md_p&u=$md_u;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_intercept_errors on;
            error_page 502 503 504 =200 /_mediadeck/allow;
        }}
        location = /_mediadeck/allow {{
            internal;
            return 204;
        }}
    }}
}}
""", encoding="utf-8")
    cmd = nginx_command(conf_dir, "-p", str(conf_dir), "-c", str(conf_dir / "nginx.conf"))
    check = subprocess.run([*cmd, "-t"], capture_output=True, text=True,
                            timeout=10, check=False)
    assert check.returncode == 0, check.stderr + check.stdout
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(0.3)
        blocked = urllib.request.Request(
            f"http://127.0.0.1:{media_port}/s/file.bin?u=blockedtag")
        try:
            urllib.request.urlopen(blocked, timeout=3)
            blocked_code = 200
        except urllib.error.HTTPError as exc:
            blocked_code = exc.code
        assert blocked_code == 403
        try:
            ok = urllib.request.urlopen(
                f"http://127.0.0.1:{media_port}/s/file.bin?u=othertag", timeout=3)
            other_code, other_body = ok.status, ok.read()
        except urllib.error.HTTPError as exc:
            other_code, other_body = exc.code, exc.read()
            log = (conf_dir / "error.log").read_text(encoding="utf-8", errors="replace")
            auth = (conf_dir / "auth.log").read_text(encoding="utf-8", errors="replace") if (conf_dir / "auth.log").exists() else ""
            raise AssertionError(
                f"othertag got {other_code} body={other_body!r} err={log[-800:]!r} auth={auth[-800:]!r}"
            ) from exc
        assert other_code == 200
        assert other_body == b"hello-media"
    finally:
        stop_proxy(proc, cmd)
        httpd.shutdown()
        meter.close()


@pytest.mark.skipif(not NGINX_AVAILABLE, reason="nginx binary required")
def test_deny_map_blocks_when_meterd_is_down(tmp_path: Path) -> None:
    """Known deny is an nginx map, not the meterd HTTP process."""
    flow_spec = importlib.util.spec_from_file_location(
        "flowmeter", AGENT / "flowmeter.py")
    flow = importlib.util.module_from_spec(flow_spec)
    assert flow_spec.loader is not None
    flow_spec.loader.exec_module(flow)
    deny = tmp_path / "deny.map"
    meter = flow.FlowMeter(str(tmp_path / "fm.db"), node="edge-a",
                           deny_map_path=str(deny))
    meter.apply_policy(["blockedtag"], terminate=False, snapshot=True)
    assert deny.read_text(encoding="utf-8").find("blockedtag") >= 0
    meter.close()

    media_port = _port()
    dead_port = _port()
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "file.bin").write_bytes(b"hello-media")
    conf_dir = tmp_path / "ngx"
    conf_dir.mkdir()
    (conf_dir / "nginx.conf").write_text(f"""
daemon off;
master_process off;
error_log {conf_dir}/error.log;
pid {conf_dir}/nginx.pid;
events {{}}
http {{
    map $arg_u $md_denied {{
        default 0;
        include {deny};
    }}
    access_log {conf_dir}/access.log;
    server {{
        listen 127.0.0.1:{media_port};
        location /s/ {{
            set $md_u $arg_u;
            if ($md_denied) {{ return 403; }}
            auth_request /_mediadeck/register;
            alias {media_root}/;
        }}
        location = /_mediadeck/register {{
            internal;
            proxy_pass http://127.0.0.1:{dead_port}/register;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_intercept_errors on;
            error_page 502 503 504 =200 /_mediadeck/allow;
        }}
        location = /_mediadeck/allow {{
            internal;
            return 204;
        }}
    }}
}}
""", encoding="utf-8")
    cmd = nginx_command(conf_dir, "-p", str(conf_dir), "-c", str(conf_dir / "nginx.conf"))
    check = subprocess.run([*cmd, "-t"], capture_output=True, text=True,
                            timeout=10, check=False)
    assert check.returncode == 0, check.stderr + check.stdout
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(0.3)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{media_port}/s/file.bin?u=blockedtag", timeout=3)
            blocked_code = 200
        except urllib.error.HTTPError as exc:
            blocked_code = exc.code
        assert blocked_code == 403
        ok = urllib.request.urlopen(
            f"http://127.0.0.1:{media_port}/s/file.bin?u=othertag", timeout=3)
        assert ok.status == 200
        assert ok.read() == b"hello-media"
    finally:
        stop_proxy(proc, cmd)


def _get_code(url: str) -> int:
    try:
        r = urllib.request.urlopen(url, timeout=3)
        return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.skipif(not NGINX_AVAILABLE, reason="nginx binary required")
def test_runtime_deny_map_reload_without_restarting_nginx(tmp_path: Path) -> None:
    """Empty map at start, then apply_policy reloads workers; meterd HTTP may die."""
    from proxy_runtime import NGINX
    flow_spec = importlib.util.spec_from_file_location(
        "flowmeter", AGENT / "flowmeter.py")
    flow = importlib.util.module_from_spec(flow_spec)
    assert flow_spec.loader is not None
    flow_spec.loader.exec_module(flow)
    meterd_mod = _load_meterd()

    deny = tmp_path / "deny.map"
    deny.write_text("# none\n", encoding="utf-8")
    media_port = _port()
    meter_port = _port()
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "file.bin").write_bytes(b"hello-media")
    conf_dir = tmp_path / "ngx"
    conf_dir.mkdir()
    # Master process (needed for -s reload) drops to nobody; pytest tmp is 0700.
    for path in (tmp_path, media_root, conf_dir, deny.parent):
        path.chmod(0o755)
    (media_root / "file.bin").chmod(0o644)
    deny.chmod(0o644)
    (conf_dir / "nginx.conf").write_text(f"""
daemon off;
user root;
error_log {conf_dir}/error.log;
pid {conf_dir}/nginx.pid;
events {{}}
http {{
    map $arg_u $md_denied {{
        default 0;
        include {deny};
    }}
    server {{
        listen 127.0.0.1:{media_port};
        location /s/ {{
            set $md_u $arg_u;
            if ($md_denied) {{ return 403; }}
            auth_request /_mediadeck/register;
            alias {media_root}/;
        }}
        location = /_mediadeck/register {{
            internal;
            proxy_pass http://127.0.0.1:{meter_port}/register?u=$md_u&lip=127.0.0.1&lp={media_port}&a=$remote_addr&p=$remote_port;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_connect_timeout 300ms;
            proxy_read_timeout 500ms;
            proxy_intercept_errors on;
            error_page 502 503 504 =200 /_mediadeck/allow;
        }}
        location = /_mediadeck/allow {{
            internal;
            return 204;
        }}
    }}
}}
""", encoding="utf-8")
    cmd = nginx_command(conf_dir, "-p", str(conf_dir), "-c", str(conf_dir / "nginx.conf"))
    check = subprocess.run([*cmd, "-t"], capture_output=True, text=True,
                            timeout=10, check=False)
    assert check.returncode == 0, check.stderr + check.stdout
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    meter = flow.FlowMeter(str(tmp_path / "fm.db"), node="edge-a",
                           deny_map_path=str(deny))
    publisher = flow.DenyPublisher(
        str(deny), nginx_bin=NGINX, nginx_conf=str(conf_dir / "nginx.conf"),
        nginx_prefix=str(conf_dir))
    daemon = meterd_mod.Meterd(meter, publisher=publisher)
    httpd = meterd_mod.serve(daemon, "127.0.0.1", meter_port)
    url = f"http://127.0.0.1:{media_port}/s/file.bin?u=blockedtag"
    other = f"http://127.0.0.1:{media_port}/s/file.bin?u=othertag"
    try:
        time.sleep(0.4)
        assert _get_code(url) == 200
        assert _get_code(other) == 200

        published = daemon.apply_remote_policy({
            "ok": True,
            "policy": {"rev": 2, "blocked_tags": ["blockedtag"], "snapshot": True},
        })
        assert published["ok"] is True
        assert published["reloaded"] is True
        time.sleep(0.3)
        assert _get_code(url) == 403
        assert _get_code(other) == 200

        again = daemon.publish_deny()
        assert again["ok"] is True
        assert again["changed"] is False
        assert again["reloaded"] is False
        assert publisher.reload_count == 1

        httpd.shutdown()
        httpd.server_close()
        time.sleep(0.3)
        assert _get_code(url) == 403
        assert _get_code(other) == 200

        httpd = meterd_mod.serve(daemon, "127.0.0.1", meter_port)
        cleared = daemon.apply_remote_policy({
            "ok": True,
            "policy": {"rev": 3, "blocked_tags": [], "snapshot": True},
        })
        assert cleared["ok"] is True
        assert cleared["reloaded"] is True
        time.sleep(0.3)
        assert _get_code(url) == 200
        assert daemon._applied_rev == 3
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except OSError:
            pass
        stop_proxy(proc, cmd)
        meter.close()


@pytest.mark.skipif(not NGINX_AVAILABLE, reason="nginx binary required")
def test_invalid_deny_map_does_not_claim_loaded(tmp_path: Path) -> None:
    from proxy_runtime import NGINX
    flow_spec = importlib.util.spec_from_file_location(
        "flowmeter", AGENT / "flowmeter.py")
    flow = importlib.util.module_from_spec(flow_spec)
    assert flow_spec.loader is not None
    flow_spec.loader.exec_module(flow)
    deny = tmp_path / "deny.map"
    deny.write_text("# none\n", encoding="utf-8")
    conf_dir = tmp_path / "ngx"
    conf_dir.mkdir()
    (conf_dir / "nginx.conf").write_text(f"""
daemon off;
user root;
error_log {conf_dir}/error.log;
pid {conf_dir}/nginx.pid;
events {{}}
http {{
    map $arg_u $md_denied {{
        default 0;
        include {deny};
    }}
    server {{ listen 127.0.0.1:{_port()}; return 204; }}
}}
""", encoding="utf-8")
    cmd = nginx_command(conf_dir, "-p", str(conf_dir), "-c", str(conf_dir / "nginx.conf"))
    check = subprocess.run([*cmd, "-t"], capture_output=True, text=True,
                            timeout=10, check=False)
    assert check.returncode == 0, check.stderr
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(0.3)
        pub = flow.DenyPublisher(
            str(deny), nginx_bin=NGINX, nginx_conf=str(conf_dir / "nginx.conf"),
            nginx_prefix=str(conf_dir))
        ok = pub.publish([])
        assert ok["ok"] is True
        orig = flow.DenyPublisher.render
        flow.DenyPublisher.render = staticmethod(lambda tags: "not a map {\n")
        try:
            bad = pub.publish(["x"], force=True)
        finally:
            flow.DenyPublisher.render = orig
        assert bad["ok"] is False
        assert bad["reloaded"] is False
        assert "nginx_t" in (bad.get("error") or "")
        assert deny.read_text(encoding="utf-8") == "# none\n"
    finally:
        stop_proxy(proc, cmd)
