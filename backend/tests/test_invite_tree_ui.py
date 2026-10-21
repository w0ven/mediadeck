"""Invite cases and the member invite tree in real Chromium against an isolated mock server."""
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

CHROMIUM = shutil.which("chromium") or "/usr/bin/chromium"
AUTH = ("demo-admin", "local-demo-only")


@pytest.fixture(scope="module")
def server():
    with tempfile.TemporaryDirectory(prefix="deck-invite-") as tmp:
        env = {**os.environ, "MEDIADECK_MOCK": "1", "MEDIADECK_DATA_DIR": tmp,
               "MEDIADECK_ADMIN_USER": AUTH[0], "MEDIADECK_ADMIN_PASSWORD": AUTH[1]}
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        base = f"http://127.0.0.1:{port}"
        with open(Path(tmp) / "server.log", "w+") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                        "--port", str(port), "--log-level", "error"], env=env,
                                       cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=base, auth=AUTH, timeout=15) as client:
                    for _ in range(200):
                        if process.poll() is not None:
                            log.seek(0)
                            raise RuntimeError("mock exited: " + log.read())
                        try:
                            if client.get("/healthz").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.05)
                    else:
                        raise RuntimeError("mock startup timed out")
                    client.put("/api/members/u1", json={"username": "demo-user-1", "group_id": "standard"}).raise_for_status()
                    client.put("/api/members/u2", json={"username": "demo-user-2", "group_id": "standard"}).raise_for_status()
                    db = sqlite3.connect(Path(tmp) / "mediadeck.db")
                    db.execute("UPDATE members SET inviter_id='u1' WHERE emby_user_id='u2'")
                    db.commit()
                    db.close()
                    client.request("DELETE", "/api/members/u2", json={"cascade": False}).raise_for_status()
                    yield base
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()


@pytest.fixture
def page(server):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})

        def isolated(route):
            url = urlparse(route.request.url)
            if url.netloc != urlparse(server).netloc:
                return route.abort()
            if "/Images/" in url.path:
                return route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg"/>')
            if url.path == "/api/update/check":
                return route.fulfill(json={"latest": "demo", "update_available": False})
            return route.continue_()
        ctx.route("**/*", isolated)
        assert ctx.request.post(server + "/api/auth/login", data={"username": AUTH[0], "password": AUTH[1]}).ok
        tab = ctx.new_page()
        tab.set_default_timeout(8000)
        errors = []
        tab.on("pageerror", lambda e: errors.append(str(e)))
        tab.goto(server + "/#/dashboard")
        tab.wait_for_function("state.pageReady")
        tab.evaluate("live.src?.close();live.pending.clear()")
        yield tab
        assert not errors, errors
        browser.close()


def go(page, route):
    page.evaluate("r=>{void go(r)}", route)
    page.wait_for_function("r=>state.route===r && state.pageReady", arg=route)


def dialog(page):
    card = page.locator(".deck-dialog-backdrop:not(.deck-dialog-closing) .deck-dialog-card")
    card.wait_for(state="visible")
    return card


def test_case_card_suspends_inviter_and_drawer_tab_shows_tree(page):
    go(page, "invites")
    cases = page.locator("#invite-cases")
    cases.get_by_text("1 个待处理").wait_for()
    assert "demo-user-2" in cases.inner_text() and "demo-user-1" in cases.inner_text()
    cases.get_by_role("button", name="暂停邀请 30 天…").click()
    box = dialog(page)
    assert "demo-user-1" in box.inner_text() and "30 天" in box.inner_text()
    box.get_by_role("button", name="暂停 30 天").click()
    cases.get_by_text("没有待处理案件").wait_for()
    assert "已暂停邀请" in cases.inner_text()
    go(page, "members?id=u1&tab=invite-tree")
    tree = page.locator('[data-member-tab-host="invite-tree"]')
    tree.get_by_text("TA 邀请的人").wait_for()
    text = tree.inner_text()
    assert "demo-user-2" in text and "已删除" in text and "暂停至" in text
    tree.get_by_role("button", name="解除暂停").click()
    dialog(page).get_by_role("button", name="解除暂停").click()
    tree.locator(".tag.ok", has_text="正常").wait_for()
