"""Member Mini App: signed Telegram identity, Bot-equivalent money paths and the UI."""
import hashlib
import hmac
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.miniapp import InitDataError, _rank_label, verify_init_data

TOKEN = '123456:local-mini-app-token'
VIEWER_TG = 777000111
DAY = 86400


def sign(user, token=TOKEN, auth_date=None, extra=None):
    fields = {'auth_date': str(auth_date or int(time.time())), 'query_id': 'AAE',
              'user': json.dumps(user, separators=(',', ':')), **(extra or {})}
    check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def headers(user_id=VIEWER_TG, **kw):
    return {'X-Telegram-Init-Data': sign({'id': user_id, 'first_name': 'Viewer', 'username': 'viewer_a'}, **kw)}


SHOWS = {9001: {'title': '示例剧集', 'original_title': 'Demo Show', 'year': 2020, 'poster_path': '',
                    'overview': '一段示例简介。', 'seasons': [{'season_number': 0, 'episode_count': 3},
                                                      {'season_number': 1, 'episode_count': 2},
                                                      {'season_number': 2, 'episode_count': 4}]}}


class FakeTmdb:
    configured = True

    async def search(self, query, page=1, media_type='', year=None):
        return {'available': True, 'page': 1, 'total_pages': 1, 'results': [
            {'media_type': 'tv', 'tmdb_id': 9001, 'title': '示例剧集', 'year': 2020, 'poster_path': ''},
            {'media_type': 'movie', 'tmdb_id': 9002, 'title': '示例电影', 'year': 2021, 'poster_path': ''}]}

    async def lookup(self, media_type, tmdb_id, *, language=None):
        if media_type == 'tv':
            return dict(SHOWS.get(int(tmdb_id), {}))
        return {'title': '示例电影', 'year': 2021, 'poster_path': '', 'overview': ''}

    async def lookup_request(self, media_type, tmdb_id):
        return await self.lookup(media_type, tmdb_id)


@pytest.fixture
def client(monkeypatch):
    with TestClient(app) as client:
        client.auth = ('admin', 'change-me')
        cfg = app.state.levels.config()
        cfg['config']['enabled'] = True
        assert client.put('/api/activity/config', json=cfg).status_code == 200
        assert client.post('/api/settings/telegram', json={
            'bot_token': TOKEN, 'miniapp_url': 'https://app.example.test/app',
            'group_interaction_chats': ['-1001234567890']}).status_code == 200
        app.state.members.upsert('u1', 'emby-account-one', {'group_id': 'standard'})
        app.state.members.upsert('u2', 'emby-account-two', {'group_id': 'standard'})
        expiry = int(time.time()) + 2 * DAY
        app.state.db.execute("UPDATE members SET tg_user_id=?, tg_display_name='小V', expires_at=? "
                             "WHERE emby_user_id='u1'", (str(VIEWER_TG), expiry))
        app.state.db.execute("UPDATE members SET tg_user_id='777000222', tg_display_name='小B', inviter_id='u1' "
                             "WHERE emby_user_id='u2'")
        assert client.post('/api/points/u1/adjust', json={'delta': 3000, 'reason': '测试'}).status_code == 200
        app.state.db.execute("UPDATE shop_items SET enabled=1")
        fake = FakeTmdb()
        monkeypatch.setattr(app.state, 'tmdb', fake)
        monkeypatch.setattr(app.state.requests, '_tmdb', fake)

        async def lookup(media_type, tmdb_id, user_id=None, *, include_episodes=True):
            if media_type != 'tv' or int(tmdb_id) != 9001:
                return []
            episodes = [{'season': 1, 'episode': 1}, {'season': 1, 'episode': 2}] if include_episodes else []
            return [{'id': 'series-9001', 'name': '示例剧集', 'url': '', 'episodes': episodes}]
        monkeypatch.setattr(app.state.emby, 'request_lookup', lookup, raising=False)

        async def offline_sync(user=None):
            return None
        monkeypatch.setattr(app.state.titles, 'sync', offline_sync)
        yield client


