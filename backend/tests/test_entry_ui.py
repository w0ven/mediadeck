"""Exercise the real settings JavaScript in a disposable Chromium profile."""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


@pytest.mark.skipif(not CHROMIUM, reason="Chromium is needed for the browser regression")
def test_entry_management_browser(tmp_path):
    tests = Path(__file__).parent
    shutil.copyfile(tests.parent / "app/static/app.js", tmp_path / "app.js")
    shutil.copyfile(tests / "entry_ui_browser.js", tmp_path / "test.js")
    page = tmp_path / "test.html"
    page.write_text("""<!doctype html><meta charset="utf-8">
<div id="ee-list"></div><div id="ee-out"></div>
<button id="ee-add">Add</button><input id="ee-id"><input id="ee-origin">
<select id="ee-server"><option value="nginx">nginx</option></select>
<pre id="report">pending</pre>
<script src="app.js"></script><script src="test.js"></script>
""")
    result = subprocess.run([
        CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu",
        "--disable-background-networking", "--no-first-run",
        f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom",
        "--virtual-time-budget=5000", page.as_uri(),
    ], capture_output=True, text=True, check=True, timeout=30)
    matched = re.search(r'<pre id="report">(.*?)</pre>', result.stdout, re.DOTALL)
    assert matched, "browser report is missing"
    report = json.loads(html.unescape(matched[1]))
    assert report["ok"], report
    assert report["checks"] >= 20
