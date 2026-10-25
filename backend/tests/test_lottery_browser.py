"""Chromium uses the shipped Mini App, a local TestClient and mock Telegram.

No server/real Telegram/domain is contacted. Optional screenshots are opt-in.
"""
import base64
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.main import app
from tests.lottery_device_helpers import device_ctx
from tests.lottery_legacy_helpers import legacy_create
from tests.test_lottery import ADMIN, GUEST, MEMBER, config, create, draw, join
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import TELEGRAM_STUB, headers

LOADED = "[...document.querySelectorAll('.lottery img')].every(i => i.complete && i.naturalWidth > 0)"
PENDING = "imgs => imgs.filter(i => !(i.complete && i.naturalWidth > 0)).map(i => i.getAttribute('src'))"


def images_loaded(page):
    # Every asset request goes through the Python bridge; slow runners need the wait.
    timeout = pytest.importorskip('playwright.sync_api').TimeoutError
    try:
        page.wait_for_function(LOADED)
    except timeout:
        raise AssertionError(f"lottery images not loaded: {page.locator('.lottery img').evaluate_all(PENDING)}") from None


def test_lottery_real_ui_create_publish_join_claim(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    output = Path(os.getenv('LOTTERY_UI_ARTIFACTS', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        errors, requests = [], []

        def page_for(tid):
            page = browser.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=2,
                                    reduced_motion='reduce')
            page.set_default_timeout(8000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            def bridge(route):
                r = route.request
                u = urlsplit(r.url)
                if u.hostname == 'telegram.org':
                    return route.fulfill(content_type='application/javascript',
                                         body=TELEGRAM_STUB % json.dumps(headers(tid)['X-Telegram-Init-Data']))
                if u.hostname != 'app.example.test':
                    return route.abort()
                requests.append((tid, r.method, u.path))
                content = r.post_data_buffer
                if r.method == 'POST' and u.path.endswith('/join'):
                    # One headless Chromium plays every account; give each its own physical device,
                    # just as each gets its own network address below.
                    body = json.loads(content)
                    if body.get('ctx'):
                        k = json.loads(base64.urlsafe_b64decode(body['ctx'] + '=' * (-len(body['ctx']) % 4)))['k']
                        body['ctx'] = device_ctx(tid, k=k)
                        content = json.dumps(body).encode()
                response = env.client.request(r.method, u.path + ('?' + u.query if u.query else ''),
                                              content=content, auth=None,
                                              headers={**{k: v for k, v in r.headers.items()
                                                          if k.lower() in ('content-type', 'x-telegram-init-data')},
                                                       'CF-Connecting-IP': env.ip_for(tid)})
                route.fulfill(status=response.status_code, body=response.content,
                              headers={k: v for k, v in response.headers.items()
                                       if k not in ('content-length', 'content-encoding')})
            page.route('**/*', bridge)
            return page

        def fits(page, name):
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.evaluate("document.querySelector('#screen').scrollWidth <= document.querySelector('#screen').clientWidth")
            images_loaded(page)
            if os.getenv('LOTTERY_UI_ARTIFACTS'):
                page.wait_for_function("document.querySelector('#toast').hidden")
            page.screenshot(path=str(output / name), full_page=True)

        admin = page_for(ADMIN)
        lid = legacy_create(env, config(title='星光放送 · 真实活动', draw_mode='manual', deadline=None,
                                        prizes=[{'kind': 'points', 'name': '500 积分', 'quantity': 3, 'amount': 500}]),
                            publish=False)
        admin.goto('https://app.example.test/app?tgWebAppStartParam=lottery_' + lid)
        admin.locator('#lot-preview').click()
        fits(admin, '01-admin-publish-preview.png')
        admin.locator('#lot-publish').click()
        admin.locator('#lot-draw').wait_for()
        assert app.state.lottery.row(lid)['state'] == 'published'
        assert app.state.lottery.row(lid)['config']['device_check'] is False  # new drafts never enable device check
        fits(admin, '02-admin-published.png')

        guest = page_for(GUEST)
        guest.goto('https://app.example.test/app')
        guest.get_by_text('欢迎来到 Mini App', exact=True).wait_for()
        assert not [p for tid, m, p in requests if tid == GUEST and p == '/api/app/me']
        assert not guest.locator('#tabbar').is_visible()
        guest.goto('https://app.example.test/app?tgWebAppStartParam=lottery_' + lid)
        guest.locator('#lot-join').click()
        guest.locator('.ticket strong', has_text='报名已记录').wait_for()
        assert guest.locator('#lot-join').is_disabled()
        assert not guest.locator('#tabbar').is_visible()
        assert not guest.locator('#lot-new,#lot-list,#lot-draw,[data-go="lottery"]').count()
        fits(guest, '03-guest-joined.png')
        member = page_for(MEMBER)
        member.goto('https://app.example.test/app')
        member.locator('.hero .plaque').wait_for()
        assert not member.locator('[data-go="lottery"]').count()
        member.goto('https://app.example.test/app?tgWebAppStartParam=lottery_' + lid)
        member.locator('#lot-join').click()
        member.locator('.ticket strong', has_text='报名已记录').wait_for()
        assert app.state.db.one('SELECT COUNT(*) AS n FROM lottery_device_entries WHERE lottery_id=?', (lid,))['n'] == 0
        admin.locator('#lot-refresh').click()
        admin.locator('#lot-draw').click()
        admin.locator('#lot-ok').click()
        admin.locator('#lot-retry-awards').wait_for()
        fits(admin, '04-admin-settled.png')
        guest.locator('#lot-refresh').click()
        guest.locator('#lot-claim').wait_for()
        assert '待绑定领取' in guest.locator('.lottery').inner_text()
        app.state.members.upsert('u3', 'private-new-account', {'group_id': 'standard'})
        app.state.db.execute("UPDATE members SET tg_user_id=? WHERE emby_user_id='u3'", (str(GUEST),))
        guest.locator('#lot-claim').click()
        guest.get_by_text('已发放', exact=True).wait_for()
        assert app.state.points.balance('u3') == 500
        fits(guest, '05-winner-claimed.png')
        assert not errors
        browser.close()


def test_lottery_ui_all_prize_types_and_private_registration(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 360, 'height': 800}, reduced_motion='reduce')
        page.set_default_timeout(8000)
        viewer = {'id': ADMIN}

        def bridge(route):
            r, u = route.request, urlsplit(route.request.url)
            if u.hostname == 'telegram.org':
                return route.fulfill(content_type='application/javascript',
                                     body=TELEGRAM_STUB % json.dumps(headers(viewer['id'])['X-Telegram-Init-Data']))
            if u.hostname != 'app.example.test':
                return route.abort()
            res = env.client.request(r.method, u.path, content=r.post_data_buffer, auth=None,
                                     headers={**{k: v for k, v in r.headers.items()
                                                 if k.lower() in ('content-type', 'x-telegram-init-data')},
                                              'CF-Connecting-IP': env.ip_for(viewer['id'])})
            route.fulfill(status=res.status_code, body=res.content, headers={k: v for k, v in res.headers.items()
                                                                           if k not in ('content-length', 'content-encoding')})
        page.route('**/*', bridge)
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').click()
        page.locator('#lot-new').click()
        for kind in ('shop', 'invite', 'registration', 'manual'):
            page.locator('#lot-add').click()
            page.locator('#lot-kind').select_option(kind)
            if kind == 'manual':
                page.locator('#lot-prize-name').fill('神秘礼物')
                page.locator('#lot-instructions').fill('仅中奖者可见的领取说明')
            page.locator('#lot-prize-ok').click()
        assert page.locator('.prize-row').count() == 4
        assert page.locator('#lot-audience').input_value() == 'all'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        lid = create(env, config(prizes=[{'kind': 'registration', 'name': '本人专属注册资格',
                                         'quantity': 1, 'group_id': 'standard', 'days': 30}]))
        join(env, lid)
        draw(env, lid)
        viewer['id'] = GUEST
        page.goto('https://app.example.test/app?tgWebAppStartParam=lottery_' + lid)
        page.locator('#lot-register').wait_for()
        assert '不可转赠' in page.locator('.lottery').inner_text()
        output = Path(os.getenv('LOTTERY_UI_ARTIFACTS', str(tmp_path)))
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / '06-registration-winner.png'), full_page=True)
        browser.close()