def test_init_data_signature_rules():
    user = {'id': 5, 'first_name': 'A'}
    assert verify_init_data(sign(user), TOKEN)['id'] == 5
    # Newer clients add a third-party signature field; it is part of the hash input.
    assert verify_init_data(sign(user, extra={'signature': 'abc'}), TOKEN)['id'] == 5
    with pytest.raises(InitDataError):
        verify_init_data(sign(user, token='999:other'), TOKEN)
    with pytest.raises(InitDataError, match='expired'):
        verify_init_data(sign(user, auth_date=int(time.time()) - 2 * DAY), TOKEN)
    tampered = sign(user).replace('%22id%22%3A5', '%22id%22%3A6')
    with pytest.raises(InitDataError):
        verify_init_data(tampered, TOKEN)
    with pytest.raises(InitDataError):
        verify_init_data(sign(user) + '&auth_date=1', TOKEN)
    with pytest.raises(InitDataError):
        verify_init_data('', TOKEN)


def test_rank_label_never_uses_emby_account():
    row = {'user_id': 'u1', 'username': 'emby-account-one', 'tg_user_id': '1', 'tg_display_name': ''}
    assert _rank_label(row, {}) == 'Telegram用户'
    assert _rank_label(dict(row, tg_username='nick'), {}) == '@nick'
    assert _rank_label(row, {'1': {'display_name': '昵称'}}) == '昵称'
    assert _rank_label({'username': 'emby-account-one'}, {}) == '未绑定'


def test_identity_gates(client):
    assert client.get('/api/app/me', auth=None).status_code == 401
    assert client.get('/api/app/me', auth=None, headers=headers(token='1:wrong')).json()['detail']['code'] == 'unsigned'
    assert client.get('/api/app/me', auth=None, headers=headers(42)).json()['detail']['code'] == 'unbound'
    assert client.post('/api/settings/telegram', json={'miniapp_url': ''}).status_code == 200
    assert client.get('/api/app/me', auth=None, headers=headers()).status_code == 503
    assert client.post('/api/settings/telegram', json={'miniapp_url': 'http://insecure.test/app'}).status_code >= 400


def test_me_and_renewal_charge_once(client):
    me = client.get('/api/app/me', auth=None, headers=headers()).json()
    assert me['name'] == 'Viewer' and me['balance'] == 3000 and me['renewal']['window_open']
    preview = client.post('/api/app/renew/preview', auth=None, headers=headers()).json()
    assert preview['blockers'] == [] and preview['nonce']
    first = client.post('/api/app/renew/confirm', auth=None, headers=headers(), json={'nonce': preview['nonce']}).json()
    again = client.post('/api/app/renew/confirm', auth=None, headers=headers(), json={'nonce': preview['nonce']}).json()
    assert first['ok'] and first['expires_at'] == again['expires_at']
    assert app.state.points.balance('u1') == 3000 - preview['price']
    # Another Telegram account cannot spend this intent even with a valid signature.
    app.state.db.execute("UPDATE members SET tg_user_id='777000333' WHERE emby_user_id='u2'")
    stolen = client.post('/api/app/renew/confirm', auth=None, headers=headers(777000333), json={'nonce': preview['nonce']})
    assert stolen.status_code == 409


def test_auto_renew_requires_current_binding(client):
    me = client.get('/api/app/me', auth=None, headers=headers()).json()
    stale = client.post('/api/app/auto-renew', auth=None, headers=headers(), json={'enabled': True, 'binding': -1})
    assert stale.status_code == 409
    ok = client.post('/api/app/auto-renew', auth=None, headers=headers(), json={'enabled': True, 'binding': me['binding']})
    assert ok.json()['auto_renew'] is True


def test_shop_redeem_is_idempotent(client):
    shop = client.get('/api/app/shop', auth=None, headers=headers()).json()
    item = next(i for i in shop['items'] if i['price'] <= 3000)
    body = {'request_id': 'r' * 32, 'expected_spec': item['spec']}
    first = client.post(f"/api/app/shop/{item['id']}/redeem", auth=None, headers=headers(), json=body).json()
    second = client.post(f"/api/app/shop/{item['id']}/redeem", auth=None, headers=headers(), json=body).json()
    assert first['balance'] == second['balance'] == 3000 - item['price']
    assert app.state.points.balance('u1') == 3000 - item['price']
    changed = dict(item['spec'], cost=item['price'] + 1)
    refused = client.post(f"/api/app/shop/{item['id']}/redeem", auth=None, headers=headers(),
                          json={'request_id': 's' * 32, 'expected_spec': changed})
    assert refused.status_code == 409


