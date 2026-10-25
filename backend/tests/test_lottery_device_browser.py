"""ux.js + the vendored ThumbmarkJS build in Chromium.

The page and both scripts are served by the real app's static mount through an
isolated TestClient; every browser request is recorded and anything else is
aborted. No real Telegram, server or third-party host is contacted.
"""
import base64
import json
import re
import secrets
import shutil
import time
from urllib.parse import urlsplit

import pytest

from app.modules import lottery_device
from app.modules.lottery_device import CONSENT_VERSION, FIELDS, LIMITS
from tests.lottery_device_helpers import device_ctx
from tests.test_lottery import BASE, GUEST, browser_id, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_lottery_device import OTHER, THIRD, legacy_create, strict
from tests.test_miniapp import headers

HOST = 'https://app.example.test'
PAGE = ('<!doctype html><html><head><meta charset="utf-8"><title>ux</title></head><body><p>consent page</p>'
        '<script src="/app/assets/vendor/thumbmark.umd.js"></script>'
        '<script src="/app/assets/ux.js"></script></body></html>')
ASSETS = {'/ux-check', '/app/assets/vendor/thumbmark.umd.js', '/app/assets/ux.js'}
LEGACY, KEY = 'mediadeck.lottery.browser.v1', 'md.ux.k'
# Counts every device/storage/network API a collector could touch; installed before any page script.
PROBE = """
(() => {
  const counts = window.__probe = {canvas: 0, audio: 0, storage: 0, net: 0, rtc: 0, log: 0};
  const wrap = (owner, name, key) => {
    const original = owner[name];
    if (typeof original !== 'function') return;
    owner[name] = function (...args) { counts[key] += 1; return original.apply(this, args); };
  };
  wrap(HTMLCanvasElement.prototype, 'getContext', 'canvas');
  for (const name of ['getItem', 'setItem', 'removeItem', 'clear', 'key']) wrap(Storage.prototype, name, 'storage');
  wrap(window, 'fetch', 'net');
  wrap(XMLHttpRequest.prototype, 'open', 'net');
  wrap(Navigator.prototype, 'sendBeacon', 'net');
  for (const name of ['log', 'info', 'warn', 'error', 'debug', 'trace', 'table']) wrap(console, name, 'log');
  for (const [name, key] of [['OfflineAudioContext', 'audio'], ['webkitOfflineAudioContext', 'audio'],
                             ['AudioContext', 'audio'], ['RTCPeerConnection', 'rtc'], ['WebSocket', 'net']]) {
    const Original = window[name];
    if (typeof Original !== 'function') continue;
    window[name] = new Proxy(Original, {construct(target, args) { counts[key] += 1; return new target(...args); }});
  }
  window.Telegram = {WebApp: {initData: '', platform: %s, version: %s, viewportHeight: innerHeight,
                              colorScheme: 'dark', ready() {}, expand() {}}};
})();
"""
SCREEN = {'width': 390, 'height': 844}


def decode(raw):
    assert re.fullmatch(r'[A-Za-z0-9_-]+', raw)  # base64url, no padding
    return json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8'))


def open_context(browser, env, requests, *, viewport, locale, platform, version):
    context = browser.new_context(viewport=viewport, screen=SCREEN, device_scale_factor=3, locale=locale,
                                  timezone_id='Asia/Shanghai', reduced_motion='reduce')
    context.add_init_script(PROBE % (json.dumps(platform), json.dumps(version)))
    context.on('request', lambda r: requests.append(r.url))

    def serve(route):
        url = urlsplit(route.request.url)
        if url.scheme + '://' + (url.hostname or '') != HOST or url.path not in ASSETS:
            return route.abort()
        if url.path == '/ux-check':
            return route.fulfill(content_type='text/html', body=PAGE)
        res = env.client.get(url.path)  # the real /app/assets static mount
        assert res.status_code == 200, url.path
        route.fulfill(status=200, body=res.content, content_type='application/javascript')
    context.route('**/*', serve)
    return context


def open_page(context, events):
    page = context.new_page()
    page.set_default_timeout(15000)
    # Chromium's own GPU driver diagnostics ("[.WebGL-...] GL Driver Message") are not page output.
    page.on('console', lambda m: None if m.text.startswith('[.WebGL-') else events.append(('console', m.text)))
    page.on('pageerror', lambda error: events.append(('error', str(error))))
    page.goto(HOST + '/ux-check')
    page.wait_for_load_state('load')
    return page


def collect(page):
    return page.evaluate('window.__mdx.collect()')


