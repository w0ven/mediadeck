#!/usr/bin/env python3
"""Validate a protected legacy export; write only with explicit --apply.

Run after deploying the account-center schema. Take and verify an online
SQLite backup before --apply; the --backup argument records that prerequisite.
No network calls and no user/credential details are printed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.modules.stats import StatsService, legacy_watch_fingerprint
from app.core.db import Database


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", required=True, type=Path)
    p.add_argument("--export", required=True, type=Path)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup", type=Path)
    a = p.parse_args()
    material = json.loads(a.export.read_text())
    rows = material["rows"]
    source = material["source"]
    if len(rows) > 200000 or legacy_watch_fingerprint(rows) != source:
        raise SystemExit("export_fingerprint_invalid")
    c = sqlite3.connect("file:" + str(a.database.resolve()) + "?mode=ro", uri=True)
    has_totals = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watch_totals'").fetchone()
    cutoff = c.execute("SELECT MIN(first_at) FROM watch_totals" if has_totals else
                       "SELECT MIN(started_at) FROM play_events").fetchone()[0]
    users = {r[0] for r in c.execute("SELECT emby_user_id FROM members")}
    bad = sum(
        r["emby_user_id"] not in users
        or not 0 <= int(r["seconds"]) <= int(r["ended_at"]) - int(r["started_at"])
        or not int(r["started_at"]) < int(r["ended_at"]) <= int(cutoff or 0)
        for r in rows
    )
    if bad or len({r["event_id"] for r in rows}) != len(rows):
        raise SystemExit("scope_overlap_or_duplicate_invalid")
    print(
        json.dumps(
            {
                "mode": "apply" if a.apply else "dry_run",
                "events": len(rows),
                "users": len({r["emby_user_id"] for r in rows}),
                "seconds": sum(r["seconds"] for r in rows),
                "cutoff_at": cutoff,
                "source_sha256": source,
            }
        )
    )
    if not a.apply:
        c.close()
        return
    if not c.execute("SELECT 1 FROM meta WHERE key='watch_totals_seeded'").fetchone():
        raise SystemExit("deploy_account_center_schema_first")
    c.close()
    if not a.backup or not a.backup.is_file() or a.backup.resolve() == a.database.resolve():
        raise SystemExit("verified_separate_backup_required")
    saved = sqlite3.connect("file:" + str(a.backup.resolve()) + "?mode=ro", uri=True)
    if saved.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise SystemExit("backup_integrity_failed")
    if {r[0] for r in saved.execute('SELECT emby_user_id FROM members')} != users:
        raise SystemExit('backup_member_set_mismatch')
    saved.close()
    db = Database(a.database)
    count = StatsService(db).import_legacy_watch(rows, source=source)
    print(json.dumps({"imported_events": count, "members_unchanged": True}))


if __name__ == "__main__":
    main()
