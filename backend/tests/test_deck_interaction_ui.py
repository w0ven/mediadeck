"""Deck interaction contract in real Chromium against an isolated mock server.

Covers the shared interaction layer rather than any single feature: grouped
navigation, typed feedback, named and graded confirmations, route-backed list
state (requests, audit), the member drawer layout and extension points, and
unsaved-draft protection on per-object pages. No production I/O.
"""
import os
import shutil
import socket
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
    with tempfile.TemporaryDirectory(prefix="deck-ix-") as tmp:
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


def test_navigation_groups_and_aliases(page):
    groups = page.evaluate("NAV.map(g=>g.group)")
    assert len(groups) == 7 and "安全与审计" in groups
    assert max(page.evaluate("NAV.map(g=>g.items.length)")) <= 5
    # The sidebar shows each workspace once; its pages appear only in the strip below the title.
    assert page.locator("#nav .nav-item").count() == 7
    assert not page.locator("#nav .nav-sub").first.is_visible()
    go(page, "audit")
    assert page.locator("#subnav a").all_inner_texts() == ["审计日志", "访问拦截", "共享检测"]
    page.evaluate("void go('plugins')")
    page.wait_for_function("state.page==='automation' && state.pageReady")
    assert page.evaluate("automation.category") == "points"
    assert "页面不存在" not in page.locator("#view").inner_text()
    # The phone drawer lists every page, and the page strip scrolls instead of wrapping.
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#nav-toggle").click()
    assert page.locator("#nav [data-subpage]:visible").count() == page.evaluate("NAV.flatMap(g=>g.items).length")
    page.keyboard.press("Escape")
    assert page.evaluate("getComputedStyle(document.querySelector('#subnav')).flexWrap") == "nowrap"


def test_feedback_and_confirmation_primitives(page):
    page.evaluate("toast('保存失败: demo', 1)")
    toast = page.locator("#toast")
    assert "is-bad" in toast.get_attribute("class") and toast.get_attribute("role") == "alert"
    bad = page.evaluate("getComputedStyle(document.querySelector('#toast')).backgroundColor")
    page.evaluate("toast('已保存')")
    assert "is-ok" in toast.get_attribute("class")
    assert page.evaluate("getComputedStyle(document.querySelector('#toast')).backgroundColor") != bad
    page.locator("#toast .toast-close").click()
    assert not toast.is_visible()
    # Destructive confirmations are marked, named, and start on 取消.
    page.evaluate("()=>{window.answer=undefined;deckConfirm('删除节点 demo？', {title:'删除节点', confirmText:'删除', danger:true}).then(v=>{window.answer=v})}")
    card = dialog(page)
    assert "is-danger" in card.get_attribute("class")
    assert card.locator(".deck-dialog-title").inner_text() == "删除节点"
    assert card.locator(".deck-dialog-btn-confirm").inner_text() == "删除"
    page.wait_for_function("document.activeElement.classList.contains('deck-dialog-btn-cancel')")
    page.keyboard.press("Enter")
    page.wait_for_function("window.answer===false")
    # Legacy call sites stay plain unless the message says the action is irreversible.
    page.evaluate("()=>{window.answer=undefined;deckConfirm('继续？').then(v=>{window.answer=v})}")
    assert "is-danger" not in dialog(page).get_attribute("class")
    page.keyboard.press("Escape")
    page.evaluate("()=>{window.answer=undefined;deckConfirm('将删除该账号，不可恢复。').then(v=>{window.answer=v})}")
    assert "is-danger" in dialog(page).get_attribute("class")
    page.keyboard.press("Escape")
    page.evaluate("()=>{window.answer=undefined;deckPrompt('状态', 'accepted', {choices:[['open','待处理'],['accepted','已接受']]}).then(v=>{window.answer=v})}")
    select = dialog(page).locator("select.deck-dialog-input")
    assert select.input_value() == "accepted"
    select.select_option("open")
    dialog(page).locator(".deck-dialog-btn-confirm").click()
    page.wait_for_function("window.answer==='open'")


def test_requests_state_lives_in_the_route(page):
    go(page, "requests?status=accepted")
    assert page.locator(".seg-btn.active").inner_text().startswith("已接受")
    page.locator("#request-search").fill("不存在的片名")
    page.locator("#request-search").press("Enter")
    page.wait_for_function("state.route.includes('q=') && state.pageReady")
    assert "status=accepted" in page.evaluate("state.route")
    assert "没有符合筛选的工单" in page.locator("#view").inner_text()
    page.go_back()
    page.wait_for_function("state.route==='requests?status=accepted' && state.pageReady")
    page.reload()
    page.wait_for_function("state.pageReady")
    assert page.evaluate("state.route") == "requests?status=accepted"
    # The status switch is a view filter, not a primary action.
    assert page.locator("#view .page-actions .btn.primary").count() == 0


