"""Socket lifecycle: collect() closes gone sockets; cid reuse is not mixed identity."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"
PROBE = Path(__file__).resolve().parent / "flowmeter_close_probe.py"


def _load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("flowmeter", AGENT / "flowmeter.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_nginx_cid_change_is_reuse_not_mixed(tmp_path) -> None:
    module = _load()
    meter = module.FlowMeter(str(tmp_path / "fm.db"))
    first = meter.register("127.0.0.1", 443, "10.0.0.1", 50000, "alice",
                           nginx_cid="9")
    second = meter.register("127.0.0.1", 443, "10.0.0.1", 50000, "bob",
                            nginx_cid="10")
    assert first["allow"] is False
    assert second["reason"] != "mixed_identity"
    assert second["ok"] is True
    assert second["conn_id"] != first["conn_id"]
    meter.close()


def test_ss_failure_does_not_mass_close(tmp_path, monkeypatch) -> None:
    module = _load()
    meter = module.FlowMeter(str(tmp_path / "fm.db"), enabled=False)
    got = meter.register("127.0.0.1", 443, "10.0.0.1", 1, "alice")
    monkeypatch.setattr(meter, "_live_tuples", lambda: None)
    meter._enabled = True
    still = meter._tuple_still_live(
        "127.0.0.1", 443, "10.0.0.1", 1, nginx_cid=None,
        stored={"nginx_cid": "", "conn_id": got["conn_id"]})
    assert still is True
    meter.close()


def test_acked_pending_is_deleted(tmp_path) -> None:
    module = _load()
    meter = module.FlowMeter(str(tmp_path / "fm.db"))
    meter.register("127.0.0.1", 443, "10.0.0.1", 1, "alice")
    env = meter.collect()
    assert meter.pending()
    meter.ack(env["boot_id"], env["seq"])
    rows = meter._db.execute("SELECT COUNT(*) AS n FROM pending").fetchone()
    assert int(rows["n"]) == 0
    meter.close()


def test_real_close_then_reuse_in_netns() -> None:
    before = subprocess.check_output(["nft", "list", "tables"], text=True)
    proc = subprocess.run(
        ["unshare", "-n", sys.executable, str(PROBE)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    after = subprocess.check_output(["nft", "list", "tables"], text=True)
    assert before == after
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout[proc.stdout.find("{"):])
    assert report["ok"] is True
