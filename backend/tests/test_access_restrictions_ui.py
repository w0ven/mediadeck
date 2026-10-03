"""Offline access-restriction UI regression; run directly with Python, no backend.

    python3 backend/tests/test_access_restrictions_ui.py

The fixture copies real frontend scripts into disposable Chromium profiles. All
API responses are mocked in-browser; no server, punishment or TG send is used.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


@unittest.skipUnless(CHROMIUM, "Chromium is needed for the offline browser regression")
class AccessRestrictionsUITest(unittest.TestCase):
    def test_access_restrictions_render_and_interact(self):
        tests = Path(__file__).parent
        static = tests.parent / "app" / "static"
        for width in (1280, 500):
            with self.subTest(width=width), tempfile.TemporaryDirectory(prefix="access-restrictions-ui-") as directory:
                target = Path(directory)
                for name in ("app.js", "workspace.js", "ops.js", "dialog.js", "app.css"):
                    shutil.copyfile(static / name, target / name)
                shutil.copyfile(tests / "access_restrictions_ui_browser.js", target / "test.js")
                page = target / "test.html"
                page.write_text("""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="connect-src 'none'; img-src 'none'">
<link rel="stylesheet" href="app.css">
<div id="view" style="padding:12px"></div><pre id="report">pending</pre>
<script>
window.fetch = () => { throw new Error('Unmocked network request'); };
window.addEventListener('error', e => {
  document.querySelector('#report').textContent = JSON.stringify({ok:false,error:e.message});
});
</script>
<script src="dialog.js"></script><script src="workspace.js"></script>
<script src="app.js"></script><script src="ops.js"></script><script src="test.js"></script>
""", encoding="utf-8")
                result = subprocess.run([
                    CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu",
                    "--disable-background-networking", "--no-first-run", "--no-proxy-server",
                    "--host-resolver-rules=MAP * ~NOTFOUND", "--hide-scrollbars",
                    f"--window-size={width},1000", f"--user-data-dir={target / 'profile'}",
                    "--dump-dom", "--virtual-time-budget=5000", page.as_uri(),
                ], capture_output=True, text=True, check=True, timeout=30)
                matched = re.search(r'<pre id="report">(.*?)</pre>', result.stdout, re.DOTALL)
                self.assertIsNotNone(matched, "browser report is missing")
                report = json.loads(html.unescape(matched[1]))
                self.assertTrue(report["ok"], report)
                self.assertEqual(report["width"], width)
                self.assertGreaterEqual(report["checks"], 64)
                print(f"Access restrictions UI: {report['checks']} checks passed at {report['width']}px")


if __name__ == "__main__":
    unittest.main()
