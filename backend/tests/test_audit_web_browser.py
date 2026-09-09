"""Full web audit regressions: real Chromium, isolated mock API, no production I/O.
Run: PYTHONPATH=. .venv/bin/python -m pytest tests/test_audit_web_browser.py -q
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

ARTIFACTS = Path(os.environ['AUDIT_WEB_ARTIFACT_DIR']) if os.environ.get('AUDIT_WEB_ARTIFACT_DIR') else None
TEMP = Path(os.environ.get('AUDIT_WEB_TEMP_DIR', tempfile.gettempdir()))


@pytest.fixture(scope="module")
def server():
    TEMP.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="audit-web-", dir=TEMP) as tmp:
        environment = {**os.environ, "MEDIADECK_MOCK": "1", "MEDIADECK_DATA_DIR": tmp,
                       "MEDIADECK_ADMIN_USER": "demo-admin", "MEDIADECK_ADMIN_PASSWORD": "local-demo-only"}
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        base = f"http://127.0.0.1:{port}"
        # A separate interpreter owns settings caches, app.state and background tasks.
        # Never change this pytest process's environment or import the global app.
        with open(Path(tmp) / "server.log", "w+") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                                        "--host", "127.0.0.1", "--port", str(port),
                                        "--log-level", "error"], env=environment,
                                       cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=base, auth=("demo-admin", "local-demo-only")) as client:
                    for _ in range(200):
                        if process.poll() is not None:
                            log.seek(0)
                            raise RuntimeError('isolated mock exited: ' + log.read())
                        try:
                            if client.get("/healthz").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.05)
                    else:
                        log.seek(0)
                        raise RuntimeError("isolated mock startup timed out: " + log.read())
                    response = client.put("/api/members/audit-viewer", json={"username": "DemoViewer", "group_id": "standard"})
                    response.raise_for_status()
                    yield base
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"])
        yield browser
        browser.close()


@pytest.fixture
def page(server, browser):
    ctx = browser.new_context(http_credentials={"username": "demo-admin", "password": "local-demo-only"}, viewport={"width": 1440, "height": 1000})
    # External imagery/update requests are not evidence about real integrations.
    def isolated(route):
        if urlparse(route.request.url).netloc != urlparse(server).netloc:
            return route.abort()
        static_dir = os.environ.get('AUDIT_WEB_STATIC_DIR')
        if static_dir and urlparse(route.request.url).path.startswith('/static/'):
            return route.fulfill(path=Path(static_dir) / Path(urlparse(route.request.url).path).name)
        if "/api/update/check" in route.request.url:
            return route.fulfill(json={"latest": "demo", "update_available": False})
        if "/Images/" in route.request.url:
            return route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="360"><rect width="240" height="360" fill="#254255"/></svg>')
        return route.continue_()
    ctx.route("**/*", isolated)
    page = ctx.new_page()
    page.set_default_timeout(7000)
    page.goto(server + "/#/dashboard")
    page.wait_for_function("state.pageReady")
    page.evaluate("live.src?.close();live.pending.clear()")
    yield page
    ctx.close()


def go(page, route):
    page.evaluate("r=>go(r)", route)
    page.wait_for_function("state.pageReady")
    page.evaluate("live.src?.close();live.pending.clear()")


def hold(page, path, method="GET"):
    page.evaluate("""([path,method])=>{
      const original=window.fetch;window.auditWaiting=false;
      window.fetch=async (url,opts={})=>{
        const response=await original(url,opts);
        if(String(url).split('?')[0]===path && (opts.method||'GET')===method){
          window.auditWaiting=true;await new Promise(resolve=>window.auditRelease=resolve);
        }
        return response;
      };
    }""", [path, method])


STALE = [("library", "/api/emby/libraries"), ("imports", "/api/imports"),
         ("groups", "/api/groups"), ("storage", "/api/storage/remotes"),
         ("stats", "/api/stats/daily"), ("audit", "/api/audit"),
         ("redeem", "/api/redeem"), ("invites", "/api/registration/grants"),
         ("automation", "/api/plugins"), ("access", "/api/access/rules"),
         ("sharing", "/api/sharing"), ("shop", "/api/shop/items"),
         ("requests", "/api/requests"), ("update", "/api/update/version")]


@pytest.mark.parametrize("route,path", STALE)
def test_slow_page_cannot_replace_new_page(page, route, path):
    hold(page, path)
    page.evaluate("r=>{void go(r)}", route)
    page.wait_for_function("auditWaiting")
    go(page, "tggroup")
    before = page.locator("#view").inner_html()
    page.evaluate("auditRelease()")
    page.wait_for_timeout(180)
    assert page.locator("#view").inner_html() == before, route
    assert page.evaluate("state.page") == "tggroup"


def test_stale_action_refresh_is_noop(page):
    go(page, "tggroup")
    before = page.locator("#view").inner_html()
    page.evaluate("renderPage('imports')")
    assert page.locator("#view").inner_html() == before


@pytest.mark.parametrize('run_ok', [True,False])
def test_plugin_run_uses_visible_draft_and_history_preserves_it(page, run_ok):
    card = {"id": "demo-job", "name": "Demo job", "enabled": False, "category": "task", "fields": [{"key": "hour", "kind": "int", "default": 4, "label": "时间"}], "config": {"hour": 4}}
    sent = []
    def plugin(route):
        if route.request.method == "POST":
            sent.append((route.request.url, route.request.post_data_json))
            route.fulfill(json={'ok':run_ok,'card':{**card,'last_run':{'ok':run_ok,'started_at':time.time(),'summary':{'demo':'result'}}}} if route.request.url.endswith('/run') else card)
        elif "/history" in route.request.url:
            route.fulfill(json=[])
        else:
            route.fulfill(json=[card])
    page.route("**/api/plugins**", plugin)
    go(page, "automation")
    page.locator("#pl-demo-job-hour").fill("9")
    page.locator('[data-act="history"]').click()
    expect(page.locator("#pl-demo-job-hour")).to_have_value("9")
    page.locator('[data-act="run"]').click()
    page.wait_for_function("!automation.busy['demo-job']")
    assert sent[0][1]["config"] == {"hour": "9"}
    expect(page.locator('.plugin-result .tag')).to_have_text('成功' if run_ok else '失败')
    expect(page.locator('#pl-demo-job-hour')).to_have_value('9')


def test_redeem_nonempty_filter_and_draft(page):
    all_codes = [{"code": "demo-unused", "masked": "unused-mask", "status": "unused"}, {"code": "demo-used", "masked": "used-mask", "status": "used"}]
    page.route("**/api/redeem?**", lambda r: r.fulfill(json={"codes": [all_codes[1]], "stats": {"used": 1}, "batches": []}))
    page.route("**/api/redeem", lambda r: r.fulfill(json={"codes": all_codes, "stats": {"used": 1, "unused": 1}, "batches": []}))
    go(page, "redeem")
    page.locator("#rd-note").fill("keep draft")
    page.locator("#rd-f-status").select_option("used")
    page.locator("#rd-filter").click()
    page.wait_for_timeout(200)
    expect(page.locator(".code-cell")).to_have_count(1)
    expect(page.locator("#rd-note")).to_have_value("keep draft")


def test_group_round_trip_preserves_limits(page):
    got = page.evaluate("""()=>{const g={id:'demo',name:'Demo',traffic_quota_bytes:0,bandwidth_limit_kbps:20000};openModal('Edit',groupForm('edit',g));return groupPayload('edit')}""")
    assert got["traffic_quota_bytes"] == 0
    assert got["bandwidth_limit_kbps"] == 20000
    got = page.evaluate("""()=>{closeModal();openModal('Edit',groupForm('edit',{traffic_quota_bytes:123456789,bandwidth_limit_kbps:1}));return groupPayload('edit')}""")
    assert got["traffic_quota_bytes"] == 123456789
    assert got["bandwidth_limit_kbps"] == 1


def test_override_round_trip_preserves_limits_and_unavailable_libraries(page):
    got = page.evaluate("""()=>{const overrides={bandwidth_limit_kbps:20000,extra_traffic_bytes:123456789,libraries_mode:'replace',libraries:['unavailable-lib']};openModal('Edit',overrideEditor({overrides},[]));return collectOverridesFromForm(overrides)}""")
    assert got["bandwidth_limit_kbps"] == 20000
    assert got["extra_traffic_bytes"] == 123456789
    assert got["libraries"] == ["unavailable-lib"]


def test_skip_link_and_modal_focus(page):
    page.locator("#nav-search").focus()
    page.evaluate("openModal('Demo dialog','<button id=demo-last>Last</button>')")
    expect(page.get_by_role("dialog", name="Demo dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#nav-search")).to_be_focused()
    page.locator(".skip-link").focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(100)
    assert page.evaluate("state.page") == "dashboard"
    expect(page.locator("#view")).to_be_focused()


def test_mobile_menu_traps_focus(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#nav-toggle").click()
    expect(page.locator("#nav a").first).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(page.locator("#nav a").last).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#nav-toggle")).to_be_focused()


def test_member_summary_keyboard_does_not_open_drawer(page):
    go(page, "members")
    summary = page.locator('tr[data-id="audit-viewer"] summary')
    summary.focus()
    page.keyboard.press("Enter")
    assert page.locator("#member-detail").is_hidden()
    assert summary.locator("..").get_attribute("open") is not None


def test_closed_member_action_cannot_reopen_drawer(page):
    go(page, "members?id=audit-viewer&tab=invites")
    page.route("**/api/points/audit-viewer/adjust", lambda r: r.fulfill(json={"ok": True}))
    hold(page, "/api/points/audit-viewer/adjust", "POST")
    page.on("dialog", lambda d: d.accept())
    page.locator("#md-points-delta").fill("2")
    page.locator("#md-points-reason").fill("demo audit")
    page.locator("#md-points-save").click()
    page.wait_for_function("auditWaiting")
    page.locator("#md-close").click()
    page.evaluate("auditRelease()")
    page.wait_for_timeout(200)
    expect(page.locator("#member-detail")).to_be_hidden()


def test_import_double_submit(page):
    sent = []
    def submit(route):
        if route.request.method == "POST":
            sent.append(route.request.post_data_json)
            route.fulfill(json={"id": "demo-import"})
        else:
            route.fulfill(json=[])
    page.route("**/api/imports", submit)
    go(page, "imports")
    hold(page, "/api/imports", "POST")
    page.locator("#imp-src").fill("demo-source")
    page.evaluate("document.querySelector('#imp-go').click();document.querySelector('#imp-go').click()")
    page.wait_for_function("auditWaiting")
    assert len(sent) == 1
    page.evaluate("auditRelease()")


def test_enroll_does_not_restart_poll_after_leaving(page):
    go(page, "nodes")
    name = page.evaluate("state.nodes[0].name")
    page.route("**/api/nodes/*/enroll", lambda r: r.fulfill(json={"enrolled": False, "command": "demo command"}))
    hold(page, "/api/nodes/" + name + "/enroll")
    page.evaluate("n=>{void showEnroll(n)}", name)
    page.wait_for_function("auditWaiting")
    go(page, "tggroup")
    page.evaluate("auditRelease()")
    page.wait_for_timeout(100)
    assert page.evaluate("!state.enrollTimer")


def test_access_failed_toggle_restores_checkbox(page):
    page.route("**/api/access/rules", lambda r: r.fulfill(json=[{"id": 1, "enabled": True, "kind": "client", "action": "deny", "pattern": "demo"}]))
    page.route("**/api/access/rules/1/enabled", lambda r: r.fulfill(status=503, json={"detail": "demo failure"}))
    go(page, "access")
    box = page.locator('input[onchange*="toggleAccessRule"]')
    box.uncheck()
    expect(page.locator("#toast")).to_contain_text("失败")
    expect(box).to_be_checked()


def test_inline_argument_apostrophe_is_data(page):
    # Shop names are arbitrary display strings; an apostrophe must not execute code.
    go(page, "shop")
    page.evaluate("""()=>{window.auditInjected=false;window.auditName=`demo');window.auditInjected=true;//`;state.shopItems=[];document.querySelector('#view').innerHTML=`<button id=probe onclick="deleteShopItem(1, '${q(auditName)}')">delete</button>`;}""")
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.locator("#probe").click()
    assert not page.evaluate("auditInjected")
    assert dialogs and page.evaluate("auditName") in dialogs[0]


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_all_pages_read_only_reachable_no_overflow(page, width):
    page.set_viewport_size({"width": width, "height": 1000})
    errors, writes, coverage = [], [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: writes.append(r.url) if r.method in ("POST", "PUT", "DELETE") else None)
    routes = page.evaluate("NAV.flatMap(g=>g.items).map(it=>it.id)")
    for route in routes:
        go(page, route)
        assert "页面不存在" not in page.locator("#view").inner_text(), route
        overflowing = page.evaluate("document.documentElement.scrollWidth>innerWidth+1")
        coverage.append({"page": route, "overflow": overflowing})
    if ARTIFACTS:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"web-coverage-{width}.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2))
    assert not errors, errors
    assert not writes, writes
    assert not [c for c in coverage if c["overflow"]], coverage


@pytest.mark.parametrize('route,path', [('library','/api/emby/libraries'), ('dashboard','/api/emby/sessions'), ('invites','/api/registration/grants'), ('requests','/api/requests/stats'), ('shop','/api/shop/orders')])
def test_failed_reads_are_not_healthy_empty_success(page, route, path):
    page.route('**' + path + '**', lambda r: r.fulfill(status=503, json={'detail':'demo unavailable'}))
    page.evaluate('r=>go(r)', route)
    expect(page.locator('.page-error')).to_be_visible()
    page.unroute('**' + path + '**')
    page.locator('#retry-page').click()
    page.wait_for_function('state.pageReady && !document.querySelector(".page-error")')


def test_member_override_raw_success_and_other_draft(page):
    go(page, 'members?id=audit-viewer&tab=entitlements')
    page.on('dialog', lambda d:d.accept())
    page.locator('.md-role[value="uploader"]').check()
    page.locator('#ov-streams').fill('7')
    page.locator('#ov-save').click()
    expect(page.locator('#toast')).to_contain_text('权限覆盖已保存')
    expect(page.locator('.md-role[value="uploader"]')).to_be_checked()
    expect(page.locator('#ov-streams')).to_have_value('7')
    assert page.evaluate("document.querySelector('.md-role[value=\"uploader\"]').checked")


def test_node_mount_read_failure_does_not_clear_assignment(page):
    sent = []
    page.route('**/api/storage/mounts', lambda r:r.fulfill(status=503, json={'detail':'demo failure'}))
    def node_save(route):
        if route.request.method == 'PUT':
            sent.append(route.request.post_data_json)
            route.fulfill(json={'ok':True})
        else:
            route.continue_()
    page.route('**/api/nodes/*', node_save)
    go(page, 'nodes')
    page.wait_for_function('document.querySelector("[id^=nmounts-]").textContent.includes("无法读取")')
    page.locator('button[onclick^="saveNodeStorage"]').first.click()
    page.wait_for_timeout(100)
    assert sent and 'mount_ids' not in sent[0]


def test_nodepool_save_preserves_other_node_draft(page):
    go(page, 'nodepool')
    page.route('**/api/nodes/*/pool', lambda r:r.fulfill(json={'changed':{'capacity':123}}))
    weights = page.locator('.np-weight')
    assert weights.count() >= 2
    weights.nth(1).fill('654')
    weights.first.fill('123')
    page.locator('.np-save').first.click()
    expect(page.locator('#toast')).to_contain_text('已保存')
    expect(weights.nth(1)).to_have_value('654')


def test_all_invite_members_can_be_selected(page):
    def listing(route):
        from urllib.parse import parse_qs
        params = parse_qs(urlparse(route.request.url).query)
        size = int(params.get('page_size', ['50'])[0])
        number = int(params.get('page', ['1'])[0])
        data = [{'emby_user_id':f'demo-{i}', 'username':f'Demo{i}'} for i in range(51)]
        route.fulfill(json={'members':data[(number-1)*size:number*size], 'total':51, 'page':number,'page_size':size})
    page.route('**/api/members*', listing)
    go(page, 'invites')
    expect(page.locator('#iq-member option')).to_have_count(51)


def test_essential_controls_not_clipped_on_small_mobile(page):
    page.set_viewport_size({'width':320,'height':844})
    clipped=[]
    for route in ['imports','nodes','automation','groups','settings']:
        go(page,route)
        clipped.extend(page.evaluate('''()=>[...document.querySelectorAll('#view input,#view select,#view button')].filter(el=>el.offsetParent!==null).filter(el=>{
          const rect=el.getBoundingClientRect();
          for(let p=el.parentElement;p;p=p.parentElement){const style=getComputedStyle(p),bound=p.getBoundingClientRect();if(['auto','scroll'].includes(style.overflowX) && p.scrollWidth>p.clientWidth+1)return false;if(style.overflowX==='hidden' && rect.right>bound.right+1)return true;}
          return false;
        }).map(el=>({page:state.page,id:el.id,text:el.textContent}))'''))
    assert not clipped,clipped


def test_server_fixture_does_not_mutate_environment(server):
    assert os.environ.get('MEDIADECK_ADMIN_USER') != 'demo-admin'
    assert os.environ.get('MEDIADECK_ADMIN_PASSWORD') != 'local-demo-only'


DUPLICATE_FORMS = [
    ('redeem', 'rd-make', '/api/redeem/generate', {}),
    ('invites', 'iq-give', '/api/members/audit-viewer/invite-quota', {}),
    ('invites', 'gr-add', '/api/registration/grants', {'gr-id':'81000001'}),
    ('storage', 'sr-add', '/api/storage/remotes', {'sr-name':'demo-remote','sr-type':'drive'}),
    ('storage', 'sm-add', '/api/storage/mounts', {'sm-name':'demo-mount','sm-target':'demo-mount'}),
    ('shop', 'sh-add', '/api/shop/items', {'sh-name':'Demo item'}),
    ('groups', 'group-create', '/api/groups', {'new-id':'demo-group','new-name':'Demo group'}),
    ('nodes', 'nd-go', '/api/nodes', {'nd-name':'demo-node'}),
    ('tgbot?section=notifications', 'tg-sendrank', '/api/telegram/rankings/send', {}),
]


@pytest.mark.parametrize('route,button,path,fields', DUPLICATE_FORMS)
@pytest.mark.parametrize('status', [200,503])
def test_side_effect_form_double_click_failure_retry(page, route, button, path, fields, status):
    sent=[]
    def fail(r):
        if r.request.method=='POST':
            sent.append(r.request.post_data_json)
            r.fulfill(status=status,json={'detail':'demo failure'} if status==503 else {'codes':[{'code':'demo-code','batch':'demo-batch'}],'name':'demo-node','quota':3,'sent':True})
        else:
            r.continue_()
    page.route('**'+path,fail)
    go(page,route)
    if button=='group-create':
        page.locator('.hg-group-new>summary').click()
    for key,value in fields.items():
        page.locator('#'+key).fill(value)
    if button=='iq-give':
        page.locator('#iq-member').select_option('audit-viewer')
    hold(page,path,'POST')
    page.evaluate('id=>{const b=document.getElementById(id);b.click();b.click()}',button)
    page.wait_for_function('auditWaiting')
    assert len(sent)==1
    page.evaluate('auditRelease()')
    expect(page.locator('#'+button)).to_be_enabled()
    if status==503:
        for key,value in fields.items():
            expect(page.locator('#'+key)).to_have_value(value)
    else:
        page.wait_for_timeout(100)
        assert '失败' not in page.locator('#toast').inner_text()


def test_group_policy_preview_does_not_discard_editor(page):
    go(page,'groups')
    page.evaluate("editGroup('standard')")
    page.locator('#edit-name').fill('Demo unsaved group')
    writes=[]
    page.on('request',lambda r:writes.append(r.url) if r.method in ('POST','PUT','DELETE') else None)
    page.locator('#group-preview').click()
    page.wait_for_timeout(150)
    expect(page.locator('#edit-name')).to_have_value('Demo unsaved group')
    assert not writes
    expect(page.locator('#group-preview')).to_contain_text('当前')


def test_old_group_save_does_not_close_new_editor(page):
    go(page,'groups')
    page.route('**/api/groups/standard', lambda r:r.fulfill(json={'id':'standard'}))
    hold(page,'/api/groups/standard','PUT')
    page.evaluate("editGroup('standard')")
    page.on('dialog',lambda d:d.accept())
    page.locator('#group-save').click()
    page.wait_for_function('auditWaiting')
    page.locator('#modal-close').click()
    page.evaluate("editGroup('vip')")
    page.locator('#edit-name').fill('Demo new draft')
    page.evaluate('auditRelease()')
    page.wait_for_timeout(150)
    expect(page.locator('#edit-name')).to_have_value('Demo new draft')


def test_inline_display_name_cannot_execute_js(page):
    go(page,'shop')
    page.evaluate('''()=>{const name="Demo'-alert(123)-'";document.querySelector('#view').innerHTML=`<button id=probe onclick="deleteShopItem(1, '${q(name)}')">delete</button>`;}''')
    dialogs=[]
    page.on('dialog',lambda d:(dialogs.append(d.type),d.dismiss()))
    page.locator('#probe').click()
    assert dialogs==['confirm']


def test_expiry_precision_and_past_boundary(page):
    stamp=int(time.time())+86423
    got=page.evaluate('''stamp=>{const overrides={expires_at_override:stamp};openModal('Edit',overrideEditor({overrides},[]));return collectOverridesFromForm(overrides)}''',stamp)
    assert got['expires_at_override']==stamp
    assert page.evaluate('fmtExpiry(Date.now()/1000-1)')=='已过期'


def test_visible_form_controls_have_names(page):
    missing=[]
    for route in page.evaluate('NAV.flatMap(g=>g.items).map(it=>it.id)'):
        go(page,route)
        missing.extend(page.evaluate('''()=>[...document.querySelectorAll('#view input,#view select,#view textarea')].filter(el=>el.offsetParent!==null&&!el.disabled&&!el.readOnly).filter(el=>
          !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby') && !el.title && ![...(el.labels||[])].some(label=>label.textContent.trim())
        ).map(el=>({page:state.page,id:el.id,cls:el.className,placeholder:el.placeholder}))'''))
    if ARTIFACTS:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / 'web-form-names.json').write_text(json.dumps(missing,ensure_ascii=False,indent=2))
    assert not missing,missing


@pytest.mark.parametrize('action,path,answer', [
    ('status','/api/members/audit-viewer/status',''),
    ('password','/api/members/audit-viewer/password','demo-pass-only'),
    ('kick','/api/members/audit-viewer/kick',''),
    ('reset-traffic','/api/members/audit-viewer/reset-traffic',''),
])
@pytest.mark.parametrize('accept',[False,True])
def test_member_actions_cancel_failure_keep_ui(page,action,path,answer,accept):
    go(page,'members?id=audit-viewer&tab=overview')
    page.locator('.hg-account-more>summary').click()
    sent=[]
    page.route('**'+path,lambda r:(sent.append(r.request.post_data_json),r.fulfill(status=503,json={'detail':'demo unavailable'})))
    page.on('dialog',lambda d:d.accept(answer) if accept and d.type=='prompt' else d.accept() if accept else d.dismiss())
    button=page.locator('[data-member-action="'+action+'"]')
    button.click()
    if accept:
        expect(page.locator('#toast')).to_contain_text('操作失败')
        assert len(sent)==1
        expect(button).to_be_enabled()
    else:
        assert not sent
    expect(page.locator('#md-title')).to_have_text('DemoViewer')


def test_member_delete_double_preview_and_close_cancel(page):
    go(page,'members')
    route='/api/members/audit-viewer/delete-preview'
    calls=[]
    page.route('**'+route+'**',lambda r:(calls.append(r.request.url),r.fulfill(json={'objects':[{'emby_user_id':'audit-viewer','username':'DemoViewer'}]})))
    hold(page,route)
    page.evaluate('''()=>{const b=document.querySelector('tr[data-id="audit-viewer"] [data-act="delete"]');b.click();b.click();}''')
    page.wait_for_function('auditWaiting')
    assert len(calls)==1
    dialogs=[]
    page.on('dialog',lambda d:(dialogs.append(d.type),d.dismiss()))
    go(page,'tggroup')
    page.evaluate('auditRelease()')
    page.wait_for_timeout(100)
    assert not dialogs


def test_late_intake_refresh_cannot_erase_new_visit(page):
    go(page,'intake')
    page.route('**/api/intake/refresh',lambda r:r.fulfill(json={'ok':True}))
    hold(page,'/api/intake/refresh','POST')
    page.locator('#intake-refresh').click()
    page.wait_for_function('auditWaiting')
    go(page,'tggroup')
    go(page,'intake')
    page.evaluate("document.querySelector('#intake-refresh').dataset.newVisit='true'")
    page.evaluate('auditRelease()')
    page.wait_for_timeout(100)
    assert page.locator('#intake-refresh').get_attribute('data-new-visit')=='true'


def test_old_import_result_preserves_new_visit_draft(page):
    page.route('**/api/imports',lambda r:r.fulfill(json={'id':'demo-import'}) if r.request.method=='POST' else r.continue_())
    go(page,'imports')
    hold(page,'/api/imports','POST')
    page.locator('#imp-src').fill('first draft')
    page.locator('#imp-go').click()
    page.wait_for_function('auditWaiting')
    go(page,'tggroup')
    go(page,'imports')
    page.locator('#imp-src').fill('new visit draft')
    page.evaluate('auditRelease()')
    page.wait_for_timeout(150)
    expect(page.locator('#imp-src')).to_have_value('new visit draft')


def test_node_mapping_delete_can_be_cancelled(page):
    go(page,'nodes')
    sent=[]
    page.on('request',lambda r:sent.append(r.url) if r.method=='PUT' else None)
    page.on('dialog',lambda d:d.dismiss())
    page.evaluate("delPool(state.nodes[0].name,0)")
    assert not sent


def test_failed_member_renew_keeps_draft(page):
    go(page,'members?id=audit-viewer&tab=overview')
    page.route('**/api/members/audit-viewer/renew-preview?**',lambda r:r.fulfill(json={'allowed':True,'current_expires_at_effective':time.time()+10,'new_expires_at':time.time()+100}))
    page.route('**/api/members/audit-viewer/renew',lambda r:r.fulfill(json={'ok':False,'error':'demo remote failure'}))
    page.on('dialog',lambda d:d.accept())
    page.locator('#md-days').fill('41')
    page.locator('#md-renew').click()
    expect(page.locator('#toast')).to_contain_text('demo remote failure')
    expect(page.locator('#md-days')).to_have_value('41')


def test_node_rates_expire_including_card_subtitle(page):
    go(page,'nodes')
    page.evaluate('''()=>{const n=PAGE_MODELS.nodes.ns[0];Object.assign(n,{egress_mbps:8.388608,egress_sampled_at:Date.now()/1000,egress_status:'fresh',ok:true});paintNodes(PAGE_MODELS.nodes,pageContext('nodes'));}''')
    assert '1.00 MiB/s' in page.locator('#view').inner_text()
    page.evaluate('''()=>{document.querySelectorAll('[data-rate-at]').forEach(el=>el.dataset.rateAt=Date.now()/1000-30);ageLiveRates();}''')
    assert '1.00 MiB/s' not in page.locator('#view').inner_text()
    assert not page.evaluate("egressValid({egress_mbps:1,egress_sampled_at:Date.now()/1000+60,ok:true})")


def test_clear_overrides_preserves_new_inflight_edit(page):
    go(page,'members?id=audit-viewer&tab=entitlements')
    page.route('**/api/members/audit-viewer/overrides',lambda r:r.fulfill(json={'emby_user_id':'audit-viewer','overrides':{}}))
    hold(page,'/api/members/audit-viewer/overrides','PUT')
    page.on('dialog',lambda d:d.accept())
    page.locator('#ov-clear').click()
    page.wait_for_function('auditWaiting')
    page.locator('#ov-streams').fill('9')
    page.evaluate('auditRelease()')
    page.wait_for_timeout(150)
    expect(page.locator('#ov-streams')).to_have_value('9')
