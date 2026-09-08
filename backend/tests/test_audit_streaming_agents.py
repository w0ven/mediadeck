"""Agent paths exercised with in-memory kernel doubles, never host nft/ss."""
import importlib.util
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[2] / "agent"


def load(name):
    spec = importlib.util.spec_from_file_location(name, AGENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    module = load("flowmeter")
    meter = module.FlowMeter(str(tmp_path / "flows.db"))
    readings = {}
    monkeypatch.setattr(module, "_run", lambda *a, **kw: pytest.fail("unexpected kernel command"))
    monkeypatch.setattr(meter, "_ensure_table", lambda: None)
    monkeypatch.setattr(meter, "_read_all", lambda: dict(readings))
    monkeypatch.setattr(meter, "_live_tuples", lambda: set(readings))

    def key(row):
        return module.format_tuple(row["local_ip"], row["local_port"], row["remote_ip"], row["remote_port"])

    monkeypatch.setattr(meter, "_add_element", lambda row: readings.setdefault(key(row), {"bytes": 0, "packets": 0}))
    monkeypatch.setattr(meter, "_delete_element", lambda row: readings.pop(key(row), None))
    meter.enable()
    yield meter, readings, module
    meter.close()


def register(meter, tag="viewer-a", cid="1.1"):
    return meter.register("127.0.0.1", 443, "192.0.2.1", 40000, tag, nginx_cid=cid)


def test_reused_tuple_never_credits_new_counter_to_old_identity(kernel):
    meter, readings, _module = kernel
    first = register(meter)
    key = next(iter(readings))
    readings[key] = {"bytes": 100, "packets": 1}
    before = meter.collect()
    meter.ack(before["boot_id"], before["seq"])
    # Uncollected final bytes still belong to the original connection.
    readings[key] = {"bytes": 130, "packets": 2}
    second = register(meter, "viewer-b", "1.2")
    readings[key] = {"bytes": 40, "packets": 1}
    final = meter.collect()
    by_id = {s["conn_id"]: s for s in final["samples"]}
    assert by_id[first["conn_id"]]["counter_bytes"] == 130
    assert by_id[first["conn_id"]]["generation"] == 1
    assert by_id[second["conn_id"]]["counter_bytes"] == 40
    meter.ack(final["boot_id"], final["seq"])
    assert key in readings, "old settlement must not delete a new live element"
    readings[key] = {"bytes": 60, "packets": 2}
    sample = meter.collect()["samples"]
    assert len(sample) == 1 and sample[0]["conn_id"] == second["conn_id"]
    assert sample[0]["counter_bytes"] == 60 and sample[0]["generation"] == 1


def test_open_sample_ack_after_close_does_not_settle_unreported_bytes(kernel):
    meter, readings, _module = kernel
    conn = register(meter)
    key = next(iter(readings))
    readings[key] = {"bytes": 100, "packets": 1}
    earlier = meter.collect()
    readings[key] = {"bytes": 150, "packets": 2}
    meter.mark_closed(conn["conn_id"])
    meter.ack(earlier["boot_id"], earlier["seq"])
    assert not meter.connections()[0]["settled"]
    assert key in readings
    final = meter.collect()
    assert final["samples"][0]["counter_bytes"] == 150
    meter.ack(final["boot_id"], final["seq"])
    assert meter.connections()[0]["settled"] and not readings


def test_nft_restart_reconciles_rules_atomically_without_resetting_elements(tmp_path, monkeypatch):
    module = load("flowmeter")
    calls = []
    monkeypatch.setattr(module, "_run", lambda cmd, **kw: (calls.append(cmd) or (0, "", "")))
    meter = module.FlowMeter(str(tmp_path / "flows.db"))
    meter.enable()
    meter.enable()
    # Only the dedicated chain is replaced, atomically; table/set flushes
    # would destroy the watermark carried by established sockets.
    transactions = [c for c in calls if any("flush chain" in arg for arg in c)]
    assert len(transactions) == 2
    assert all("add rule" in " ".join(c) for c in transactions)
    assert not any("flush table" in " ".join(c) or "flush set" in " ".join(c) for c in calls)
    meter.close()


def test_loadprobe_announce_is_loopback_only(monkeypatch):
    from types import SimpleNamespace

    module = load("loadprobe")
    learned = []
    monkeypatch.setattr(module, "SpeedLog", lambda *a: SimpleNamespace(learn=lambda *a: learned.append(a)))
    monkeypatch.setattr(module, "Sampler", lambda iface, ports, log: SimpleNamespace(speedlog=log))
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
    assert codes == [403] and not learned
    handler.client_address = ("127.0.0.1", 50000)
    handler.do_GET()
    assert codes[-1] == 204 and len(learned) == 1


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


def test_exact_speed_owner_expires_instead_of_attributing_reused_port(monkeypatch):
    module = load("loadprobe")
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    clock = 1800000000
    monkeypatch.setattr(module.time, "time", lambda: clock)
    log = module.SpeedLog("")
    log._sock_owners["192.0.2.1:40000"] = ("old-viewer", clock - log.OWNER_TTL - 1)
    log._conns["192.0.2.1:40000"] = [(clock - 1, 100), (clock, 200)]
    assert log.speeds() == {}


def test_log_timestamp_millisecond_rounding_does_not_lose_shared_ip_owner(monkeypatch):
    module = load("loadprobe")
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    clock = 1800000000.0
    monkeypatch.setattr(module.time, "time", lambda: clock)
    log = module.SpeedLog("")
    log._sock_owners["192.0.2.1:40000"] = ("viewer", clock + 0.0004)
    log._owners["192.0.2.1"] = {"viewer": clock, "other-viewer": clock}
    log._conns["192.0.2.1:40000"] = [(clock - 1, 100), (clock, 200)]
    assert log.speeds() == {"viewer": 100}


def test_ipv6_announced_identity_matches_ss_bracketed_socket(monkeypatch):
    module = load("loadprobe")
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    clock = 1800000000
    monkeypatch.setattr(module.time, "time", lambda: clock)
    log = module.SpeedLog("")
    log.learn("2001:db8::1", "40000", "viewer")
    log._conns["[2001:db8::1]:40000"] = [(clock - 1, 100), (clock, 200)]
    assert log.speeds() == {"viewer": 100}


@pytest.mark.parametrize("samples", [[(1800000000 - 60, 100), (1800000000, 200)],
                                     [(1800000000 - 1, 100), (1800000000, 10)]])
def test_unknown_socket_interval_is_not_a_fresh_rate(monkeypatch, samples):
    module = load("loadprobe")
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(module.time, "time", lambda: 1800000000)
    log = module.SpeedLog("")
    log.learn("192.0.2.1", "40000", "viewer")
    log._conns["192.0.2.1:40000"] = samples
    assert "viewer" not in log.speeds()
