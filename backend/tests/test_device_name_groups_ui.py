"""Actual panel scripts expand raw IDs from real isolated SQLite/API payloads."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_device_name_groups import AUTH, seed_33
from test_members_ui import CHROMIUM, _parse_report, _write_harness

from app.main import app


@pytest.mark.skipif(not CHROMIUM, reason="Chromium is needed for the UI regression")
@pytest.mark.parametrize("width", [1440, 390])
def test_name_group_counts_expand_original_ids_and_target_only_selected_id(tmp_path, width):
    with TestClient(app) as client:
        seed_33(app.state.members)
        initial = client.get("/api/members/viewer-a", auth=AUTH).json()
        target = next(row for row in initial["devices"] if row["device_name"] == "iPhone")
        block_path = f'/api/members/viewer-a/devices/{target["device_id"]}/block'
        blocked_response = client.post(block_path, auth=AUTH).json()
        mixed = client.get("/api/members/viewer-a", auth=AUTH).json()
        for row in initial["devices"]:
            if row["device_name"] == "iPhone":
                app.state.members.set_device_blocked("viewer-a", row["device_id"], True)
        all_phone_blocked = client.get("/api/members/viewer-a", auth=AUTH).json()
        assert all_phone_blocked["member"]["device_count"] == 1
        payload = {
            "initial": initial, "mixed": mixed, "blocked": all_phone_blocked,
            "block_path": block_path, "block_response": blocked_response,
            "groups": client.get("/api/groups", auth=AUTH).json(),
            "listing": client.get("/api/members", auth=AUTH).json(),
        }
        # Use the initial API count for the initial list projection.
        payload["listing"]["members"] = [initial["member"]]
        page = _write_harness(tmp_path)
        shutil.copyfile(Path(__file__).with_name("device_name_groups_ui_browser.js"),
                        tmp_path / "test.js")
        markup = page.read_text().replace(
            '<script src="test.js"></script>',
            f'<script>window.__DEVICE_GROUPS={json.dumps(payload)};</script>'
            '<script src="test.js"></script>',
        )
        page.write_text(markup)
        driver = pytest.importorskip("playwright.sync_api")
        errors = []
        with driver.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=CHROMIUM,
                args=["--no-sandbox", "--disable-background-networking"],
            )
            try:
                tab = browser.new_page(viewport={"width": width, "height": 1100})
                tab.on("pageerror", lambda error: errors.append(str(error)))
                tab.goto(page.as_uri(), wait_until="domcontentloaded")
                tab.wait_for_function("document.querySelector('#report').textContent !== 'pending'")
                tab.screenshot(path=str(tmp_path / f"device-groups-{width}.png"), full_page=True)
                report = _parse_report(tab.content())
                assert not errors, errors
                assert report.get("ok"), report
                assert report["checks"] >= 20
            finally:
                browser.close()
        assert len(app.state.members.devices("viewer-a")) == 33