def test_requests_follow_and_quota(client):
    title = client.get('/api/app/requests/title?media_type=tv&tmdb_id=9001', auth=None, headers=headers()).json()
    assert [(s['season'], s['state']) for s in title['seasons']] == [(1, 'full'), (2, 'missing')]
    search = client.get('/api/app/requests/search?q=示例', auth=None, headers=headers()).json()
    assert search['results'][0]['in_library'] and not search['results'][1]['in_library']
    body = {'media_type': 'tv', 'tmdb_id': 9001, 'seasons': [2], 'create_key': 'k' * 32}
    created = client.post('/api/app/requests', auth=None, headers=headers(), json=body).json()
    replay = client.post('/api/app/requests', auth=None, headers=headers(), json=body).json()
    assert created['created'] and created['id'] == replay['id'] and replay['remaining'] == created['remaining']
    check = client.post('/api/app/requests/check', auth=None, headers=headers(777000222),
                        json={'media_type': 'tv', 'tmdb_id': 9001, 'seasons': [2]}).json()
    assert check['same']['status'] == 'open' and not check['same']['mine']
    followed = client.post('/api/app/requests', auth=None, headers=headers(777000222),
                           json=dict(body, create_key='f' * 32)).json()
    assert followed['followed'] and not followed['created']
    mine = client.get('/api/app/requests/mine', auth=None, headers=headers(777000222)).json()
    assert mine['rows'][0]['followed'] and mine['remaining'] == mine['quota']


def test_invites_show_two_levels_without_emby_names(client):
    data = client.get('/api/app/invites', auth=None, headers=headers()).json()
    assert data['direct'] == 1 and data['nodes'][0]['label'] == '小B'
    assert 'emby-account' not in json.dumps(data)


def test_bot_home_offers_mini_app_only_in_private():
    from app.modules.telegram import _GROUP
    with TestClient(app):
        app.state.settings_service.save_telegram({'bot_token': TOKEN, 'miniapp_url': 'https://app.example.test/app'})
        app.state.members.upsert('u9', 'emby-nine', {'group_id': 'standard'})
        app.state.db.execute("UPDATE members SET tg_user_id='555' WHERE emby_user_id='u9'")
        _, keyboard = app.state.telegram._home('555', 'Nine')
        assert keyboard[0] == [{'text': '📱 会员面板', 'web_app': {'url': 'https://app.example.test/app'}}]
        token = _GROUP.set(True)
        try:
            _, keyboard = app.state.telegram._home('555', 'Nine')
        finally:
            _GROUP.reset(token)
        assert all('web_app' not in b for row in keyboard for b in row)


TELEGRAM_STUB = """
window.__opened = [];
window.Telegram = {WebApp: {initData: %s, colorScheme: 'dark', ready(){}, expand(){}, disableVerticalSwipes(){},
  onEvent(){}, setHeaderColor(){}, setBackgroundColor(){}, close(){ window.__closed = true; },
  openLink(u){ window.__opened.push(u); }, openTelegramLink(u){ window.__opened.push(u); },
  BackButton: {visible: false, show(){ this.visible = true; }, hide(){ this.visible = false; }, onClick(fn){ window.__back = fn; }},
  HapticFeedback: {notificationOccurred(){}, impactOccurred(){}}}};
"""