def test_lottery_ui_silent_association_admin_records(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    lid = legacy_create(env, config(draw_mode='manual', prizes=[{'kind': 'points', 'name': '星光积分', 'quantity': 1, 'amount': 5}]))
    viewer = {'id': GUEST}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
        page.set_default_timeout(8000)
        errors, tokens = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def bridge(route):
            r, u = route.request, urlsplit(route.request.url)
            if u.hostname == 'telegram.org':
                return route.fulfill(content_type='application/javascript',
                                     body=TELEGRAM_STUB % json.dumps(headers(viewer['id'])['X-Telegram-Init-Data']))
            if u.hostname != 'app.example.test':
                return route.abort()
            if u.path.endswith('/join'):
                tokens.append(json.loads(r.post_data)['browser_id'])
            res = env.client.request(r.method, u.path + ('?' + u.query if u.query else ''),
                                     content=r.post_data_buffer, auth=None,
                                     headers={**{k: v for k, v in r.headers.items()
                                                 if k.lower() in ('content-type', 'x-telegram-init-data')},
                                              'CF-Connecting-IP': env.ip_for(viewer['id'])})
            route.fulfill(status=res.status_code, body=res.content,
                          headers={k: v for k, v in res.headers.items() if k not in ('content-length', 'content-encoding')})
        page.route('**/*', bridge)
        url = 'https://app.example.test/app?tgWebAppStartParam=lottery_' + lid
        for tg in (GUEST, MEMBER):
            viewer['id'] = tg
            page.goto(url)
            page.locator('#lot-join').click()
            page.locator('.ticket strong', has_text='报名已记录').wait_for()
            assert page.locator('#lot-join').inner_text() == '已报名 · 等待开奖'
            assert not any(word in page.locator('.lottery').inner_text() for word in ('异常', '排除', '资格风险', '关联簇'))
            assert not page.locator('#lot-records').count()
        assert len(tokens) == 2 and tokens[0] == tokens[1] and len(tokens[0]) == 64
        assert env.service.records(lid)['excluded'] == 2
        join(env, lid, 888000555)  # A separate installation/IP remains eligible.
        output = Path(os.getenv('LOTTERY_UI_ARTIFACTS', str(tmp_path)))
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / '07-silent-joined.png'), full_page=True)
        viewer['id'] = ADMIN
        page.goto(url)
        page.locator('#lot-to-manage').click()
        page.locator('#lot-records').click()
        page.locator('.lot-cluster').wait_for()
        assert '相同浏览器安装标识' in page.locator('.lot-cluster').inner_text()
        assert str(GUEST) in page.locator('.lot-cluster').inner_text()
        assert str(MEMBER) in page.locator('.lot-cluster').inner_text()
        page.locator('details summary').click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        images_loaded(page)
        page.screenshot(path=str(output / '08-admin-association-records.png'), full_page=True)
        page.locator('#lot-record-back').click()
        page.locator('#lot-draw').click()
        page.locator('#lot-ok').click()
        page.locator('#lot-retry-awards').wait_for()
        viewer['id'] = GUEST
        page.goto(url)
        page.get_by_text('这次与好运擦肩而过', exact=True).wait_for()
        assert not any(word in page.locator('.lottery').inner_text() for word in ('异常', '排除', '资格风险', '关联簇'))
        assert not page.locator('#lot-claim,#lot-register,#lot-records').count()
        page.screenshot(path=str(output / '09-silent-result.png'), full_page=True)
        assert not errors
        browser.close()


