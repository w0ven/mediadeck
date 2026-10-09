"""Real formal assets + temporary application API in desktop/mobile Chromium."""
import functools
import os
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from app.main import app
from app.modules.telegram import TelegramBot

AUTH = ('admin', 'change-me')


def test_actual_shop_custom_and_whitelist_editor_and_red_packets_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(TelegramBot, 'start', lambda self: None)
    static = Path(__file__).parents[1] / 'app/static'
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(static)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    artifacts = Path(os.environ.get('PACKETS_SHOP_UI_ARTIFACTS', str(tmp_path)))
    artifacts.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with TestClient(app) as client, sync_playwright() as p:
            assert client.get('/api/shop/items').status_code == 401
            for kind in ('traffic', 'days', 'bandwidth', 'invite'):
                assert client.post('/api/shop/items', auth=AUTH,
                    json={'kind': kind, 'name': 'rejected legacy', 'cost': 1, 'amount': 1}).status_code == 400
            browser = p.chromium.launch(executable_path=os.getenv('MEDIADECK_TEST_CHROMIUM',shutil.which('chromium') or '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            page.set_default_timeout(12000)
            page.on('pageerror', lambda e: errors.append(str(e)))
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
            page.goto(base + '/#/shop', wait_until='domcontentloaded')
            page.wait_for_function('state.pageReady')
            values = page.locator('#sh-kind option').evaluate_all('(opts)=>opts.map(o=>o.value)')
            assert set(values) == {'invite_card', 'bandwidth_card', 'streams_card', 'title_card', 'whitelist_card', 'custom'}
            expect(page.locator('#sh-notice')).to_be_hidden()
            page.locator('#sh-kind').select_option('whitelist_card')
            expect(page.locator('#sh-cost')).to_have_value('5000')
            expect(page.locator('#sh-duration')).to_have_value('0')
            expect(page.locator('#sh-amount')).to_be_disabled()
            page.screenshot(path=str(artifacts / 'shop-whitelist-desktop.png'), full_page=True)
            page.locator('#sh-kind').select_option('custom')
            expect(page.locator('#sh-notice')).to_be_visible()
            expect(page.locator('#sh-retention')).to_have_value('7')
            expect(page.locator('#sh-duration')).to_be_hidden()
            page.locator('#sh-name').fill('隔离UI验收商品')
            page.locator('#sh-desc').fill('公开介绍，不含付费内容')
            page.locator('#sh-cost').fill('75')
            page.locator('#sh-notice').fill('隔离付费说明 <实际快照>\n第二行')
            page.locator('#sh-enabled').check()
            with page.expect_response(lambda r: '/api/shop/items' in r.url and r.request.method == 'POST') as saved:
                page.locator('#sh-add').click()
            assert saved.value.status == 200
            item = saved.value.json()
            assert item['kind'] == 'custom' and item['retention_days'] == 7 and item['purchase_notice'].endswith('第二行')
            assert 'purchase_notice' not in app.state.shop.get(item['id'])
            page.wait_for_function('state.shopItems.some(i=>i.kind === "custom")')
            page.evaluate('(id)=>editShopItem(id)', item['id'])
            expect(page.locator('#se-notice')).to_have_value('隔离付费说明 <实际快照>\n第二行')
            page.locator('#se-retention').fill('9')
            page.locator('#se-notice').fill('修订仅影响后续订单')
            with page.expect_response(lambda r: r.url.endswith('/api/shop/items/' + str(item['id'])) and r.request.method == 'PUT') as edited:
                page.locator('#se-save').click()
            assert edited.value.status == 200 and edited.value.json()['retention_days'] == 9
            page.wait_for_function('!document.querySelector("#se-save")')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#sh-kind').select_option('custom')
            page.locator('#sh-notice').scroll_into_view_if_needed()
            page.screenshot(path=str(artifacts / 'shop-custom-mobile.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
            page.evaluate('go("automation")')
            page.get_by_role('button', name='积分', exact=True).click()
            page.locator('[data-plugin="red_packets"]').wait_for(state='visible')
            inv = page.locator('[data-plugin="inventory"]')
            expect(inv.locator('#pl-inventory-bandwidth_cap_mbps')).to_have_value('0')
            expect(inv).to_contain_text('0=不封顶')
            expect(inv).to_contain_text('不包含原基础带宽')
            for bonus_cap in ('100', '0'):
                inv.locator('#pl-inventory-bandwidth_cap_mbps').fill(bonus_cap)
                with page.expect_response(lambda r: r.url.endswith('/api/plugins/inventory') and r.request.method == 'POST') as bandwidth_saved:
                    inv.locator('[data-act="save"]').click()
                assert bandwidth_saved.value.status == 200
                configured_bonus = client.get('/api/plugins/inventory', auth=AUTH).json()['config']
                assert configured_bonus['bandwidth_cap_mbps'] == int(bonus_cap) and configured_bonus['streams_cap'] == 10
            inv.screenshot(path=str(artifacts / 'bonus-bandwidth-plugin-mobile.png'))
            assert client.post('/api/plugins/inventory', auth=AUTH, json={'config': {'bandwidth_cap_mbps': -1}}).status_code == 400
            card = page.locator('[data-plugin="red_packets"]')
            expect(card.locator('#pl-red_packets-max_total')).to_have_value('5000')
            expect(card.locator('#pl-red_packets-max_parts')).to_have_value('50')
            expect(card.locator('#pl-red_packets-ttl_hours')).to_have_count(0)
            expect(card).to_contain_text('无截止时间')
            expect(card.locator('#pl-red_packets-enabled')).not_to_be_checked()
            card.locator('#pl-red_packets-enabled').check()
            card.locator('#pl-red_packets-max_total').fill('800')
            card.locator('#pl-red_packets-max_parts').fill('20')
            with page.expect_response(lambda r: r.url.endswith('/api/plugins/red_packets') and r.request.method == 'POST') as configured:
                card.locator('[data-act="save"]').click()
            assert configured.value.status == 200
            stored = client.get('/api/plugins/red_packets', auth=AUTH).json()
            assert stored['enabled'] and stored['config']['max_total'] == 800 and stored['config']['max_parts'] == 20
            card.screenshot(path=str(artifacts / 'packets-plugin-mobile.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
            assert client.post('/api/plugins/red_packets', auth=AUTH, json={'config': {'max_parts': 0}}).status_code == 400
            assert client.post('/api/plugins/red_packets', auth=AUTH, json={'config': {'max_total': 1.5}}).status_code == 400
            assert client.post('/api/settings/telegram', auth=AUTH, json={'enabled': True, 'bot_token': '123:local-only', 'group_interaction_chats': ['-9000']}).status_code == 200
            page.evaluate('go("tgbot")')
            page.locator('[data-config-section="groups"]').click()
            expect(page.locator('#tg-shop-notice')).not_to_be_checked()
            for enabled in (True, False):
                page.locator('#tg-shop-notice').set_checked(enabled)
                with page.expect_response(lambda r: r.url.endswith('/api/settings/telegram') and r.request.method == 'POST') as broadcast_saved:
                    page.locator('#tg-save-groups').click()
                assert broadcast_saved.value.status == 200
                actual = client.get('/api/settings/telegram', auth=AUTH).json()
                assert actual['shop_purchase_broadcast'] is enabled and actual['group_interaction_chats'] == ['-9000']
            assert not app.state.db.query('SELECT * FROM shop_notices')
            browser.close()
            assert not errors, errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
