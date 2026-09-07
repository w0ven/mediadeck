"""Exercise SSE updates against actual panel scripts in isolated Chromium."""
from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


@pytest.mark.skipif(not CHROMIUM, reason="Chromium is needed for the browser regression")
def test_live_updates_browser(tmp_path):
    tests = Path(__file__).parent
    for name in ("app.js", "intake.js"):
        shutil.copyfile(tests.parent / "app/static" / name, tmp_path / name)
    shutil.copyfile(tests / "live_ui_browser.js", tmp_path / "test.js")
    page = tmp_path / "test.html"
    page.write_text('''<!doctype html><meta charset="utf-8">
<div id="nav"></div><div id="page-title"></div><div id="page-sub"></div>
<div id="last-updated"></div><div id="live-state"></div><div id="toast"></div>
<main id="view"></main><pre id="report">pending</pre>
<script src="app.js"></script><script src="intake.js"></script><script src="test.js"></script>
''')
    result = subprocess.run([
        CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu",
        "--disable-background-networking", "--no-first-run",
        f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom",
        "--virtual-time-budget=10000", page.as_uri(),
    ], capture_output=True, text=True, check=True, timeout=40)
    matched = re.search(r'<pre id="report">(.*?)</pre>', result.stdout, re.DOTALL)
    assert matched, "browser report is missing"
    report = json.loads(html.unescape(matched[1]))
    assert report["ok"], report
    assert report["checks"] >= 30


def test_live_dashboard_topics_have_real_providers():
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    cfg = settings()
    with TestClient(app) as client:
        auth = (cfg.mediadeck_admin_user, cfg.mediadeck_admin_password)
        for topic, path in (
            ("dispatch", "/api/dispatch/log?limit=20"),
            ("overview", "/api/stats/overview?days=30"),
            ("latest", "/api/emby/latest?limit=12"),
        ):
            actual = asyncio.run(app.state.events._collect(topic))
            expected = client.get(path, auth=auth)
            assert expected.status_code == 200
            assert actual is not None
            assert actual == expected.json()
