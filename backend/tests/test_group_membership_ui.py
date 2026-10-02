"""Formal HardGlass assets in Chromium, actual temporary API behind route bridge."""
import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright
from test_group_membership_api import AUTH
from test_group_membership_api import api as base_api  # noqa: F401


@pytest.fixture
def api(request):
    return request.getfixturevalue('base_api')


def test_formal_settings_dirty_permissions_save_and_scan_results(api, tmp_path):
    client, bot, state = api
    static = Path(__file__).parents[1] / 'app/static'
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(static)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    artifacts = Path(os.environ.get('GROUP_MEMBERSHIP_ARTIFACTS', str(tmp_path)))
    artifacts.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            page.set_default_timeout(10000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            def bridge(route):
                url = urlsplit(route.request.url)
                if not route.request.url.startswith(base):
                    return route.abort()
                if url.path.startswith(('/api/live', '/api/stream')):
                    return route.fulfill(status=200, content_type='text/event-stream', body='')
                if url.path.startswith(('/api/', '/static/')) or url.path == '/healthz':
                    response = client.request(route.request.method, url.path + ('?' + url.query if url.query else ''),
                        auth=AUTH, content=route.request.post_data_buffer,
                        headers={'content-type': 'application/json'})
                    return route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'application/json'), body=response.content)
                return route.continue_()
            page.route('**/*', bridge)
            page.goto(base + '/#/tgbot?section=membership', wait_until='domcontentloaded')
            page.wait_for_function('state.pageReady')
            expect(page.locator('#gm-delete')).to_be_disabled()
            expect(page.locator('#gm-schedule-on')).not_to_be_checked()
            page.locator('#gm-add').click()
            page.locator('.gm-id').fill('@some_channel')
            page.locator('.gm-url').fill('https://t.me/local_channel')
            assert page.evaluate('configDirty()')
            state.permission = 'member'
            page.locator('#gm-verify').click()
            expect(page.locator('.card').filter(has=page.locator('#gm-save')).locator('.save-feedback')).to_contain_text('权限不足')
            expect(page.locator('#gm-delete')).to_be_disabled()
            state.permission = 'administrator'
            page.locator('#gm-verify').click()
            expect(page.locator('.gm-title')).to_have_text('Actual <channel>')
            expect(page.locator('#gm-delete')).to_be_enabled()
            page.locator('#gm-gate').check()
            page.locator('#gm-delete').check()
            page.locator('#gm-save').click()
            page.wait_for_function('!configDirty()')
            stored = client.get('/api/settings/telegram', auth=AUTH).json()
            assert stored['membership_rules']['delete_enabled'] and stored['membership_rules']['gate_enabled']
            assert stored['require_group'] == '@legacy_required'
            assert stored['group_interaction_chats'] == ['-100444']
            # Failed permission recheck retains the user's edit and dirty state.
            page.locator('.gm-url').fill('https://t.me/edited_link')
            state.permission = 'member'
            page.locator('#gm-save').click()
            expect(page.locator('.card').filter(has=page.locator('#gm-save')).locator('.save-feedback')).to_contain_text('保存失败')
            expect(page.locator('.gm-url')).to_have_value('https://t.me/edited_link')
            assert page.evaluate('configDirty()')
            page.evaluate("void go('tggroup')")
            page.locator('.deck-dialog-backdrop:not(.deck-dialog-closing) .deck-dialog-btn-cancel').click()
            assert page.evaluate("state.page === 'tgbot'")
            state.permission = 'administrator'
            page.locator('#gm-save').click()
            page.wait_for_function('!configDirty()')
            page.locator('#gm-mode').select_option('interval')
            page.locator('#gm-hours').fill('2')
            page.locator('#gm-schedule-on').check()
            page.locator('#gm-schedule-save').click()
            page.wait_for_function('!configDirty()')
            schedule = client.get('/api/plugins/group_membership', auth=AUTH).json()
            assert schedule['enabled'] and schedule['interval'] == 7200
            page.screenshot(path=str(artifacts / 'membership-settings-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(artifacts / 'membership-settings-mobile.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 1100})
            page.evaluate("go('tggroup')")
            expect(page.locator('#gm-scan-policy')).to_contain_text('删除开关已开启')
            state.membership = 'left'
            state.block_user = True
            page.locator('#gm-scan').click()
            page.locator('.deck-dialog-backdrop:not(.deck-dialog-closing) .deck-dialog-btn-confirm').click()
            expect(page.locator('#gm-scan')).to_be_disabled()
            expect(page.locator('#gm-scan-progress progress')).to_be_visible()
            page.screenshot(path=str(artifacts / 'membership-scan-progress.png'), full_page=True)
            state.block_user = False
            expect(page.locator('#gm-scan-state')).to_have_text('检测已结束')
            expect(page.locator('#gm-scan-results')).to_contain_text('已删除本人')
            assert state.deleted == ['group-test-user']
            expect(page.locator('#gm-scan-summary')).to_contain_text('已删除 1')
            expect(page.locator('#gm-scan-results')).to_contain_text('未加入/未关注：Actual <channel>')
            # Saved outcomes stay inspectable without repeating a destructive scan.
            bot.membership._latest['rows'].insert(0, {'username': 'KeptFirst', 'user_id': 'kept',
                'tg_user_id': '9100', 'state': 'present', 'action': 'detected', 'targets': []})
            bot.membership._latest['rows'].extend([
                {'username': 'FailedUser', 'user_id': 'failed', 'tg_user_id': '9101',
                 'state': 'absent', 'action': 'failed_retained', 'targets': []},
                {'username': 'CancelledUser', 'user_id': 'cancelled', 'tg_user_id': '9102',
                 'state': 'present', 'action': 'cancelled', 'targets': []},
                {'username': 'UnknownUser', 'user_id': 'unknown', 'tg_user_id': '9103',
                 'state': 'unknown', 'action': 'detected', 'targets': []},
                {'username': 'DetectedUser', 'user_id': 'detected', 'tg_user_id': '9104',
                 'state': 'absent', 'action': 'detected', 'targets': []}])
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function('state.pageReady')
            expect(page.locator('#gm-scan-results tbody tr').first).to_contain_text('GroupUser')
            expect(page.locator('#gm-scan-summary')).to_contain_text('失败保留 1')
            for value, name in [('deleted', 'GroupUser'), ('failed_retained', 'FailedUser'),
                                ('cancelled', 'CancelledUser'), ('unknown', 'UnknownUser'),
                                ('noncompliant', 'DetectedUser')]:
                page.locator('#gm-result-filter').select_option(value)
                expect(page.locator('#gm-scan-results tbody tr')).to_have_count(1)
                expect(page.locator('#gm-scan-results')).to_contain_text(name)
            page.locator('#gm-result-filter').select_option('all')
            page.locator('#gm-result-search').fill('9101')
            expect(page.locator('#gm-scan-results tbody tr')).to_have_count(1)
            expect(page.locator('#gm-scan-results')).to_contain_text('FailedUser')
            page.locator('#gm-result-search').fill('no-such-account')
            expect(page.locator('#gm-scan-results')).to_contain_text('没有符合当前筛选或搜索的结果')
            page.locator('#gm-result-search').fill('')
            page.locator('#gm-result-filter').select_option('deleted')
            assert state.deleted == ['group-test-user']
            page.screenshot(path=str(artifacts / 'membership-scan-results.png'), full_page=True)
            (artifacts / 'membership-browser-evidence.json').write_text(json.dumps({
                'errors': errors, 'formal_assets': True, 'temporary_api': True,
                'tested': ['permission rejection', 'HTML escaping', 'dirty retained on failed save', 'dirty navigation cancel',
                           'scoped settings save', 'schedule period', 'async progress', 'self deletion result'],
                'settings_text': page.locator('#view').inner_text()}, ensure_ascii=False, indent=2))
            assert not errors
            browser.close()
    finally:
        state.block_user = False
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
