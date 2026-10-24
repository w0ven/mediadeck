"""Chromium uses the shipped Mini App, a local TestClient and mock Telegram.

No server/real Telegram/domain is contacted. Optional screenshots are opt-in.
"""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.main import app
from tests.test_lottery import ADMIN, GUEST, MEMBER, config, create, draw, join
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import TELEGRAM_STUB, headers


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
                response = env.client.request(r.method, u.path + ('?' + u.query if u.query else ''),
                                              content=r.post_data_buffer, auth=None,
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
            assert page.locator('.lottery img').evaluate_all('(imgs) => imgs.every(i=>i.complete && i.naturalWidth>0)')
            if os.getenv('LOTTERY_UI_ARTIFACTS'):
                page.wait_for_function("document.querySelector('#toast').hidden")
            page.screenshot(path=str(output / name), full_page=True)

        admin = page_for(ADMIN)
        admin.goto('https://app.example.test/app')
        admin.locator('[data-go="lottery"]').click()
        admin.locator('#lot-new').click()
        admin.locator('#lot-title').fill('周末星光放送')
        admin.locator('#lot-date').fill('2099-10-25T21:00')  # server rejects out-of-range
        admin.locator('#lot-add').click()
        admin.locator('#lot-kind').select_option('points')
        admin.locator('#lot-quantity').fill('3')
        admin.locator('#lot-prize-ok').click()
        admin.locator('#lot-save').click()
        admin.locator('#lot-form-error').filter(has_text='截止时间').wait_for()
        deadline = config()['deadline']
        from datetime import datetime

        from app.modules.economy_rules import BEIJING
        admin.locator('#lot-date').fill(datetime.fromtimestamp(deadline, BEIJING).strftime('%Y-%m-%dT%H:%M'))
        admin.locator('#lot-save').click()
        admin.locator('#lot-preview').wait_for()
        lid = app.state.db.one('SELECT id FROM lotteries ORDER BY created_at DESC')['id']
        admin.locator('#lot-edit').click()
        admin.locator('#lot-title').fill('星光放送 · 真实活动')
        admin.locator('#lot-save').click()
        admin.locator('#lot-preview').click()
        fits(admin, '01-admin-publish-preview.png')
        admin.locator('#lot-publish').click()
        admin.locator('#lot-draw').wait_for()
        assert app.state.lottery.row(lid)['state'] == 'published'
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
    lid = create(env, config(prizes=[{'kind': 'points', 'name': '星光积分', 'quantity': 1, 'amount': 5}]))
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
        page.locator('#lot-records').click()
        page.locator('.lot-cluster').wait_for()
        assert '相同浏览器安装标识' in page.locator('.lot-cluster').inner_text()
        assert str(GUEST) in page.locator('.lot-cluster').inner_text()
        assert str(MEMBER) in page.locator('.lot-cluster').inner_text()
        page.locator('details summary').click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('.lottery img').evaluate_all('(imgs)=>imgs.every(i=>i.complete && i.naturalWidth>0)')
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
