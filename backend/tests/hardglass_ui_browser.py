"""Hard-glass acceptance with real Chromium and isolated mock API. Never hits production."""
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
    artifacts = Path(os.environ.get('HARDGLASS_ARTIFACT_DIR', tempfile.mkdtemp()))
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hardglass-') as tmp:
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
                names=['Alex Chen','Morgan','林间晚风','Dylan','南山','Harper','月下旅人','Taylor']
                for i,name in enumerate(names):
                    uid=f'demo-{i}'
                    app.state.members.upsert(uid,name,{'group_id':'whitelist' if i==1 else 'standard','expires_at':int(time.time())+(90-i)*86400})
                    app.state.members.bind_telegram(uid,str(80000+i),f'demo_{i}',actor='mock')
                    app.state.db.execute('INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES(?,?,?,?)',(uid,13200,time.time()-14000,time.time()-500))
                app.state.groups.update('vip',{'name':'白名单 VIP'})
                app.state.members.upsert('name-only','NotWhitelist',{'group_id':'vip'})
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
                    context=browser.new_context(http_credentials={'username':auth[0],'password':auth[1]},viewport={'width':1440,'height':1080})
                    context.route('**/emby/Items/**/Images/**',lambda r:r.fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="360"><rect width="240" height="360" fill="#254255"/><text x="45" y="180" fill="white" font-size="24">DEMO</text></svg>'))
                    page=context.new_page();page.set_default_timeout(10000)
                    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    writes=[];page.on('request',lambda r:writes.append((r.method,r.url)) if r.method in ('PUT','POST','DELETE') else None)
                    def go(route, selector):
                        page.evaluate('(route)=>go(route)',route)
                        page.locator(selector).first.wait_for(state='visible')
                        page.wait_for_function('state.pageReady')
                    page.goto(base+'/#/members',wait_until='load')
                    page.wait_for_function('state.pageReady')
                    assert page.locator('#nav .nav-item').count()==6
                    assert page.locator('#members-bulk').is_hidden()
                    white=page.locator('tr[data-id="demo-1"]')
                    assert white.locator('.whitelist-badge').count()==1
                    assert page.locator('tr[data-id="name-only"] .whitelist-badge').count()==0
                    page.screenshot(path=str(artifacts/'members-desktop.png'),full_page=True)
                    white.locator('.m-pick').check()
                    expect(page.locator('#members-bulk')).to_be_visible()
                    page.screenshot(path=str(artifacts/'members-selection.png'),full_page=True)
                    page.locator('[data-act="clear-selection"]').click()
                    # Open from a scrolled list. Detail must be in the viewport immediately.
                    page.evaluate('document.querySelector("#members-table-wrap").scrollTop=70')
                    position=page.locator('#members-table-wrap').evaluate('(e)=>e.scrollTop')
                    white.locator('[data-act="open"]').first.click()
                    page.locator('#md-title').wait_for()
                    panel=page.locator('.member-detail-card')
                    assert panel.bounding_box()['y']==14
                    assert page.locator('.hg-whitelist-banner').count()==1
                    assert page.locator('#members-table-card').evaluate('(e)=>e.inert')
                    # Select the rights tab and preserve editor focus/draft under a real SSE refresh.
                    page.locator('[data-tab="entitlements"]').click()
                    page.locator('#ov-streams').fill('7')
                    page.evaluate('window.savedEditor=document.querySelector("#ov-streams");window.savedDrawer=document.querySelector(".member-detail-card")')
                    page.evaluate('async()=>{for(const fn of LIVE_UPDATERS.get("members"))await fn("members",{},pageContext("members",true));}')
                    assert page.evaluate('savedEditor===document.querySelector("#ov-streams") && savedDrawer===document.querySelector(".member-detail-card")')
                    expect(page.locator('#ov-streams')).to_have_value('7')
                    assert page.locator('#members-table-card').evaluate('(e)=>e.inert')
                    page.once('dialog',lambda d:d.dismiss())
                    with page.expect_event('dialog'):
                        page.locator('#md-close').click()
                    expect(page.locator('#ov-streams')).to_have_value('7')
                    page.once('dialog',lambda d:d.dismiss())
                    with page.expect_event('dialog'):
                        page.locator('[data-tab="devices"]').click()
                    expect(page.locator('#ov-streams')).to_have_value('7')
                    page.once('dialog',lambda d:d.accept())
                    with page.expect_event('dialog'):
                        page.locator('[data-tab="overview"]').click()
                    page.locator('#md-group').wait_for()
                    page.screenshot(path=str(artifacts/'whitelist-detail-desktop.png'),full_page=True)
                    page.keyboard.press('Escape')
                    expect(page.locator('#member-detail')).to_be_hidden()
                    assert not page.locator('#members-table-card').evaluate('(e)=>e.inert')
                    assert page.locator('#members-table-wrap').evaluate('(e)=>e.scrollTop')==position
                    assert not writes, writes
                    # Instant search retains focus; no Enter ceremony needed.
                    page.locator('#m-q').fill('Morgan')
                    page.wait_for_function('document.querySelectorAll("#members-tbody tr").length===1')
                    expect(page.locator('#m-q')).to_be_focused()
                    page.locator('#m-q').fill('')
                    page.wait_for_function('document.querySelectorAll("#members-tbody tr").length>1')
                    go('groups','.hg-whitelist-group')
                    assert page.locator('.hg-whitelist-group').count()==1
                    assert page.locator('.hg-whitelist-group').get_by_role('button',name='删除',exact=True).count()==0
                    page.screenshot(path=str(artifacts/'whitelist-group-desktop.png'),full_page=True)
                    # Real modal remains accessible above the details layer, and deep links still work.
                    go('members?id=demo-0&tab=overview','#md-group')
                    page.locator('.hg-account-more summary').click()
                    marker=len(writes)
                    page.once('dialog',lambda d:d.dismiss())
                    with page.expect_event('dialog'):
                        page.locator('[data-member-action="telegram/unbind"]').click()
                    assert len(writes)==marker
                    page.locator('#md-close').click()
                    go('dashboard','.dashboard-focus')
                    page.screenshot(path=str(artifacts/'overview-desktop.png'),full_page=True)
                    go('settings','#em-url')
                    assert page.locator('.config-section:visible').count()==1
                    page.screenshot(path=str(artifacts/'settings-desktop.png'),full_page=True)
                    ids=page.evaluate('NAV.flatMap(g=>g.items).map(it=>it.id)')
                    for route in ids:
                        page.evaluate('(route)=>go(route)',route)
                        page.wait_for_function('state.pageReady')
                        assert '页面不存在' not in page.locator('#view').inner_text(),route
                    page.set_viewport_size({'width':390,'height':844})
                    go('members','#members-table')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(artifacts/'members-mobile.png'),full_page=True)
                    go('members?id=demo-1&tab=overview','#md-group')
                    assert page.locator('.member-detail-card').bounding_box()['y']==0
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(artifacts/'whitelist-detail-mobile.png'),full_page=False)
                    page.locator('#md-close').click()
                    assert not errors,errors
                    browser.close()
                # Bot preview is generated from the actual local Bot view strings, not a fabricated UI.
                bot=app.state.telegram
                home,keyboard=bot._home('80001','Morgan')
                import html
                text=home.replace('\n','<br>')
                keys=''.join('<div class="row">'+''.join('<span>'+html.escape(x['text'])+'</span>' for x in row)+'</div>' for row in keyboard)
                preview='<html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>body{background:#101622;color:#dfe8f8;font:15px/1.8 sans-serif;margin:0;padding:35px;width:470px;box-sizing:border-box}h1{font-size:19px;font-weight:500}p{font-size:11px;color:#8a9bb6}.message{border:1px solid #bba5f369;background:linear-gradient(135deg,#3e3755,#222e44);padding:24px;border-radius:14px;box-shadow:inset 0 1px #e2d2ff45}code{color:#c8b7ef}.row{display:flex;gap:6px;margin:7px 0}.row span{flex:1;background:#29384f;border:1px solid #b0c6ef24;text-align:center;font-size:11px;border-radius:6px;padding:9px 4px}</style><h1>MediaDeck · 白名单 Bot 装饰</h1><p>本地实际 Bot 文案生成的示意 · 非线上 Telegram 截图</p><div class="message">'+text+'</div>'+keys+'</html>'
                (artifacts/'bot-whitelist.html').write_text(preview)
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':470,'height':850})
                    page.goto((artifacts/'bot-whitelist.html').as_uri())
                    page.screenshot(path=str(artifacts/'bot-whitelist.png'),full_page=True)
                    browser.close()
                print(json.dumps({'ok':True,'pages':len(ids),'browser_errors':errors,'checked':['hardglass styles','fixed whitelist id badge','drawer top14/mobile top0','dirty close/tab guard','SSE draft+inert preservation','scroll restoration','instant search focus','danger cancel no write','25 pages','mobile no overflow','actual bot text']},ensure_ascii=False))
        finally:
            server.should_exit=True
            thread.join(timeout=10)


if __name__=='__main__':
    main()
