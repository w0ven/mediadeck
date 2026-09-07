"""Run the real FlowMeter API inside unshare -n. Host nft is read-only."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parent / "flowmeter_netns_probe.py"


def _nft_tables() -> str:
    return subprocess.check_output(["nft", "list", "tables"], text=True)


def test_flowmeter_kernel_two_conn_in_netns() -> None:
    before = _nft_tables()
    proc = subprocess.run(
        ["unshare", "-n", sys.executable, str(PROBE)],
        capture_output=True, text=True, timeout=40, check=False,
    )
    after = _nft_tables()
    assert before == after, "host nft tables changed"
    assert "table ip filter" in after
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    # Last JSON object in stdout.
    text = proc.stdout.strip()
    start = text.find("{")
    report = json.loads(text[start:])
    assert report["ok"] is True
    assert report["verdict"]["unreg_not_counted"] is True
    assert report["verdict"]["ipv6"] is True
    assert report["tables_after"] == []
