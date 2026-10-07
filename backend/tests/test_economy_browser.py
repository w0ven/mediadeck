"""Real Chromium + actual frontend + real isolated backend, no live network.
The transport bridge uses TestClient instead of starting a daemon/service.
Run with a system Chromium (MEDIADECK_TEST_CHROMIUM may override its path).
"""

import os
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from app.main import app


def test_admin_shop_cards_title_controls_and_plugin_config_in_chromium(tmp_path, monkeypatch):
    # Chromium's Unix socket path has a 108-byte ceiling.
    monkeypatch.setenv("TMPDIR", str(tmp_path.parent))
    with TestClient(app) as client:
        client.auth = ("admin", "change-me")
        app.state.members.upsert("u", "viewer", {"group_id": "standard"})
        with sync_playwright() as driver:
            browser = driver.chromium.launch(
                executable_path=os.getenv("MEDIADECK_TEST_CHROMIUM", "/usr/bin/chromium"),
                args=["--no-sandbox"],
                headless=True,
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def bridge(route):
                request = route.request
                url = urlsplit(request.url)
                if url.hostname != "panel.test":
                    route.abort()
                    return
                if url.path == "/api/stream":
                    route.fulfill(
                        status=200,
                        content_type="text/event-stream",
                        body=": local browser test\n\n",
                    )
                    return
                path = url.path + ("?" + url.query if url.query else "")
                headers = {
                    k: v
                    for k, v in request.headers.items()
                    if k not in ("host", "authorization", "content-length", "accept-encoding")
                }
                response = client.request(
                    request.method, path, content=request.post_data_buffer, headers=headers
                )
                route.fulfill(
                    status=response.status_code,
                    headers={
                        k: v
                        for k, v in response.headers.items()
                        if k not in ("content-length", "content-encoding")
                    },
                    body=response.content,
                )

            page.route("**/*", bridge)
            page.goto("http://panel.test/#shop")
            page.locator("#sh-kind").wait_for()
            page.select_option("#sh-kind", "bandwidth_card")
            page.fill("#sh-name", "带宽20Mbps测试卡")
            page.fill("#sh-cost", "450")
            page.fill("#sh-amount", "20")
            page.fill("#sh-duration", "7")
            page.fill("#sh-limit", "2")
            page.check("#sh-enabled")
            page.click("#sh-add")
            page.wait_for_function("(state.shopItems || []).some(x => x.name==='带宽20Mbps测试卡')")
            item = next(r for r in app.state.shop.items() if r["name"] == "带宽20Mbps测试卡")
            assert (
                item["kind"] == "bandwidth_card"
                and item["duration_days"] == 7
                and item["amount"] == 20
                and item["per_user_limit"] == 2
            )
            page.fill("#ec-user", "u")
            page.click("#ec-load")
            page.locator("#ec-tag").wait_for()
            page.fill("#ec-tag", "浏览器授予")
            page.fill("#ec-days", "0")
            page.click("#ec-grant")
            page.get_by_text("浏览器授予", exact=False).wait_for()
            assert app.state.titles.titles("u")[0]["tag"] == "浏览器授予"
            # Real custom confirmation modal, not window.confirm.
            page.locator("[data-title-revoke]").click()
            page.get_by_role("button", name="确认", exact=True).click()
            page.wait_for_function(
                "document.querySelector('#ec-member').textContent.includes('已撤销')"
            )
            assert app.state.titles.titles("u")[0]["revoked_at"] is not None
            page.evaluate("automation.category='points'; go('automation')")
            page.locator("#pl-checkin-streak_tiers").wait_for()
            assert "2026-02-17" in page.locator("#pl-checkin-holidays").input_value()
            page.fill("#pl-checkin-double_ppm", "123456")
            page.locator('[data-plugin="checkin"] [data-act="save"]').click()
            page.wait_for_function(
                "document.querySelector('[data-plugin=checkin]').textContent.includes('已保存')"
            )
            assert app.state.plugins.config("checkin")["double_ppm"] == 123456

            # Verify itemized editor and mobile viewport behavior
            page.locator("[data-add-tier='checkin']").click()
            new_row_input = page.locator("[data-tier-rows='checkin'] tr:last-child [data-tier-days]")
            new_row_input.fill("45")
            new_bonus_input = page.locator("[data-tier-rows='checkin'] tr:last-child [data-tier-bonus]")
            new_bonus_input.fill("88")
            # Close advanced details to ensure itemized editor takes precedence
            page.evaluate("document.querySelector('#details-pl-checkin-streak_tiers').removeAttribute('open')")
            page.locator('[data-plugin="checkin"] [data-act="save"]').click()
            page.wait_for_function("document.querySelector('[data-plugin=checkin]').textContent.includes('已保存')")
            saved_tiers = app.state.plugins.config("checkin")["streak_tiers"]
            assert '"days":45' in saved_tiers or '"days": 45' in saved_tiers

            # Check mobile responsive width doesn't cause body horizontal overflow
            page.set_viewport_size({"width": 390, "height": 844})
            scroll_width = page.evaluate("document.body.scrollWidth")
            inner_width = page.evaluate("window.innerWidth")
            assert scroll_width <= inner_width + 1

            assert not errors
            browser.close()
