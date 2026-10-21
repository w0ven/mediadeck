"""Overview expiry and sampled-only watch import regressions.

The byte-log cursor/ledger regressions that lived here covered retired traffic
accounting (docs/TRAFFIC_RETIREMENT.md) and were removed with it.
"""
from app.core.db import Database
from app.modules.stats import StatsService, legacy_watch_fingerprint


def test_overview_ignores_retired_quota_and_respects_group_expiry(tmp_path):
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
    # The seeded vip group is a never-expiring, non-time group.
    assert member["billing_mode"] == "none"
    overview = StatsService(db).overview()
    assert overview["members"]["exhausted"] == 0
    # A stored expiry on a non-time group is not a pending expiry.
    assert overview["expiring_7d"] == []
    # Archived estimates and measured rows never exhaust anyone any more.
    db.execute("UPDATE members SET traffic_used_bytes=12 WHERE emby_user_id='u1'")
    assert StatsService(db).overview()["members"]["exhausted"] == 0
    measured = StatsService(db, measured_cutover=lambda: True)
    db.execute("INSERT INTO measured_usage_monthly VALUES(?,?,?,?,?,?)", ("2020-01", "node-a", "tag", "u1", 12, 0))
    result = measured.overview()
    assert result["members"]["exhausted"] == 0 and result["members"]["active"] == 1
    assert result["traffic_tracking"] is False and "traffic" not in result
    db.close()


def test_sampled_only_history_supplies_safe_legacy_import_boundary(tmp_path):
    db = Database(tmp_path / "watch.db")
    db.execute("INSERT INTO members(emby_user_id,username,created_at,updated_at) VALUES('u1','viewer',1,1)")
    db.execute("INSERT INTO watch_sample_totals VALUES('u1',30,1800000000,1800000030)")
    stats = StatsService(db)
    rows = [{"event_id": "old", "emby_user_id": "u1", "seconds": 20,
             "started_at": 1799999900, "ended_at": 1799999950}]
    assert stats.import_legacy_watch(rows, source=legacy_watch_fingerprint(rows)) == 1
    assert stats.watch_summary('u1')['recorded_seconds'] == 50
    assert stats.watch_summary('u1')['historical_unverified_seconds'] == 50
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
