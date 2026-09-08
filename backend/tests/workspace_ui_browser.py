"""Real browser acceptance against an isolated local mock server. No live credentials."""
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright


def main():
    artifact_dir = Path(os.environ.get('WORKSPACE_UI_ARTIFACT_DIR', tempfile.mkdtemp()))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workspace-ui-') as tmp:
        os.environ.update(MEDIADECK_MOCK='1', MEDIADECK_DATA_DIR=tmp,
                          MEDIADECK_ADMIN_USER='demo-admin', MEDIADECK_ADMIN_PASSWORD='local-demo-only')
        from app.core.config import settings
        settings.cache_clear()
        from app.main import app
        sock = socket.socket()
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
        sock.close()
        server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread = threading.Thread(target=server.run,daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{port}'
        auth = ('demo-admin','local-demo-only')
        try:
            with httpx.Client(base_url=base,auth=auth,timeout=15) as client:
                for _ in range(150):
                    try:
                        if client.get('/healthz').status_code==200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.05)
                else:
                    raise RuntimeError('mock startup timeout')
                assert client.put('/api/members/demo-viewer',json={'username':'DemoUser','group_id':'standard'}).status_code==200
                now = time.time()
                app.state.db.execute('INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES(?,?,?,?)',('demo-viewer',1200,now-86400-600,now-86400+1200))
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
                    ctx = browser.new_context(http_credentials={'username':auth[0],'password':auth[1]},viewport={'width':1440,'height':1000})
                    ctx.route('**/emby/Items/**/Images/**', lambda r:r.fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="360"><rect width="240" height="360" fill="#254255"/><text x="50" y="180" fill="white" font-size="28">DEMO</text></svg>'))
                    page = ctx.new_page()
                    page.set_default_timeout(8000)
                    errors = []
                    writes = []
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('request',lambda r:writes.append((r.method,r.url,r.post_data)) if r.method in ('PUT','POST','DELETE') else None)
                    def route(name, selector):
                        page.evaluate('(name)=>go(name)',name)
                        page.locator(selector).first.wait_for(state='visible')
                        page.wait_for_function('state.pageReady')
                    page.goto(base+'/#/dashboard',wait_until='domcontentloaded')
                    page.wait_for_function('state.pageReady')
                    assert page.locator('#nav .nav-item').count()==6
                    assert page.locator('#view h3').all_text_contents()==['正在播放','待处理事项']
                    assert not page.locator('[href="#/tgrequests"]').count()
                    page.screenshot(path=str(artifact_dir/'overview-desktop.png'),full_page=True)
                    route('settings','#em-url')
                    expect(page.locator('.config-section:visible')).to_have_count(1)
                    # Independent saves, failed requests and dirty navigation are exercised on real endpoints.
                    page.locator('#em-url').fill('http://service.example.test:8096')
                    page.locator('[data-config-section="membership"]').click()
                    page.locator('#mb-interval').fill('20')
                    with page.expect_response(lambda r:r.url.endswith('/api/settings/membership') and r.request.method=='PUT'):
                        page.locator('#mb-save').click()
                    expect(page.locator('.config-section:visible .save-feedback')).to_contain_text('已保存')
                    assert client.get('/api/settings').json()['emby']['url']!='http://service.example.test:8096'
                    assert page.evaluate('configDirty()')
                    page.locator('[data-config-section="connections"]').click()
                    expect(page.locator('#em-url')).to_have_value('http://service.example.test:8096')
                    page.route('**/api/settings/emby',lambda r:r.fulfill(status=503,content_type='application/json',body='{"detail":"temporary failure"}') if r.request.method=='PUT' else r.continue_())
                    page.locator('#em-save').click()
                    expect(page.locator('#em-save').locator('xpath=ancestor::div[contains(@class,"card")][last()]').locator('.save-feedback')).to_contain_text('保存失败')
                    expect(page.locator('#em-url')).to_have_value('http://service.example.test:8096')
                    page.unroute('**/api/settings/emby')
                    # Click navigation and cancel: hash and visible form both stay put.
                    page.once('dialog',lambda d:d.dismiss())
                    with page.expect_event('dialog'):
                        page.locator('#nav a[data-page="members"]').click()
                    page.wait_for_function("location.hash.startsWith('#/settings') && state.page === 'settings'")
                    expect(page.locator('#em-url')).to_be_visible()
                    page.once('dialog',lambda d:d.dismiss())
                    page.locator('#topbar').get_by_role('button',name='刷新',exact=True).click()
                    expect(page.locator('#em-url')).to_have_value('http://service.example.test:8096')
                    with page.expect_response(lambda r:r.url.endswith('/api/settings/emby') and r.request.method=='PUT'):
                        page.locator('#em-save').click()
                    page.wait_for_function('!configDirty()')
                    page.screenshot(path=str(artifact_dir/'settings-desktop.png'),full_page=True)
                    assert page.locator('#em-timeout').is_hidden()
                    route('tgbot?section=appearance','#tg-logo')
                    page.locator('#tg-logo').fill('https://assets.example.test/logo.png')
                    page.locator('[data-config-section="registration"]').click()
                    before = client.get('/api/settings/telegram').json()
                    page.locator('#tg-regdays').fill(str(before['register_days']+1))
                    page.locator('#tg-save2').click()
                    expect(page.locator('.config-section:visible .save-feedback')).to_contain_text('已保存')
                    assert client.get('/api/settings/telegram').json()['menu_logo_url']==before['menu_logo_url']
                    page.locator('[data-config-section="appearance"]').click()
                    expect(page.locator('#tg-logo')).to_have_value('https://assets.example.test/logo.png')
                    page.locator('#tg-save-logo').click()
                    expect(page.locator('.config-section:visible .save-feedback')).to_contain_text('已保存')
                    assert client.get('/api/settings/telegram').json()['menu_logo_url']=='https://assets.example.test/logo.png'
                    page.locator('#tg-logo-clear').click()
                    assert page.evaluate('configDirty()')
                    page.locator('#tg-save-logo').click()
                    page.wait_for_function('!configDirty()')
                    page.locator('[data-config-section="groups"]').click()
                    page.locator('#tg-reviewgroups').fill('-100123456789')
                    page.locator('#tg-save-groups').click()
                    page.wait_for_function('!configDirty()')
                    assert client.get('/api/settings/telegram').json()['group_interaction_chats']==['-100123456789']
                    page.screenshot(path=str(artifact_dir/'bot-groups-desktop.png'),full_page=True)
                    page.locator('[data-config-section="connection"]').click()
                    marker = len(writes)
                    page.locator('#tg-test').click()
                    expect(page.locator('#tg-result')).not_to_have_text('测试中…')
                    assert [x for x in writes[marker:] if x[0] in ('POST','PUT')]==[x for x in writes[marker:] if x[1].endswith('/verify')]
                    route('tgrequests','#tg-reviewgroups')
                    assert '#/tgbot?section=groups' in page.url
                    route('members?id=demo-viewer&tab=overview','#md-group')
                    text = page.locator('#view').inner_text()
                    assert '有跨界历史，时长无法完整还原' in text
                    assert '直链 7/30/累计' not in text and '会话估算' not in text
                    page.screenshot(path=str(artifact_dir/'member-watch-desktop.png'),full_page=True)
                    route('stats','#trend-svg')
                    text = page.locator('#view').inner_text()
                    assert '本月实测流量' in text and '暂无实测记录' in text
                    page.screenshot(path=str(artifact_dir/'statistics-desktop.png'),full_page=True)
                    # Playback quality: correct poster reference, landscape contain, failed image,
                    # account-wide measured rate and stable artwork under live patches.
                    poster_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><defs><linearGradient id="g" x2="0" y2="1"><stop stop-color="#172d52"/><stop offset="1" stop-color="#446d75"/></linearGradient></defs><rect width="400" height="600" fill="url(#g)"/><circle cx="285" cy="165" r="70" fill="#efca8c"/><path d="M0 450 130 250 250 470 330 330 400 450V600H0" fill="#102234"/><path d="M0 510 190 380 400 520V600H0" fill="#0a172a"/><text x="35" y="78" font-size="35" font-family="serif" letter-spacing="5" fill="#fff4d6">NIGHTFALL</text><text x="38" y="550" font-size="15" letter-spacing="7" fill="#bad4db">DEMO SERIES</text></svg>'
                    ctx.route('**/emby/Items/series-art/Images/**', lambda r:r.fulfill(status=200,content_type='image/svg+xml',body=poster_svg))
                    ctx.route('**/emby/Items/broken-art/Images/**', lambda r:r.fulfill(status=404,body=''))
                    ctx.route('**/emby/Items/wide-art/Images/**', lambda r:r.fulfill(status=200,content_type='image/svg+xml',body=poster_svg.replace('width="400" height="600"','width="800" height="450"',1)))
                    route('dashboard','.dashboard-focus')
                    page.evaluate("live.src?.close(); live.pending.clear();")
                    demo = {'Id':'poster-demo','UserId':'demo','UserName':'DemoViewer','Client':'Demo Player','Item':'第 03 集','SeriesName':'夜幕之下','ItemId':'episode-art','PosterItemId':'series-art','PosterKind':'series','ItemType':'Episode','Overview':'海报与播放信息分区展示，观看进度和实时实测各有清晰位置。','Paused':False,'ProgressPercent':38,'RunTimeTicks':36000000000,'PositionTicks':13680000000,'SpeedSource':'node','SpeedBps':2*1048576,'SpeedScope':'user','SpeedAccountSessions':2,'SpeedCollectedAt':time.time(),'SpeedTimeBasis':'collector','SpeedWindowSeconds':8}
                    page.evaluate('(s)=>{PAGE_MODELS.dashboard.sessions=[s];paintDashboard(PAGE_MODELS.dashboard,pageContext("dashboard"));}',demo)
                    page.locator('.pc-art.loaded').wait_for()
                    assert '/series-art/' in page.locator('.pc-art-image').get_attribute('src')
                    assert '2.00 MiB/s' in page.locator('.pc-speed').inner_text()
                    assert '不可相加' in page.locator('.pc-speed').inner_text()
                    page.evaluate('window.qualityImage=document.querySelector(".pc-art-image")')
                    page.evaluate('()=>{PAGE_MODELS.dashboard.sessions[0].ProgressPercent=40;paintDashboard(PAGE_MODELS.dashboard,pageContext("dashboard",true));}')
                    assert page.evaluate('window.qualityImage===document.querySelector(".pc-art-image")')
                    page.screenshot(path=str(artifact_dir/'playback-poster-desktop.png'),full_page=True)
                    page.evaluate('()=>{const s=PAGE_MODELS.dashboard.sessions[0];s.PosterItemId="wide-art";s.PosterAspectRatio=16/9;paintDashboard(PAGE_MODELS.dashboard,pageContext("dashboard",true));}')
                    page.locator('.pc-art.loaded').wait_for()
                    assert page.locator('.pc-art-image').evaluate('(el)=>getComputedStyle(el).objectFit')=='contain'
                    page.evaluate('()=>{PAGE_MODELS.dashboard.sessions[0].PosterItemId="broken-art";paintDashboard(PAGE_MODELS.dashboard,pageContext("dashboard",true));}')
                    page.locator('.pc-art.failed').wait_for()
                    expect(page.locator('.pc-art-fallback')).to_be_visible()
                    page.evaluate('()=>{document.querySelector(".live-rate").dataset.rateAt=(Date.now()/1000-30);ageLiveRates();}')
                    assert '采样已过期' in page.locator('.pc-speed').inner_text()
                    page.set_viewport_size({'width':390,'height':844})
                    demo['SpeedCollectedAt']=time.time()
                    page.evaluate('(s)=>{PAGE_MODELS.dashboard.sessions=[s];paintDashboard(PAGE_MODELS.dashboard,pageContext("dashboard"));}',demo)
                    page.locator('.pc-art.loaded').wait_for()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(artifact_dir/'playback-poster-mobile.png'),full_page=True)
                    page.set_viewport_size({'width':1440,'height':1000})
                    route('nodepool','.np-weight')
                    page.evaluate('live.src?.close();live.pending.clear();')
                    weight=page.locator('.np-weight').first
                    weight.fill('321')
                    page.evaluate('window.qualityWeight=document.querySelector(".np-weight")')
                    page.evaluate('()=>{const data=PAGE_MODELS.nodepool.nodes.map(n=>({...n,ok:true,egress_status:"fresh",egress_mbps:8.388608,egress_sampled_at:Date.now()/1000,egress_time_basis:"collector",egress_window_seconds:8}));for(const h of LIVE_UPDATERS.get("nodepool"))h("nodes",data,pageContext("nodepool",true));}')
                    assert page.evaluate('LIVE.nodepool.includes("nodes") && window.qualityWeight===document.querySelector(".np-weight")')
                    expect(weight).to_have_value('321')
                    assert '1.00 MiB/s' in page.locator('[data-live-key^="pool:"]').first.inner_text()
                    page.evaluate('()=>{const data=PAGE_MODELS.nodepool.nodes.map(n=>({...n,ok:false,egress_status:"unavailable",egress_mbps:null}));for(const h of LIVE_UPDATERS.get("nodepool"))h("nodes",data,pageContext("nodepool",true));}')
                    assert '暂无有效实测' in page.locator('#view').inner_text()
                    expect(weight).to_have_value('321')
                    print('PASS: poster selection/contain/fallback/stable SSE/mobile, rate expiry/units/scope, nodepool live update/draft preservation')
                    # Every existing low-frequency page remains reachable under its workspace.
                    ids = page.evaluate('NAV.flatMap(g=>g.items).map(it=>it.id)')
                    for name in ids:
                        page.evaluate('(name)=>go(name)', name)
                        page.wait_for_function('state.pageReady')
                        assert '页面不存在' not in page.locator('#view').inner_text(), name
                    # Mobile navigation uses the same anchors; no hidden permanent sidebar or overflow.
                    page.set_viewport_size({'width':390,'height':844})
                    route('dashboard','.dashboard-focus')
                    page.locator('#nav-toggle').click()
                    expect(page.locator('#nav-toggle')).to_have_attribute('aria-expanded','true')
                    page.keyboard.press('Escape')
                    expect(page.locator('#nav-toggle')).to_have_attribute('aria-expanded','false')
                    page.locator('#nav-toggle').click()
                    page.locator('#nav-backdrop').click(position={'x':300,'y':200})
                    expect(page.locator('#sidebar')).to_be_hidden()
                    page.locator('#nav-toggle').click()
                    page.locator('#nav a[data-page="settings"]').click()
                    page.locator('#em-url').wait_for(state='visible')
                    expect(page.locator('#nav-toggle')).to_have_attribute('aria-expanded','false')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(artifact_dir/'settings-mobile.png'),full_page=True)
                    route('dashboard','.dashboard-focus')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(artifact_dir/'overview-mobile.png'),full_page=True)
                    assert not errors, errors
                    print(json.dumps({'ok':True,'pages':len(ids),'browser_errors':errors,'screenshots':9,'checked':['six_workspaces','one_playback_surface','scoped_saves','save_failure_keeps_input','cancel_navigation','cancel_refresh','logo_save_close','group_settings','verify_no_implicit_save','review_removed','watch_uncertainty','mobile']},ensure_ascii=False))
                    browser.close()
        finally:
            server.should_exit=True
            thread.join(timeout=10)


if __name__=='__main__':
    main()
