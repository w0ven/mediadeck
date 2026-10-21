"""Agent paths exercised in-process, never host nft/ss."""
import importlib.util
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"


def load(name):
    spec = importlib.util.spec_from_file_location(name, AGENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loadprobe_announce_is_loopback_only(monkeypatch):
    from types import SimpleNamespace

    module = load("loadprobe")
    monkeypatch.setattr(module, "Sampler", lambda iface, ports, log: SimpleNamespace(snapshot=dict))
    monkeypatch.setattr(module, "default_iface", lambda: "lo")
    monkeypatch.setattr("sys.argv", ["loadprobe", "--token", "fixture-token"])
    captured = []
    monkeypatch.setattr(module, "ThreadingHTTPServer", lambda addr, handler: (
        captured.append(handler) or SimpleNamespace(serve_forever=lambda: None)))
    module.main()
    handler = captured[0].__new__(captured[0])
    handler.path = "/announce?a=192.0.2.1&p=40000&u=viewer"
    handler.headers = {"X-Forwarded-For": "127.0.0.1"}
    codes = []
    handler.send_response = codes.append
    handler.end_headers = lambda: None
    handler.client_address = ("192.0.2.2", 50000)
    handler.do_GET()
    assert codes == [403]
    handler.client_address = ("127.0.0.1", 50000)
    handler.do_GET()
    # Old nginx mirrors are only acknowledged: no identity/byte attribution
    # is collected any more (docs/TRAFFIC_RETIREMENT.md).
    assert codes[-1] == 204
    assert not hasattr(module, "SpeedLog")


def test_installer_bootstraps_certificate_before_tls_site_and_has_meter_tools():
    import subprocess

    from app.core.config import StreamNode
    from app.modules.provisioning import install_script

    node = StreamNode(name="node-a", base_url="https://node.example.com", probe_url="http://127.0.0.1/load")
    script = install_script(node, "https://panel.example.com")
    assert script.index("certbot certonly") < script.index("<<'MEDIADECK_NGINX_EOF'")
    apt = next(line for line in script.splitlines() if line.startswith("apt-get install"))
    assert "nftables" in apt and "iproute2" in apt
    assert subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False).returncode == 0