def assert_contract(data):
    assert set(data) <= {'v', 'k', 's'} and data['v'] == 1
    assert re.fullmatch(r'[0-9a-f]{64}', data['k'])
    s = data['s']
    assert set(s) <= set(FIELDS) and {'a', 'e', 'f', 'g', 'i', 'j', 'n'} <= set(s)
    # WebGL (b, c) is absent on GPU-less headless runners; when present it comes as a pair.
    assert {'b', 'c'} <= set(s) or not {'b', 'c'} & set(s)
    for key, value in s.items():
        assert isinstance(value, str) and len(value) <= LIMITS.get(key, 64), key
        assert re.fullmatch(FIELDS[key], value), (key, value)


def test_ux_collects_only_on_call_is_stable_and_offline(request):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    requests, events = [], []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        android = open_context(browser, env, requests, viewport={'width': 390, 'height': 844}, locale='zh-CN',
                               platform='android', version='7.10')
        page = open_page(android, events)
        page.wait_for_timeout(500)
        # Loading the library and ux.js reads nothing: no canvas/audio/storage/network use.
        assert page.evaluate('window.__probe') == {'canvas': 0, 'audio': 0, 'storage': 0, 'net': 0, 'rtc': 0, 'log': 0}
        assert page.evaluate('localStorage.length') == 0
        assert page.evaluate('Object.keys(window.__mdx)') == ['collect']
        assert page.evaluate('Object.isFrozen(window.__mdx) && typeof ThumbmarkJS.Thumbmark') == 'function'
        legacy = secrets.token_hex(32)
        page.evaluate('([key, value]) => localStorage.setItem(key, value)', [LEGACY, legacy])
        loaded = len(requests)
        first = collect(page)
        probe = page.evaluate('window.__probe')
        assert probe['canvas'] > 0 and probe['audio'] > 0  # the collection happens only now
        assert probe['net'] == 0 and probe['rtc'] == 0 and probe['log'] == 0
        data = decode(first)
        assert_contract(data)
        assert data['k'] == legacy  # migrated, then the old key is removed
        assert page.evaluate('k => localStorage.getItem(k)', LEGACY) is None
        assert page.evaluate('k => localStorage.getItem(k)', KEY) == legacy
        assert lottery_device.unpack(first)[0] == legacy and lottery_device.unpack(first)[1]
        probe = page.evaluate('window.__probe')  # after the test's own two getItem checks
        assert collect(page) == first == collect(page)  # cached for this page load
        assert page.evaluate('window.__probe') == probe  # cached: nothing is read again
        page.reload()
        assert collect(page) == first  # same browser context: same installation and device values
        # Telegram resizing its viewport does not change window.screen (CDP keeps the emulated device screen).
        android.new_cdp_session(page).send('Emulation.setDeviceMetricsOverride', {
            'width': 320, 'height': 480, 'deviceScaleFactor': 3, 'mobile': False,
            'screenWidth': SCREEN['width'], 'screenHeight': SCREEN['height']})
        page.reload()
        assert page.evaluate('[innerWidth, innerHeight, screen.width]') == [320, 480, 390]
        assert collect(page) == first
        # Another Telegram client on the same device: other storage, locale, platform/version and viewport.
        ios = open_context(browser, env, requests, viewport={'width': 360, 'height': 640}, locale='en-US',
                           platform='ios', version='8.0')
        other_page = open_page(ios, events)
        assert other_page.evaluate('[navigator.language, Telegram.WebApp.platform, innerWidth]') == ['en-US', 'ios', 360]
        other = decode(collect(other_page))
        assert_contract(other)
        assert other['s'] == data['s'] and other['k'] != data['k']
        agent = page.evaluate('navigator.userAgent')
        for payload in (data, other):
            values = list(payload['s'].values())
            for forbidden in ('zh-CN', 'en-US', agent, 'android', 'ios', '7.10', '8.0', '390x844x24x3x', '320', '360'):
                assert forbidden not in values
            assert all(agent not in value for value in values)
        assert data['s']['f'] == '844x390x24x3'
        # Every request was one of the three local files; collecting issued none.
        assert {urlsplit(u).scheme + '://' + urlsplit(u).hostname + urlsplit(u).path for u in requests} <= {
            HOST + path for path in ASSETS}
        assert requests[loaded:loaded + 3] != [] and all(urlsplit(u).path in ASSETS for u in requests[loaded:])
        assert len(requests) == 3 * 4  # four page loads, three files each
        assert events == []  # no console output, no page errors
        assert other_page.evaluate('window.__probe.log') == page.evaluate('window.__probe.log') == 0
        browser.close()


