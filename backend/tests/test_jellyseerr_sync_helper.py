"""Retired integrations are explicit not_applicable, never fake sync success."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/helpers/jellyseerr_sync.py"
spec = importlib.util.spec_from_file_location("jellyseerr_sync", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def retired():
    return {"mode": "not_applicable", "reason": "replaced", "successor": "current-request-watch"}


def invoke(tmp_path, config, *args):
    path = tmp_path / "integration.json"
    if config is not None:
        path.write_text(json.dumps(config))
    result = subprocess.run([sys.executable, str(SCRIPT), "--config", str(path), *args],
                            capture_output=True, text=True, timeout=10, check=False)
    return result.returncode, json.loads(result.stdout), result.stderr


def test_not_applicable_never_reads_settings_or_network(monkeypatch):
    monkeypatch.setattr(helper.urllib.request, "urlopen", MagicMock(side_effect=AssertionError("no network")))
    monkeypatch.setattr(helper.Path, "read_text", MagicMock(side_effect=AssertionError("no old settings")))
    report, code = helper.evaluate(retired())
    assert code == 0
    assert report == {"status": "not_applicable", "reason": "replaced",
                      "successor": "current-request-watch", "attempted_jobs": 0}
    assert "success" not in report and "synced" not in str(report)


def test_real_cli_receipt_and_applicability_exit(tmp_path):
    receipt = tmp_path / "state/receipt.json"
    config = {**retired(), "receipt_file": str(receipt)}
    code, result, stderr = invoke(tmp_path, config)
    assert code == 0 and result["status"] == "not_applicable" and stderr == ""
    stored = json.loads(receipt.read_text())
    assert stored["status"] == "not_applicable" and stored["attempted_jobs"] == 0
    assert receipt.stat().st_mode & 0o777 == 0o600
    code, result, _ = invoke(tmp_path, config, "--applicability-only")
    assert code == 3 and result["status"] == "not_applicable"


@pytest.mark.parametrize("config", [None, {}, {"mode": "unknown"}, {"mode": "not_applicable"},
                                    {"mode": "active", "base_url": "http://localhost:1",
                                     "settings_path": "/nonexistent-synthetic-settings"}])
def test_missing_settings_or_state_not_inferred_retired(tmp_path, config):
    code, result, stderr = invoke(tmp_path, config)
    assert code == 1 and result["status"] == "error"
    assert stderr == "" and "not_applicable" not in str(result)
    assert "nonexistent-synthetic" not in str(result)


def test_active_error_replaces_prior_retired_receipt(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"status": "not_applicable"}))
    code, result, _ = invoke(tmp_path, {"mode": "active", "base_url": "http://localhost:1",
                                        "settings_path": str(tmp_path / "missing"),
                                        "receipt_file": str(receipt)})
    assert code == 1 and result["status"] == "error"
    assert json.loads(receipt.read_text())["status"] == "error"


def test_active_jobs_confirm_request_not_completion(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"main": {"apiKey": "synthetic-credential"}}))
    response = MagicMock()
    response.__enter__.return_value.status = 202
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(helper.urllib.request, "urlopen", urlopen)
    report, code = helper.evaluate({"mode": "active", "base_url": "http://localhost:1",
                                    "settings_path": str(settings)})
    assert code == 0 and report["status"] == "sync_requested"
    assert report["attempted_jobs"] == 2 and urlopen.call_count == 2
    assert "synthetic-credential" not in str(report)


def test_active_transport_failure_nonzero_and_redacted(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"main": {"apiKey": "synthetic-credential"}}))
    monkeypatch.setattr(helper.urllib.request, "urlopen",
                        MagicMock(side_effect=RuntimeError("synthetic-credential?token=secret")))
    report, code = helper.evaluate({"mode": "active", "base_url": "http://localhost:1",
                                    "settings_path": str(settings)})
    assert code == 1 and report["status"] == "failed"
    assert "synthetic-credential" not in str(report) and "token=" not in str(report)
