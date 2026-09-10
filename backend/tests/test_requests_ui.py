"""Chromium exercises actual panel scripts against isolated real FastAPI request APIs.

All browser network requests are intercepted: local app TestClient or local poster
fixture only. No production DB, Telegram, TMDB, service start or deployment.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app

CHROMIUM = shutil.which('chromium')


@pytest.mark.skipif(not CHROMIUM, reason='real Chromium required')
def test_request_center_real_browser(tmp_path):
    from playwright.sync_api import sync_playwright

    artifacts=Path(os.environ.get('MEDIADECK_REQUEST_ARTIFACTS',tmp_path/'artifacts'))
    artifacts.mkdir(parents=True,exist_ok=True)
    image=Image.new('RGB',(240,360),'#183743');draw=ImageDraw.Draw(image)
    draw.rectangle((20,20,220,340),outline='#70dfcb',width=4)
    draw.text((46,130),'LOCAL TEST\n\nMOVIE POSTER\n\n1999',fill='white')
    stream=io.BytesIO();image.save(stream,format='PNG');poster=stream.getvalue()
    checks=[];posts=[];errors=[]
    with TestClient(app) as client:
        assert not app.state.telegram.enabled
        members=app.state.members;service=app.state.requests
        members.upsert('browser-u','浏览器验收用户',{'group_id':'standard'},actor='test')
        app.state.groups.update('standard',{'request_quota':0})
        for n in range(13):
            kind='tv' if n%2 else 'movie'
            demand={'scope':'episodes','seasons':[1],'episodes':[2,3],'quality':'4K','subtitle':'简中'} if kind=='tv' else {'scope':'version','version':'导演剪辑版','quality':'1080p'}
            row=asyncio.run(service.create('browser-u',kind,500+n,note='希望保留原声',demand=demand))
            app.state.db.execute('UPDATE media_requests SET title=?,year=?,poster_path=?,original_title=?,overview=? WHERE id=?',
                ('测试剧集' if kind=='tv' else '搏击俱乐部',1999,'/local-test.png','Fight Club' if kind=='movie' else 'Local Series','此简介仅为本地验收夹具。',row['id']))
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=CHROMIUM,headless=True,args=['--no-sandbox','--disable-background-networking'])
            context=browser.new_context(viewport={'width':1440,'height':1100},device_scale_factor=1)
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            def route_handler(route):
                request=route.request;url=urlsplit(request.url)
                if url.hostname=='image.tmdb.org':
                    route.fulfill(status=200,content_type='image/png',body=poster);return
                if url.hostname!='request-center.test':
                    route.abort();return
                if url.path=='/api/stream':
                    route.fulfill(status=200,content_type='text/event-stream',body=': local test\n\n');return
                path=url.path+('?' + url.query if url.query else '')
                if request.method=='POST': posts.append((path,json.loads(request.post_data or '{}')))
                response=client.request(request.method,path,auth=('admin','change-me'),content=request.post_data,
                    headers={'content-type':request.headers.get('content-type','application/json')})
                route.fulfill(status=response.status_code,content_type=response.headers.get('content-type','text/plain'),body=response.content)
            context.route('**/*',route_handler)
            page.goto('https://request-center.test/#/requests');page.wait_for_selector('#request-search')
            page.wait_for_function("document.querySelectorAll('#view tbody tr').length === 10")
            assert '待接单' not in page.locator('#view').inner_text() and '处理中' not in page.locator('#view').inner_text()
            assert page.locator('#view img').first.evaluate('(e)=>e.complete && e.naturalWidth>0')
            checks.append('真实列表载入10条、真实图片元素、无旧状态')
            page.screenshot(path=str(artifacts/'requests-desktop-list.png'),full_page=True)
            page.get_by_role('button',name='下一页',exact=True).click();page.wait_for_function("document.querySelectorAll('#view tbody tr').length === 3")
            checks.append('数据库分页第二页3条')
            page.select_option('#request-type','tv');page.fill('#request-search','测试剧集');page.get_by_role('button',name='搜索 / 筛选',exact=True).click()
            page.wait_for_function("document.querySelectorAll('#view tbody tr').length === 6")
            assert '季：1' in page.locator('#view').inner_text() and '集：2-3' in page.locator('#view').inner_text()
            checks.append('剧集搜索筛选及季集需求可见')
            page.get_by_role('button',name='详情',exact=True).first.click();page.wait_for_selector('#view h2')
            assert 'Local Series' in page.locator('#view').inner_text()
            assert '本地验收夹具' in page.locator('#view').inner_text()
            rid=int(page.locator('#view h2').inner_text().split('#')[1].split(' ')[0])
            checks.append('真实工单详情原名简介与需求')
            def prompt(text):
                page.wait_for_selector('.deck-dialog-card:not(.deck-dialog-closing)')
                page.locator('.deck-dialog-input').last.fill(text)
                page.locator('.deck-dialog-btn-confirm').last.click()
                page.wait_for_timeout(220)
            page.get_by_role('button',name='询问用户',exact=True).click();prompt('请确认配音版本')
            page.wait_for_function("document.querySelector('#view').textContent.includes('请确认配音版本')")
            assert service.get(rid)['status']=='open'
            checks.append('询问走实际API写记录，工单仍待处理')
            page.get_by_role('button',name='内部备注',exact=True).click();prompt('仅内部可见的片源备注')
            page.wait_for_function("document.querySelector('#view').textContent.includes('仅内部可见的片源备注')")
            assert '仅内部可见的片源备注' not in json.dumps(service.events(rid),ensure_ascii=False)
            checks.append('内部备注隔离')
            page.get_by_role('button',name='拒绝',exact=True).click()
            page.wait_for_selector('.deck-dialog-input')
            assert page.locator('.deck-dialog-input').last.input_value()==''
            prompt('')
            page.wait_for_function("document.querySelector('#view h2').textContent.includes('已拒绝')")
            assert service.get(rid)['result_note']==''
            assert page.get_by_role('button',name='接受请求',exact=True).count()==0
            checks.append('无理由直接拒绝，终态无重复处理按钮')
            page.get_by_role('button',name='管理员纠错',exact=True).click();prompt('open');prompt('本地验收纠错')
            page.wait_for_function("document.querySelector('#view h2').textContent.includes('待处理')")
            page.get_by_role('button',name='接受请求',exact=True).click()
            page.wait_for_selector('.deck-dialog-btn-confirm');page.locator('.deck-dialog-btn-confirm').last.click();page.wait_for_timeout(220)
            page.wait_for_function("document.querySelector('#view h2').textContent.includes('已接受')")
            assert '将安排下载，请耐心等待下载完成并入库' in page.locator('#view').inner_text()
            assert service.get(rid)['status']=='accepted'
            checks.append('纠错后接受即终结，通知措辞不声称可看')
            page.screenshot(path=str(artifacts/'requests-accepted-detail.png'),full_page=True)
            page.get_by_role('button',name='人工退回额度',exact=True).click();prompt('本地验收额度修正')
            page.wait_for_function("document.querySelector('#view').textContent.includes('额度已退回')")
            assert service.used('browser-u')==12
            assert page.get_by_role('button',name='额度已退回',exact=True).is_disabled()
            checks.append('人工退回一次额度，禁用重复退回')
            page.get_by_role('button',name='重试失败通知',exact=True).click();page.wait_for_timeout(200)
            assert service.get(rid)['status']=='accepted' and service.used('browser-u')==12
            checks.append('通知重试无额外业务变更')
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(artifacts/'requests-mobile-detail.png'),full_page=True)
            assert page.locator('#view h2').is_visible()
            checks.append('移动端详情可达')
            assert not any('/claim' in p or '/resolve' in p for p,_ in posts)
            assert not errors,errors
            (artifacts/'requests-browser-report.json').write_text(json.dumps({'ok':True,'checks':checks,'posts':posts,'console_errors':errors},ensure_ascii=False,indent=2))
            browser.close()