def test_ux_output_links_two_clients_on_one_device(request):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    lid = legacy_create(env, strict(prizes=[{'kind': 'points', 'name': '积分', 'quantity': 3, 'amount': 5}]))
    requests, events = [], []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        blobs = []
        for viewport, locale, platform in (({'width': 390, 'height': 844}, 'zh-CN', 'android'),
                                           ({'width': 360, 'height': 700}, 'en-US', 'tdesktop')):
            context = open_context(browser, env, requests, viewport=viewport, locale=locale,
                                   platform=platform, version='7.10')
            blobs.append(collect(open_page(context, events)))
        browser.close()
    for tg, ctx in ((GUEST, blobs[0]), (OTHER, blobs[1]), (THIRD, device_ctx(THIRD))):
        r = env.client.post(BASE + f'/{lid}/join', headers={**headers(tg), 'CF-Connecting-IP': env.ip_for(tg)},
                            json={'browser_id': browser_id(tg), 'consent_version': CONSENT_VERSION, 'ctx': ctx})
        assert r.status_code == 200, r.text
    records = req(env, 'GET', f'/{lid}/records').json()
    [cluster] = records['clusters']
    assert cluster['labels'] == ['设备环境特征相同']
    assert {a['tg_id'] for a in cluster['accounts']} == {str(GUEST), str(OTHER)}
    assert records['excluded'] == 2 and events == []


