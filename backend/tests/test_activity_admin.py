"""Admin-only activity UI and explicit absence of member Web routes."""
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch):
    with TestClient(app) as client:
        client.auth = ('admin', 'change-me')
        cfg = app.state.levels.config()
        cfg['config']['enabled'] = True
        assert client.put('/api/activity/config', json=cfg).status_code == 200
        app.state.members.upsert('other', 'Other Viewer', {'group_id':'standard'})
        app.state.db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(time.time()-86400),))
        yield client


@pytest.mark.parametrize('path,method', [('/activity','GET'),('/api/activity/login','POST'),
    ('/api/activity/me','GET'),('/api/activity/checkin','POST'),('/api/activity/renew','POST'),
    ('/api/activity/auto-renew','POST'),('/static/activity.html','GET'),('/static/activity_member.js','GET')])
def test_member_web_no_longer_exists(client, path, method):
    assert client.request(method, path).status_code == 404
    assert not app.state.db.one("SELECT name FROM sqlite_master WHERE name='activity_web_tokens'")


@pytest.mark.parametrize('path,method', [('/api/activity/config','GET'),('/api/activity/config','PUT'),
    ('/api/activity/migration','GET'),('/api/activity/migration','POST'),('/api/activity/deletion-preview','GET'),
    ('/api/activity/members/other/grant-lv8','POST'),('/api/activity/members/other/recover-deletion','POST')])
def test_admin_activity_requires_admin_auth(client, path, method):
    assert client.request(method, path, json={}, auth=None).status_code == 401


def test_admin_browser_config_and_migration(client, tmp_path, monkeypatch):
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    monkeypatch.setenv('TMPDIR', str(tmp_path.parent))
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
        output = Path(os.getenv('ACTIVITY_UI_ARTIFACTS', str(tmp_path)))
        output.mkdir(parents=True, exist_ok=True)
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
