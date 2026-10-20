"""Credential-free member Web and admin migration end-to-end in isolated Chromium."""
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from test_economy import proof

from app.main import app
from app.modules.activity_web import exchange, issue_code, resolve
from app.modules.levels import DAY


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('MEDIADECK_CHECKIN_SECRET', 'test-only-secret-material-for-activity-checkin')
    with TestClient(app) as client:
        client.auth = ('admin', 'change-me')
        cfg = app.state.levels.config()
        cfg['config']['enabled'] = True
        assert client.put('/api/activity/config', json=cfg).status_code == 200
        app.state.members.upsert('u', 'Demo Viewer', {'group_id': 'standard', 'expires_at': int(time.time())+DAY})
        app.state.members.upsert('other', 'Other Viewer', {'group_id': 'standard'})
        app.state.db.execute("UPDATE members SET expires_at=?,tg_user_id='12345' WHERE emby_user_id='u'", (int(time.time())+DAY,))
        app.state.points.add('u', 100, 'admin.adjust')
        app.state.plugins.save('checkin', enabled=True, config={'dual_checkin': True})
        noon = (int(time.time())+8*3600)//DAY*DAY-8*3600+12*3600
        # Tests may run near midnight; independent proofs are in prior days and
        # today's proof ends at a controlled noon for deterministic admission.
        monkeypatch.setattr('time.time', lambda: noon)
        for i in range(3):
            proof(app.state.db, 'u', now=noon-i*DAY, seconds=1600, key=str(i))
        yield client


def test_member_code_binding_single_use_and_cross_account_auth(client):
    code = issue_code(app.state.db, 'u', '12345')
    assert client.get('/api/activity/me').status_code == 401  # admin cookie cannot impersonate
    assert client.post('/api/activity/login', json={'code': code}).status_code == 200
    assert client.post('/api/activity/login', json={'code': code}).status_code == 409
    data = client.get('/api/activity/me').json()
    assert data['username'] == 'Demo Viewer' and data['qualification']['eligible']
    assert data['level']['level'] == 1
    assert data['today']['points'] == 0  # GET cannot settle evidence
    assert not app.state.db.query('SELECT * FROM activity_watch_days')
    assert client.post('/api/activity/renew', json={'request_id': 'web-one', 'user_id': 'other'}).status_code == 200
    assert app.state.points.balance('u') == 20 and app.state.points.balance('other') == 0
    assert client.post('/api/activity/renew', json={'request_id': 'web-one'}).json()['cost'] == 80
    app.state.db.execute("UPDATE members SET tg_user_id='54321' WHERE emby_user_id='u'")
    assert client.get('/api/activity/me').status_code == 401


def test_token_not_stored_plaintext_and_expiry_rejected(client, monkeypatch):
    code = issue_code(app.state.db, 'u', '12345')
    rows = app.state.db.query('SELECT * FROM activity_web_tokens')
    assert code not in json.dumps(rows)
    token = exchange(app.state.db, code)
    assert resolve(app.state.db, token) == 'u'
    now = time.time()
    monkeypatch.setattr('time.time', lambda: now+3601)
    assert resolve(app.state.db, token) is None