def test_lottery_device_consent_ui_full_flow(request):
    """Test full UI consent flows:
    - User declines consent -> no join request sent, bottom sheet closes.
    - User accepts consent -> collects device ctx and sends {consent_version, ctx}.
    - Combined fee + device consent bottom sheet -> single sheet, expected_entry_fee sent.
    - Admin form device_check switch toggle & records page with 相同设备.
    """
    env = request.getfixturevalue('lottery_env')
    from app.main import app
    from tests.test_lottery import ADMIN, MEMBER
    from tests.test_miniapp import TELEGRAM_STUB
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        viewer = {'id': ADMIN}
        join_payloads = []

        def bridge(route):
            r, u = route.request, urlsplit(route.request.url)
            if u.hostname == 'telegram.org':
                return route.fulfill(content_type='application/javascript',
                                     body=TELEGRAM_STUB % json.dumps(headers(viewer['id'])['X-Telegram-Init-Data']))
            if u.hostname != 'app.example.test':
                return route.abort()
            if r.method == 'POST' and u.path.endswith('/join'):
                join_payloads.append(json.loads(r.post_data))
            res = env.client.request(r.method, u.path + ('?' + u.query if u.query else ''),
                                     content=r.post_data_buffer, auth=None,
                                     headers={**{k: v for k, v in r.headers.items()
                                                 if k.lower() in ('content-type', 'x-telegram-init-data')},
                                              'CF-Connecting-IP': env.ip_for(viewer['id'])})
            route.fulfill(status=res.status_code, body=res.content,
                          headers={k: v for k, v in res.headers.items() if k not in ('content-length', 'content-encoding')})

        page = browser.new_page(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
        page.set_default_timeout(8000)
        page.route('**/*', bridge)

        # 1. Admin creates activity: verify no device_check switch in new draft, device_check is False
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').click()
        page.locator('#lot-new').click()
        page.locator('#lot-title').fill('新版严格防小号活动')
        from datetime import datetime

        from app.modules.economy_rules import BEIJING
        deadline = int(time.time()) + 7200
        page.locator('#lot-date').fill(datetime.fromtimestamp(deadline, BEIJING).strftime('%Y-%m-%dT%H:%M'))
        assert page.locator('#lot-anti-multi').is_checked()
        assert page.locator('#lot-device-check').count() == 0  # removed from new draft

        # Add prize and save
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('points')
        page.locator('#lot-quantity').fill('2')
        page.locator('#lot-prize-ok').click()
        page.locator('#lot-save').click()
        page.locator('#lot-preview').wait_for()

        lid = app.state.db.one('SELECT id FROM lotteries ORDER BY created_at DESC')['id']
        assert env.service.row(lid)['config']['device_check'] is False

        page.locator('#lot-preview').click()
        page.locator('#lot-publish').click()
        page.locator('#lot-cancel-act').wait_for()
        assert '设备校验' not in page.locator('.notice').first.inner_text()

        # 2. Member joins new activity: directly joins without consent sheet or collect
        viewer['id'] = MEMBER
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{lid}')
        page.locator('#lot-join').wait_for()
        page.locator('#lot-join').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()
        assert page.locator('#lot-sheet').count() == 0
        assert len(join_payloads) == 1
        assert join_payloads[0].get('consent_version') is None
        assert join_payloads[0].get('ctx') is None

        # 3. Legacy activity with device_check=True: verifies consent flow and disabled edit switch
        viewer['id'] = ADMIN
        legacy_lid = legacy_create(env, strict(entry_fee=50, prizes=[{'kind': 'points', 'name': '积分', 'quantity': 2, 'amount': 100}]))
        env.service.points.add('u2', 200, 'admin.mint', 'test-mint')

        # Check legacy draft/activity editor has disabled checkbox
        legacy_draft_lid = legacy_create(env, strict(), publish=False)
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{legacy_draft_lid}')
        page.locator('#lot-edit').click()
        page.locator('#lot-device-check').wait_for()
        assert page.locator('#lot-device-check').is_checked()
        assert page.locator('#lot-device-check').is_disabled()
        page.locator('#lot-title').fill('历史草稿重命名')
        page.locator('#lot-save').click()
        page.locator('#lot-preview').wait_for()
        assert env.service.row(legacy_draft_lid)['config']['device_check'] is True  # preserved

        # 4. Member joins legacy paid lottery: test combined consent + fee bottom sheet
        viewer['id'] = MEMBER
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{legacy_lid}')
        page.locator('#lot-join').wait_for()
        assert '支付 50 积分报名' in page.locator('#lot-join').inner_text()

        join_payloads.clear()
        page.locator('#lot-join').click()
        page.locator('#lot-sheet').wait_for()
        sheet_text = page.locator('#lot-sheet').inner_text()
        assert '设备校验' in sheet_text
        assert '50 积分' in sheet_text
        assert '同意并支付 50 积分报名' in page.locator('#lot-ok').inner_text()

        # Decline
        page.locator('#lot-cancel').click()
        page.locator('#lot-sheet').wait_for(state='detached')
        assert len(join_payloads) == 0

        # Accept
        page.locator('#lot-join').click()
        page.locator('#lot-sheet').wait_for()
        page.locator('#lot-ok').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()
        assert len(join_payloads) == 1
        paid_p = join_payloads[0]
        assert paid_p.get('consent_version') == CONSENT_VERSION
        assert paid_p.get('expected_entry_fee') == 50
        assert paid_p.get('ctx')

        # 5. Check admin records page display: label is 设备环境特征相同
        viewer['id'] = ADMIN
        other_tg = 999111222
        app.state.members.upsert('u_other', 'private-account-other', {'group_id': 'standard'})
        app.state.db.execute('UPDATE members SET tg_user_id=? WHERE emby_user_id=?', (str(other_tg), 'u_other'))
        env.service.points.add('u_other', 200, 'admin.mint', 'test-mint')
        r_other = env.client.post(BASE + f'/{legacy_lid}/join',
                                  headers={**headers(other_tg), 'CF-Connecting-IP': env.ip_for(other_tg)},
                                  json={'browser_id': browser_id(other_tg), 'consent_version': CONSENT_VERSION,
                                        'ctx': paid_p['ctx'], 'expected_entry_fee': 50})
        assert r_other.status_code == 200, r_other.text

        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{legacy_lid}')
        page.locator('#lot-to-manage').click()
        page.locator('#lot-records').click()
        page.locator('.lot-cluster').wait_for()
        records_text = page.locator('.lottery').inner_text()
        assert '设备环境特征相同' in records_text
        page.locator('details summary').click()
        notes = page.locator('.rule-note').all_inner_texts()
        assert any('设备环境特征相同' in t for t in notes)

        # 6. Copy legacy activity: verify newly created draft has device_check=False and no switch
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{legacy_lid}')
        page.locator('#lot-to-manage').click()
        page.locator('#lot-copy').click()
        page.locator('#lot-form').wait_for()
        assert page.locator('#lot-device-check').count() == 0  # no switch
        page.locator('#lot-title').fill('从历史活动复制的新活动')
        page.locator('#lot-date').fill(datetime.fromtimestamp(deadline + 3600, BEIJING).strftime('%Y-%m-%dT%H:%M'))
        page.locator('#lot-save').click()
        page.locator('#lot-preview').wait_for()
        copied_lid = app.state.db.one('SELECT id FROM lotteries ORDER BY created_at DESC')['id']
        assert env.service.row(copied_lid)['config']['device_check'] is False

        browser.close()
