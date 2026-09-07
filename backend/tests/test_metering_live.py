"""Decisive isolated path: two nodes, live restart, interrupt, old URL deny."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parent / "metering_live_probe.py"


def _nft_tables() -> str:
    return subprocess.check_output(["nft", "list", "tables"], text=True)


def test_two_node_quota_path_in_netns() -> None:
    before = _nft_tables()
    proc = subprocess.run(
        ["unshare", "-n", sys.executable, str(PROBE)],
        capture_output=True, text=True, timeout=40, check=False,
    )
    after = _nft_tables()
    assert before == after, "host nft tables changed"
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    text = proc.stdout.strip()
    report = json.loads(text[text.find("{"):])
    assert report["ok"] is True
    assert report["tables_after"] == []
