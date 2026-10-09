"""Real isolated API and actual Chromium UI: five switches, drafts and hidden risk fields."""
import copy
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from app.main import app
from app.modules.checkin_cleanup import CleanupService

IDS=('checkin_cleanup','blackwhite','niuniu','red_packets','points_ranking')


def test_five_plugin_api_live_readonly_unknown_preservation_validation_and_auth():
    with TestClient(app) as client:
        client.auth=('admin','change-me')
        registry=app.state.plugins
        for pid in IDS:
            registry.save(pid,enabled=True)
        raw=copy.deepcopy(registry._store.section('plugins'))
        for pid in IDS:
            raw[pid]['extra_top']={'keep':[1,2]}
            raw[pid].setdefault('config',{})['unknown_saved']={'nested':'keep'}
        registry._store.set_section('plugins',raw)
        before=app.state.db.query('SELECT * FROM points_ledger ORDER BY id')
        for pid in IDS:
            response=client.get('/api/plugins/'+pid)
            assert response.status_code==200 and 'live_status' in response.json()
            saved=client.post('/api/plugins/'+pid,json={'config':{'untrusted_new':'drop'}}).json()
            assert saved['config']['unknown_saved']=={'nested':'keep'} and 'untrusted_new' not in saved['config']
            assert registry._store.section('plugins')[pid]['extra_top']=={'keep':[1,2]}
        invalid=(('checkin_cleanup',{'delay_seconds':59}),('blackwhite',{'min_stake':200,'max_stake':100}),
                 ('niuniu',{'default_stake':30,'max_stake':20}),('red_packets',{'max_parts':0}),
                 ('points_ranking',{'page_size':4}),('niuniu',{'lobby_seconds':15.5}))
        for pid,cfg in invalid:
            old=copy.deepcopy(registry.config(pid))
            assert client.post('/api/plugins/'+pid,json={'config':cfg}).status_code==400
            assert registry.config(pid)==old
        assert app.state.db.query('SELECT * FROM points_ledger ORDER BY id')==before
        client.auth=None
        assert client.get('/api/plugins/niuniu').status_code==401
        assert client.post('/api/plugins/red_packets',json={'enabled':False}).status_code==401


