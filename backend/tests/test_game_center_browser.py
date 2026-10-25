"""Collect the full Node/Playwright member UI suite in the normal pytest CI run.

The suite bridges the shipped Mini App to an isolated real FastAPI/SQLite backend.
ZJH and Hold'em both use real HTTP/SSE snapshots, actions and wallet settlement.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_game_center_member_ui_in_chromium(tmp_path, monkeypatch):
    node = shutil.which("node")
    assert node, "Node is required for the game-center browser suite"
    # Match test_economy_browser.py and CI's Playwright-installed chromium symlink.
    chromium = os.getenv("MEDIADECK_TEST_CHROMIUM") or shutil.which("chromium")
    assert chromium, "Install Chromium or set MEDIADECK_TEST_CHROMIUM"
    # Chromium's Unix socket path has a 108-byte ceiling.
    monkeypatch.setenv("TMPDIR", str(tmp_path.parent))
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/game_center_browser.test.mjs"],
        cwd=backend,
        env={
            **os.environ,
            "MEDIADECK_TEST_PYTHON": sys.executable,
            "MEDIADECK_TEST_CHROMIUM": chromium,
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