def test_member_browser_checkins_auto_and_renewal(client, tmp_path, monkeypatch):
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    monkeypatch.setenv('TMPDIR', str(tmp_path.parent))
    monkeypatch.setattr('app.modules.dual_checkin.draw', lambda *args: 130 if args[3].endswith(':luck') else 999999)
    code = issue_code(app.state.db, 'u', '12345')
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'], headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844}, timezone_id='America/Los_Angeles')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def bridge(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname != 'panel.test': return route.abort()
            if url.path.startswith(('/api/stream', '/api/live')):
                return route.fulfill(status=200, content_type='text/event-stream', body='')
            response = client.request(request.method, url.path+('?' + url.query if url.query else ''),
                                      content=request.post_data_buffer,
                                      headers={'Content-Type': request.headers.get('content-type', 'application/json')})
            route.fulfill(status=response.status_code, body=response.content,
                          headers={k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding','set-cookie')})
        page.route('**/*', bridge)
        page.goto('https://panel.test/activity')
        page.fill('#activity-code', code)
        page.get_by_role('button', name='登录', exact=True).click()
        page.locator('#activity-profile:not([hidden])').wait_for()
        assert 'Lv1 布衣' in page.locator('#activity-level').inner_text()
        assert '🌱' in page.locator('#activity-level').inner_text()
        assert '偷得浮生半日闲' in page.locator('#activity-title').inner_text()
        assert '北京时间' in page.locator('#activity-facts').inner_text()
        page.click('#activity-checkin')
        page.wait_for_function("document.querySelector('#activity-status').textContent.includes('最终奖励10')")
        page.click('#activity-luck')
        page.get_by_role('button', name='确认', exact=True).click()
        page.wait_for_function("document.querySelector('#activity-status').textContent.includes('最终奖励3')")
        assert app.state.points.balance('u') == 103
        page.check('#activity-auto')
        page.wait_for_function("document.querySelector('#activity-status').textContent.includes('偏好已保存')")
        assert app.state.db.one("SELECT auto_renew FROM activity_members WHERE emby_user_id='u'")['auto_renew'] == 1
        page.click('#activity-renew')
        page.get_by_role('button', name='确认', exact=True).click()
        page.wait_for_function("document.querySelector('#activity-status').textContent.includes('本地已续至')")
        assert app.state.points.balance('u') == 23
        output = Path(os.getenv('ACTIVITY_UI_ARTIFACTS', str(tmp_path)))
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output/'member-activity-mobile.png'), full_page=True)
        page.goto('https://panel.test/#/groups')
        page.wait_for_function("document.querySelector('#activity-level-admin') || document.querySelector('.error')", timeout=10000)
        assert page.locator('#activity-level-admin').count(), (errors, page.locator('body').inner_text())
        assert not page.is_checked('#level-delete-enabled')
        assert page.locator('#view > #activity-level-admin').count() == 1
        assert page.locator('#level-special-rights').is_visible()
        assert page.locator('.hg-whitelist-group').is_visible()
        assert not page.locator('#level-legacy-groups').evaluate('(e)=>e.open')
        assert not page.locator('#group-create').is_visible()
        assert len(set(page.locator('[data-level-key="icon"]').evaluate_all('(es)=>es.map(e=>e.value)'))) == 8
        page.locator('#level-legacy-groups > summary').click()
        assert '不会覆盖等级基础权益' in page.locator('#level-legacy-groups').inner_text()
        page.locator('#level-legacy-groups > summary').click()
        page.locator('[data-level-row="0"][data-level-key="title"]').fill('Demo title')
        page.click('#level-save')
        page.wait_for_function("document.querySelector('#level-feedback').textContent.includes('已保存')")
        assert app.state.levels.config()['config']['levels'][0]['title'] == 'Demo title'
        app.state.db.execute("DELETE FROM activity_members WHERE emby_user_id='other'")
        before = app.state.points.balance('other')
        page.click('#level-preview')
        page.locator('#level-migrate-confirm').wait_for()
        assert '旧采样' in page.locator('#level-preview-results').inner_text()
        assert '北京时间' in page.locator('#level-preview-results').inner_text()
        assert page.evaluate('activityBeijingStamp(0)') == '1970/1/1 08:00:00 北京时间'
        page.screenshot(path=str(output/'levels-migration-mobile.png'), full_page=True)
        page.click('#level-migrate-confirm')
        page.get_by_role('button', name='确认', exact=True).click()
        page.wait_for_function("document.querySelector('#level-feedback').textContent.includes('迁移完成')")
        assert app.state.points.balance('other') == before
        assert app.state.members.get('other')['activity_level']['level'] == 1
        page.set_viewport_size({'width':1440,'height':1000})
        page.screenshot(path=str(output/'levels-desktop.png'), full_page=True)
        assert not page.locator('#level-legacy-groups').evaluate('(e)=>e.open')
        assert not errors
        browser.close()
