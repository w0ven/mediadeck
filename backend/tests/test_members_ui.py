"""Exercise the members page against actual panel scripts in isolated Chromium."""
from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path

import pytest

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
SCRIPTS = ("app.js", "intake.js", "nodepool.js", "ops.js", "members.js", "app.css")


def _write_harness(tmp_path: Path, *, hash_route: str = "members") -> Path:
    for name in SCRIPTS:
        shutil.copyfile(STATIC / name, tmp_path / name)
    shutil.copyfile(Path(__file__).parent / "members_ui_browser.js", tmp_path / "test.js")
    page = tmp_path / "test.html"
    markup = (STATIC / "index.html").read_text(encoding="utf-8").replace("/static/", "")
    markup = markup.replace(
        '<script src="members.js"></script>',
        '<script>window.__bootPanel = bootPanel; bootPanel = function () {};</script>'
        '<script src="members.js"></script>',
    )
    markup = markup.replace(
        "</body>",
        '<pre id="report" style="position:absolute;left:-9999px">pending</pre>'
        f'<script>window.__MEMBERS_HASH = {hash_route!r};</script>'
        '<script src="test.js"></script></body>',
    )
    page.write_text(markup, encoding="utf-8")
    return page


def _run_chromium(page: Path, tmp_path: Path, *, screenshot: Path | None = None,
                  budget: int = 15000) -> str:
    playwright = pytest.importorskip("playwright.sync_api")
    errors = []
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(
            executable_path=CHROMIUM,
            args=["--no-sandbox", "--disable-background-networking"],
        )
        try:
            tab = browser.new_page(viewport={"width": 1440, "height": 1100})
            tab.on("pageerror", lambda error: errors.append(str(error)))
            tab.goto(page.as_uri(), wait_until="domcontentloaded")
            tab.wait_for_function(
                "document.querySelector('#report') && "
                "document.querySelector('#report').textContent !== 'pending'",
                timeout=max(budget, 20000),
            )
            if screenshot:
                tab.screenshot(path=str(screenshot), full_page=True)
            assert not errors, errors
            return tab.content()
        except playwright.TimeoutError as exc:
            state = tab.evaluate("({url:location.href, report:document.querySelector('#report')?.textContent,"
                                 "view:document.querySelector('#view')?.textContent.slice(0,500)})")
            raise AssertionError({"browser_timeout": state, "errors": errors}) from exc
        finally:
            browser.close()


def _parse_report(dom: str) -> dict:
    matched = re.search(r'<pre id="report"[^>]*>(.*?)</pre>', dom, re.DOTALL)
    assert matched, "browser report is missing"
    raw = html.unescape(matched.group(1)).strip()
    assert raw and raw != "pending", f"browser report did not finish: {raw!r}"
    return json.loads(raw)


@pytest.mark.skipif(not CHROMIUM, reason="Chromium is needed for the browser regression")
def test_members_ui_browser(tmp_path: Path) -> None:
    page = _write_harness(tmp_path, hash_route="members")
    report = _parse_report(_run_chromium(page, tmp_path))
    assert report.get("ok"), report
    assert report["checks"] >= 20


@pytest.mark.skipif(not CHROMIUM, reason="Chromium is needed for the browser regression")
def test_members_direct_hash_and_detail_screenshot(tmp_path: Path) -> None:
    page = _write_harness(tmp_path, hash_route="members?id=u-alice&tab=overview&page_size=50")
    report = _parse_report(_run_chromium(page, tmp_path, budget=18000))
    assert report.get("ok"), report
