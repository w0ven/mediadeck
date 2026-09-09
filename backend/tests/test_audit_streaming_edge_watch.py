"""Atomic log cursor, archive bounds and sampled-only watch import regressions."""
import gzip
from unittest.mock import Mock

import pytest
from test_audit_streaming_agents import load

from app.core.db import Database
from app.modules.edgelog import TrafficLedger
from app.modules.stats import StatsService, legacy_watch_fingerprint


def test_log_report_cursor_and_bytes_commit_once(tmp_path):
    db = Database(tmp_path / "ledger.db")
    ledger = TrafficLedger(db)
    payload = {"path": "speed.log", "inode": 1, "offset": 100, "previous_offset": 0,
               "lines": ["1800000000 u=tag r=0 100 1.0"]}
    assert ledger.ingest_batch("node-a", payload, {"tag": "u1"})["bytes"] == 100
    assert ledger.ingest_batch("node-a", payload, {"tag": "u1"})["bytes"] == 0
    # Renaming a plain rotation does not create new traffic.
    assert ledger.ingest_batch("node-a", {**payload, "path": "speed.log.1"}, {"tag": "u1"})["bytes"] == 0
    assert ledger.totals_for_users() == {"u1": 100}
    # Explicit compare-and-swap supports copytruncate without replaying a batch.
    smaller = {**payload, "previous_offset": 100, "offset": 30}
    assert ledger.ingest_batch("node-a", smaller, {"tag": "u1"})["bytes"] == 100
    assert ledger.ingest_batch("node-a", smaller, {"tag": "u1"})["bytes"] == 0
    db.close()


def test_log_write_failure_rolls_back_cursor_and_bytes(tmp_path):
    db = Database(tmp_path / "ledger.db")
    db.execute("CREATE TRIGGER fail_cursor BEFORE INSERT ON edge_cursors BEGIN SELECT RAISE(ABORT,'failed'); END")
    ledger = TrafficLedger(db)
    with pytest.raises(Exception, match="failed"):
        ledger.ingest_batch("node-a", {"path": "speed.log", "inode": 1, "offset": 100,
                                       "previous_offset": 0,
                                       "lines": ["1800000000 u=tag r=0 100 1.0"]}, {"tag": "u1"})
    assert not db.query("SELECT * FROM edge_usage_daily")
    assert not db.query("SELECT * FROM edge_cursors")
    db.close()


def test_reporter_never_commits_an_intermediate_unchanged_cursor(tmp_path, monkeypatch):
    agent = load("edgereport")
    monkeypatch.setattr(agent, "BATCH_LINES", 2)
    path = tmp_path / "speed.log"
    path.write_text("1800000000 u=tag r=0 100 1.0\n" * 5)
    monkeypatch.setattr(agent, "cursors_from", lambda *a: {})
    post = Mock(return_value={"ok": True})
    monkeypatch.setattr(agent, "post", post)
    assert agent.run_once("https://panel.example.com", "node-a", "fixture-token", str(path), False) == 0
    assert all(call.args[3]["offset"] > 0 for call in post.call_args_list)
    assert sum(len(call.args[3]["lines"]) for call in post.call_args_list) == 5


def test_oversized_gzip_is_not_silently_acknowledged(tmp_path, monkeypatch):
    agent = load("edgereport")
    monkeypatch.setattr(agent, "MAX_LINES_PER_PASS", 2)
    path = tmp_path / "speed.log.gz"
    with gzip.open(path, "wt") as output:
        output.write("1800000000 u=tag r=0 100 1.0\n" * 3)
    with pytest.raises(ValueError, match="archive"):
        agent.read_since(str(path), 0)


def test_overview_respects_personal_quota_override_and_group_expiry(tmp_path):
    import time

    from app.modules.groups import GroupService
    from app.modules.members import MemberService

    db = Database(tmp_path / "watch.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    groups.update("vip", {"traffic_quota_bytes": 1})
    member = members.upsert("u1", "viewer", {"group_id": "vip"})
    members.set_overrides("u1", {"extra_traffic_bytes": 10})
    db.execute("UPDATE members SET traffic_used_bytes=2,expires_at=? WHERE emby_user_id='u1'", (int(time.time()) + 60,))
    assert member["billing_mode"] == "traffic"
    overview = StatsService(db).overview()
    assert overview["members"]["exhausted"] == 0
    assert overview["expiring_7d"] == []
    db.execute("UPDATE members SET traffic_used_bytes=12 WHERE emby_user_id='u1'")
    assert StatsService(db).overview()["members"]["exhausted"] == 1
    # Measured cutover must not read the old estimate when no measurement exists.
    measured = StatsService(db, measured_cutover=lambda: True)
    assert measured.overview()["members"]["exhausted"] == 0
    month = measured.measured_month()["period"]
    db.execute("INSERT INTO measured_usage_monthly VALUES(?,?,?,?,?,?)", (month, "node-a", "tag", "u1", 12, 0))
    assert measured.overview()["members"]["exhausted"] == 1
    db.execute("INSERT INTO measured_credits VALUES(?,?,?,?)", (month, "u1", 12, time.time()))
    assert measured.overview()["members"]["exhausted"] == 0
    db.close()


def test_sampled_only_history_supplies_safe_legacy_import_boundary(tmp_path):
    db = Database(tmp_path / "watch.db")
    db.execute("INSERT INTO members(emby_user_id,username,created_at,updated_at) VALUES('u1','viewer',1,1)")
    db.execute("INSERT INTO watch_sample_totals VALUES('u1',30,1800000000,1800000030)")
    stats = StatsService(db)
    rows = [{"event_id": "old", "emby_user_id": "u1", "seconds": 20,
             "started_at": 1799999900, "ended_at": 1799999950}]
    assert stats.import_legacy_watch(rows, source=legacy_watch_fingerprint(rows)) == 1
    assert stats.watch_summary("u1")["recorded_seconds"] == 50
    assert stats.import_legacy_watch(rows, source=legacy_watch_fingerprint(rows)) == 0
    db.close()


def test_legacy_cli_dry_run_is_readonly_and_uses_sampled_boundary(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / "watch.db"
    db = Database(path)
    db.execute("INSERT INTO members(emby_user_id,username,created_at,updated_at) VALUES('u1','viewer',1,1)")
    db.execute("INSERT INTO watch_sample_totals VALUES('u1',30,1800000000,1800000030)")
    db.close()
    rows = [{"event_id": "old", "emby_user_id": "u1", "seconds": 20,
             "started_at": 1799999900, "ended_at": 1799999950}]
    material = tmp_path / "export.json"
    material.write_text(json.dumps({"rows": rows, "source": legacy_watch_fingerprint(rows)}))
    before = path.read_bytes()
    command = [sys.executable, str(Path(__file__).resolve().parents[2] / "tools/import-watch-history.py"),
               "--database", str(path), "--export", str(material)]
    result = subprocess.run(command, text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cutoff_at"] == 1800000000
    assert path.read_bytes() == before
    # Applying without a verified separate backup remains refused.
    result = subprocess.run([*command, "--apply"], text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode != 0 and "verified_separate_backup_required" in result.stderr
    assert path.read_bytes() == before