def test_chromium_five_gemini_groups_advanced_hidden_save_drafts_and_real_issues(tmp_path,monkeypatch):
    monkeypatch.setenv('TMPDIR',str(tmp_path.parent))
    with TestClient(app) as client:
        client.auth=('admin','change-me')
        registry=app.state.plugins
        for pid in IDS:registry.save(pid,enabled=True)
        raw=copy.deepcopy(registry._store.section('plugins'))
        for pid in IDS:
            raw[pid]['operator_extension']='keep'
            raw[pid].setdefault('config',{})['future_config']={'retain':True}
        registry._store.set_section('plugins',raw)
        job=CleanupService(app.state.db).enqueue('123',-9000,9,40,'command','910',now=time.time()-61)
        app.state.db.execute("UPDATE play_jobs SET state='blocked',last_error='没有群删除权限，未删除' WHERE id=?",(job['id'],))
        before=app.state.db.query('SELECT * FROM points_ledger ORDER BY id')
        with sync_playwright() as driver:
            browser=driver.chromium.launch(executable_path=os.getenv('MEDIADECK_TEST_CHROMIUM',shutil.which('chromium') or '/usr/bin/chromium'),args=['--no-sandbox'],headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1100})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            def bridge(route):
                request=route.request;url=urlsplit(request.url)
                if url.hostname!='panel.test':route.abort();return
                if url.path=='/api/stream':route.fulfill(status=200,content_type='text/event-stream',body=': local\n\n');return
                response=client.request(request.method,url.path+('?' +url.query if url.query else ''),content=request.post_data_buffer,
                    headers={k:v for k,v in request.headers.items() if k not in ('host','authorization','content-length','accept-encoding')})
                route.fulfill(status=response.status_code,body=response.content,headers={k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding')})
            page.route('**/*',bridge)
            page.goto('http://panel.test/#automation')
            page.wait_for_function("typeof automation !== 'undefined'")
            page.evaluate("automation.category='points'; go('automation')")
            page.locator('#pl-points_ranking-page_size').wait_for()
            assert page.locator('.plugin-feature-group').count()==3
            for pid in IDS:
                assert page.locator(f'[data-plugin="{pid}"] .plugin-live').count()==1
                assert page.locator(f'#pl-{pid}-enabled').is_checked()
            for pid in ('blackwhite','niuniu'):
                advanced=page.locator(f'[data-plugin="{pid}"] .plugin-settings-group').last
                assert not advanced.evaluate('(e)=>e.open')
            cleanup=page.locator('[data-plugin="checkin_cleanup"]')
            assert '没有群删除权限，未删除' in cleanup.inner_text()
            assert '权限或归属待处理1' in cleanup.inner_text().replace('\n','')
            # An unrelated draft and a hidden advanced draft survive another card save and status refresh.
            page.fill('#pl-niuniu-default_stake','20')
            niuniu=page.locator('[data-plugin="niuniu"]')
            niuniu.locator('.plugin-settings-group').last.locator('summary').click()
            page.fill('#pl-niuniu-lobby_seconds','1200')
            niuniu.locator('.plugin-settings-group').last.locator('summary').click()
            page.fill('#pl-blackwhite-default_stake','50')
            # Concurrent backend change to an untouched known field and switch is not overwritten by this editor.
            registry.save('blackwhite',enabled=False,config={'max_stake':450})
            page.locator('[data-plugin="blackwhite"] [data-act="save"]').click()
            page.wait_for_function("document.querySelector('[data-plugin=blackwhite]').textContent.includes('已保存')")
            assert registry.config('blackwhite')['default_stake']==50 and registry.config('blackwhite')['max_stake']==450
            assert not registry.enabled('blackwhite')
            assert page.locator('#pl-blackwhite-max_stake').input_value()=='450'
            assert page.locator('#pl-niuniu-default_stake').input_value()=='20'
            assert page.locator('#pl-niuniu-lobby_seconds').input_value()=='1200'
            niuniu.locator('[data-act="refresh-status"]').click()
            page.wait_for_function("document.querySelector('[data-plugin=niuniu]').textContent.includes('状态已更新')")
            assert page.locator('#pl-niuniu-lobby_seconds').input_value()=='1200'
            niuniu.locator('[data-act="save"]').click()
            page.wait_for_function("document.querySelector('[data-plugin=niuniu]').textContent.includes('已保存')")
            assert registry.config('niuniu')['lobby_seconds']==1200
            page.fill('#pl-points_ranking-page_size','5')
            page.locator('[data-plugin="points_ranking"] [data-act="save"]').click()
            page.wait_for_function("document.querySelector('[data-plugin=points_ranking]').textContent.includes('已保存')")
            assert registry.config('points_ranking')['page_size']==5
            # Cross-field error leaves all draft values intact; no successful save or partial config change.
            page.fill('#pl-niuniu-max_stake','15')
            niuniu.locator('[data-act="save"]').click()
            page.wait_for_function("document.querySelector('[data-plugin=niuniu]').textContent.includes('保存失败')")
            assert page.locator('#pl-niuniu-default_stake').input_value()=='20' and page.locator('#pl-niuniu-max_stake').input_value()=='15'
            assert registry.config('niuniu')['max_stake']==500
            for pid in IDS:
                assert registry.config(pid)['future_config']=={'retain':True}
                assert registry._store.section('plugins')[pid]['operator_extension']=='keep'
            group=page.locator('.plugin-feature-group').nth(1)
            group.locator(':scope > summary').click()
            assert not group.evaluate('(e)=>e.open')
            group.locator(':scope > summary').click()
            assert page.locator('#pl-niuniu-default_stake').input_value()=='20'
            path=os.getenv('GAMES_ARTIFACTS')
            if path:
                page.locator('[data-plugin="niuniu"]').screenshot(path=str(Path(path,'admin-niuniu.png')))
                page.locator('[data-plugin="checkin_cleanup"]').screenshot(path=str(Path(path,'admin-cleanup.png')))
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.body.scrollWidth<=window.innerWidth+1')
            assert not errors
            assert app.state.db.query('SELECT * FROM points_ledger ORDER BY id')==before
            browser.close()
