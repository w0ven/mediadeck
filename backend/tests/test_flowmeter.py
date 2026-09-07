"""FlowMeter identity and spool — no nft, so host tables stay untouched."""
from __future__ import annotations

import importlib.util
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent" / "flowmeter.py"


def _load():
    spec = importlib.util.spec_from_file_location("flowmeter", AGENT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_import_and_construct_do_not_enable(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=8.0):
        calls.append(cmd)
        return 0, "", ""

    module = _load()
    monkeypatch.setattr(module, "_run", fake_run)
    meter = module.FlowMeter(str(tmp_path / "fm.db"), node="n1")
    assert meter.enabled is False
    got = meter.register("127.0.0.1", 18080, "127.0.0.1", 41001, "alice")
    assert got["ok"] is True
    assert got["allow"] is False
    assert got["reason"] == "not_enabled"
    assert calls == []
    meter.close()


def test_mixed_identity_on_live_tuple_is_refused(tmp_path) -> None:
    module = _load()
    meter = module.FlowMeter(str(tmp_path / "fm.db"))
    first = meter.register("127.0.0.1", 443, "10.0.0.1", 50000, "alice")
    second = meter.register("127.0.0.1", 443, "10.0.0.1", 50000, "bob")
    assert first["allow"] is False
    assert second["ok"] is False
    assert second["reason"] == "mixed_identity"
    assert second["allow"] is False
    meter.close()


def test_tuple_reuse_after_close_mints_new_conn_id(tmp_path) -> None:
    module = _load()
    meter = module.FlowMeter(str(tmp_path / "fm.db"))
    first = meter.register("127.0.0.1", 443, "10.0.0.1", 50000, "alice")
    meter.mark_closed(first["conn_id"])
    second = meter.register("127.0.0.1", 443, "10.0.0.1", 50000, "alice")
    assert second["ok"] is True
    assert second["conn_id"] != first["conn_id"]
    meter.close()


def test_pending_survives_restart_and_ack_clears(tmp_path) -> None:
    module = _load()
    path = str(tmp_path / "fm.db")
    meter = module.FlowMeter(path, node="n1")
    meter.register("127.0.0.1", 443, "10.0.0.1", 1, "alice")
    env = meter.collect()
    assert env["nft_ok"] is False
    assert env["boot_id"] == meter.boot_id
    boot, seq = env["boot_id"], env["seq"]
    meter.close()

    again = module.FlowMeter(path, node="n1")
    assert again.boot_id != boot
    pending = again.pending()
    assert len(pending) == 1
    assert pending[0]["boot_id"] == boot
    assert pending[0]["seq"] == seq
    assert again.ack(boot, seq) == 1
    assert again.pending() == []
    again.close()