def test_mini_app_browser_flows(client, tmp_path):
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    init = headers()['X-Telegram-Init-Data']
    output = Path(os.getenv('MINIAPP_UI_ARTIFACTS', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium') or '/usr/bin/chromium',
                                         args=['--no-sandbox'], headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=2)
        page.set_default_timeout(8000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def bridge(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname == 'telegram.org':
                return route.fulfill(content_type='application/javascript', body=TELEGRAM_STUB % json.dumps(init))
            if url.hostname != 'app.example.test':
                return route.abort()
            response = client.request(request.method, url.path + ('?' + url.query if url.query else ''),
                                      content=request.post_data_buffer, auth=None,
                                      headers={k: v for k, v in request.headers.items()
                                               if k.lower() in ('content-type', 'x-telegram-init-data')})
            route.fulfill(status=response.status_code, body=response.content,
                          headers={k: v for k, v in response.headers.items()
                                   if k not in ('content-length', 'content-encoding')})
        page.route('**/*', bridge)

        def fits(name):
            # Nothing on a phone-width screen may spill sideways or under the tab bar.
            spill = page.evaluate("""() => {
              const w = document.documentElement.clientWidth, out = [];
              for (const el of document.querySelectorAll('#app *, #layer *')) {
                const r = el.getBoundingClientRect();
                if (r.width && (r.right > w + 1 || r.left < -1) && !el.closest('.backdrop')) out.push(el.className || el.tagName);
              }
              const s = document.querySelector('#screen');
              return {out: out.slice(0, 5), scroll: s.scrollWidth - s.clientWidth};
            }""")
            assert spill['scroll'] <= 0 and not spill['out'], (name, spill)
            page.screenshot(path=str(output / name))

        page.goto('https://app.example.test/app')
        page.locator('.hero .plaque').wait_for()
        assert 'Viewer' in page.locator('.hero').inner_text()
        assert 'emby-account' not in page.locator('body').inner_text()
        fits('miniapp-me.png')

        page.locator('#renew').click()
        sheet = page.locator('.sheet')
        assert '续期后' in sheet.inner_text()
        fits('miniapp-renew.png')
        sheet.get_by_role('button', name='确认续期').click()
        page.locator('#toast', has_text='续期成功').wait_for()
        assert app.state.points.balance('u1') < 3000

        page.locator('[data-go="level"]').click()
        page.get_by_text('升级之路').wait_for()
        assert page.evaluate('Telegram.WebApp.BackButton.visible')
        fits('miniapp-level.png')
        page.evaluate('window.__back()')
        page.locator('#renew').wait_for()

        page.locator('[data-tab="bag"]').click()
        page.locator('[data-buy]').first.wait_for()
        fits('miniapp-shop.png')
        title_card = page.locator('.list .it', has_text='称号').locator('[data-buy]')
        title_card.click()
        page.locator('.sheet').get_by_role('button', name='确认兑换').click()
        page.locator('#toast', has_text='兑换成功').wait_for()
        page.locator('[data-seg="items"]').click()
        page.locator('[data-use]').first.click()
        page.locator('#sheet-input').fill('观影达人')
        page.locator('.sheet').get_by_role('button', name='创建称号').click()
        page.locator('#toast', has_text='称号已创建').wait_for()
        page.locator('[data-seg="titles"]').click()
        page.locator('[data-wear]:not([data-wear=""])').first.click()
        page.locator('.hero .plaque.big', has_text='观影达人').wait_for()
        fits('miniapp-titles.png')
        page.locator('[data-seg="invites"]').click()
        page.get_by_text('我邀请的人').wait_for()
        assert '小B' in page.locator('#bag-body').inner_text()
        fits('miniapp-invites.png')

        page.locator('[data-tab="req"]').click()
        page.locator('#req-q').fill('示例')
        page.locator('#req-form button').click()
        page.locator('[data-pick]').first.wait_for()
        assert '已入库' in page.locator('.pgrid').inner_text()
        fits('miniapp-search.png')
        page.locator('[data-pick]').first.click()
        page.locator('#seasons [data-s="2"].on').wait_for()
        assert 'have' in page.locator('#seasons [data-s="1"]').get_attribute('class')
        page.locator('#submit:not([disabled])').wait_for()
        fits('miniapp-request.png')
        page.locator('#submit').click()
        page.locator('.sheet').get_by_role('button', name='提交求片').click()
        page.locator('#toast', has_text='已提交').wait_for()
        page.get_by_text('我的求片').wait_for()
        assert '待处理' in page.locator('#req-body').inner_text()

        app.state.plugins.save('points_ranking', True)
        for index, (uid, name) in enumerate([('u3', '小C'), ('u4', '@d_user'), ('u5', '小E')], 3):
            app.state.members.upsert(uid, f'emby-account-{index}', {'group_id': 'standard'})
            app.state.db.execute("UPDATE members SET tg_user_id=?, tg_display_name=? WHERE emby_user_id=?",
                                 (str(777000000 + index), '' if name.startswith('@') else name, uid))
            if name.startswith('@'):
                app.state.db.execute("UPDATE members SET tg_username=? WHERE emby_user_id=?", (name[1:], uid))
            app.state.points.add(uid, 4000 - index * 300, '测试')
        page.locator('[data-tab="me"]').click()
        page.locator('#renew').wait_for()
        page.locator('[data-tab="rank"]').click()
        page.locator('#rank-body .empty, #rank-body .podium').first.wait_for()
        page.locator('[data-kind="points"]').click()
        page.locator('#rank-body .podium').wait_for()
        board = page.locator('#rank-body').inner_text()
        assert '@d_user' in board and '我的排名' in board and 'emby-account' not in board
        fits('miniapp-rank.png')

        page.evaluate("document.documentElement.dataset.theme='light'")
        page.locator('[data-tab="me"]').click()
        page.locator('.hero .plaque').wait_for()
        fits('miniapp-me-light.png')
        assert not errors, errors
        browser.close()