def test_audit_filters_are_exact_clickable_and_kept(page):
    go(page, "audit")
    first = page.locator("#view [data-audit-filter='action']").first
    value = first.get_attribute("data-value")
    first.click()
    page.wait_for_function("v=>state.route.includes('action='+encodeURIComponent(v)) && state.pageReady", arg=value)
    actions = set(page.locator("#view [data-audit-filter='action']").evaluate_all("els=>els.map(e=>e.dataset.value)"))
    assert actions == {value}
    page.reload()
    page.wait_for_function("state.pageReady")
    assert page.locator("#au-action").input_value() == value
    page.locator("#au-action").fill("")
    page.locator("#au-action").press("Enter")
    page.wait_for_function("state.route==='audit' && state.pageReady")


def test_member_drawer_actions_first_and_extension_points(page):
    page.evaluate("""()=>{
      registerMemberDetailTab('demo-tree', '演示分区', (host, ctx) => { host.textContent = 'tree:' + ctx.member.username; });
      registerMemberDetailSection('overview', (host, ctx) => { host.id = 'demo-section'; host.textContent = 'section:' + ctx.id; });
    }""")
    go(page, "members?id=u1&tab=overview")
    page.locator("#md-title").wait_for()
    for selector in ("#md-renew", "#md-group-go", '[data-member-action="actions/disable"]', '[data-member-action="password"]'):
        box = page.locator(selector).bounding_box()
        assert box and box["y"] + box["height"] <= 900, selector
        assert not page.locator(selector).evaluate("e=>!!e.closest('details')"), selector
    assert page.locator("#demo-section").inner_text() == "section:u1"
    page.locator('[data-tab="demo-tree"]').click()
    page.wait_for_function("document.querySelector('[data-member-tab-host]')?.textContent==='tree:demo-user-1'")
    page.locator('[data-tab="overview"]').click()
    page.locator("#md-renew").wait_for()
    page.locator("#md-renew").click()
    card = dialog(page)
    assert "demo-user-1" in card.locator(".deck-dialog-body").inner_text()
    card.locator(".deck-dialog-btn-cancel").click()
    page.locator('[data-member-action="actions/disable"]').click()
    card = dialog(page)
    assert "is-danger" in card.get_attribute("class") and "demo-user-1" in card.inner_text()
    card.locator(".deck-dialog-btn-cancel").click()


def test_node_drafts_survive_actions_and_guard_navigation(page):
    go(page, "nodes")
    names = page.evaluate("state.nodes.map(n=>n.name)")
    assert len(names) >= 2
    draft = page.locator(f'[id="nc-size-{names[1]}"]')
    draft.fill("321G")
    assert page.locator(f'[data-node-draft="{names[1]}"]').inner_text() == "有未保存的修改"
    # Taking another node offline is confirmed by name and does not wipe the draft.
    page.locator(f'[data-live-key="card:⛁ {names[0]}"] button', has_text="下线").click()
    card = dialog(page)
    assert names[0] in card.inner_text() and "is-danger" in card.get_attribute("class")
    card.locator(".deck-dialog-btn-confirm").click()
    page.wait_for_function("n=>state.nodes.find(x=>x.name===n).manually_disabled", arg=names[0])
    assert draft.input_value() == "321G"
    assert "unsaved" in page.locator(f'[data-live-key="card:⛁ {names[1]}"]').get_attribute("class")
    page.locator(f'[data-live-key="card:⛁ {names[0]}"] button', has_text="上线").click()
    page.wait_for_function("n=>!state.nodes.find(x=>x.name===n).manually_disabled", arg=names[0])
    page.evaluate("void go('dashboard')")
    card = dialog(page)
    assert names[1] in card.inner_text()
    card.locator(".deck-dialog-btn-cancel").click()
    page.wait_for_timeout(200)
    assert page.evaluate("state.page") == "nodes" and draft.input_value() == "321G"


def test_settings_membership_section_and_session_expiry(page, server):
    go(page, "settings?section=membership")
    visible = page.locator(".config-section:not([hidden]) .card h3").all_inner_texts()
    assert visible == ["会员与观看"]
    assert page.locator("#view > .card h3").all_inner_texts() in ([], ["演示模式"])
    # A lost session sends the operator to the login page with the route kept.
    page.context.clear_cookies()
    page.evaluate("void go('audit')")
    page.wait_for_url("**/login#/audit", timeout=8000)