def test_lottery_v3_operations_and_interactions(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    # Enable levels for testing min_level setting
    levels = app.state.levels
    cfg = levels.config()
    cfg['config']['enabled'] = True
    levels.save_config(cfg['config'], cfg['revision'])

    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        viewer = {'id': ADMIN}

        def bridge(route):
            r, u = route.request, urlsplit(route.request.url)
            if u.hostname == 'telegram.org':
                return route.fulfill(content_type='application/javascript',
                                     body=TELEGRAM_STUB % json.dumps(headers(viewer['id'])['X-Telegram-Init-Data']))
            if u.hostname != 'app.example.test':
                return route.abort()
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

        # 1. Test create form fields (thresholds + reminder + pin)
        viewer['id'] = ADMIN
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').click()
        page.locator('#lot-new').click()
        page.locator('#lot-title').fill('门槛与开关测试')
        page.locator('#lot-audience').select_option('member')
        from datetime import datetime

        from app.modules.economy_rules import BEIJING
        deadline = config()['deadline']
        page.locator('#lot-date').fill(datetime.fromtimestamp(deadline, BEIJING).strftime('%Y-%m-%dT%H:%M'))
        page.locator('#lot-min-level').select_option('3')
        page.locator('#lot-min-points').fill('500')
        page.locator('#lot-min-watch').fill('10')
        assert page.locator('#lot-reminder').is_checked()
        assert page.locator('#lot-pin').is_checked()
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('points')
        page.locator('#lot-quantity').fill('2')
        page.locator('#lot-prize-ok').click()
        page.locator('#lot-save').click()
        page.locator('#lot-preview').wait_for()

        lid = app.state.db.one('SELECT id FROM lotteries ORDER BY created_at DESC')['id']
        row = env.service.row(lid)
        assert row['config']['min_level'] == 3
        assert row['config']['min_points'] == 500
        assert row['config']['min_watch_hours'] == 10
        assert row['config']['reminder'] is True
        assert row['config']['pin'] is True

        # Publish first lottery, so it has #lot-list in published state
        page.locator('#lot-preview').click()
        page.locator('#lot-publish').click()
        page.locator('#lot-cancel-act').wait_for()

        # 2. Test copy activity (from published activity)
        page.locator('#lot-copy').click()
        page.locator('#lot-copied-banner').wait_for()
        assert '请重新设置截止时间' in page.locator('#lot-copied-banner').inner_text()
        copied_lid = app.state.db.one('SELECT id FROM lotteries ORDER BY rowid DESC')['id']
        assert copied_lid != lid

        # 3. Test delete draft via bottom sheet
        page.locator('#lot-cancel').click()  # cancel editor -> back to published lottery detail
        page.locator('#lot-list').click()
        page.locator(f'[data-lot-open="{copied_lid}"]').click()
        page.locator('#lot-delete').click()
        page.locator('#lot-sheet').wait_for()
        assert page.locator('#lot-ok').is_visible()
        page.locator('#lot-ok').click()
        page.locator('#lot-sheet').wait_for(state='detached')
        assert not env.service.db.one('SELECT 1 FROM lotteries WHERE id=?', (copied_lid,))

        # 4. Member checks threshold pills and admin cancels activity
        # Join as member
        viewer['id'] = MEMBER
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{lid}')
        page.locator('#lot-join').wait_for()
        # Verify threshold pills
        assert 'Lv3 及以上' in page.locator('.lot-threshold-pills').inner_text()
        assert '积分余额 ≥ 500' in page.locator('.lot-threshold-pills').inner_text()
        assert '近 30 天观看 ≥ 10 小时' in page.locator('.lot-threshold-pills').inner_text()

        # Admin cancels activity via bottom sheet with {confirm: true}
        viewer['id'] = ADMIN
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{lid}')
        page.locator('#lot-to-manage').click()
        page.locator('#lot-cancel-act').click()
        page.locator('#lot-sheet').wait_for()
        assert '取消后不再报名和开奖' in page.locator('#lot-sheet').inner_text()
        page.locator('#lot-ok').click()
        page.locator('#lot-sheet').wait_for(state='detached')
        assert env.service.row(lid)['state'] == 'cancelled'

        # 5. Member visits cancelled lottery
        viewer['id'] = MEMBER
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{lid}')
        page.locator('.consolation-card').wait_for()
        assert '本期活动已取消' in page.locator('.consolation-card').inner_text()
        assert not page.locator('#lot-join').count()

        # 6. Member home shows ongoing lotteries
        # Create an active free lottery
        fresh_lid = create(env, config(title='首页展示抽奖', prizes=[{'kind': 'points', 'name': '100 积分', 'quantity': 1, 'amount': 100}]))
        page.goto('https://app.example.test/app')
        page.locator('#home-ongoing').wait_for()
        assert '首页展示抽奖' in page.locator('#home-ongoing').inner_text()
        page.locator(f'#home-ongoing [data-lot-id="{fresh_lid}"]').click()
        page.locator('#lot-join').wait_for()
        assert page.locator('.lot-hero h3').inner_text() == '首页展示抽奖'

        browser.close()
