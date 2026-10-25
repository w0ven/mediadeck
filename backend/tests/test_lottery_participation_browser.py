"""Integration tests for lottery participation, admin switching, and fee payments."""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.main import app
from tests.test_lottery import ADMIN, GUEST, MEMBER, config, create, draw
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import TELEGRAM_STUB, headers


def test_admin_participation_and_manage_toggle(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    output = Path(os.getenv('LOTTERY_PARTICIPATION_ARTIFACTS', '/opt/openbear/workspace/artifacts/lottery/participation'))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
        page.set_default_timeout(8000)
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
        page.route('**/*', bridge)

        # 1. Create a free activity
        lid = create(env, config(title='星光惊喜抽奖', draw_mode='manual', prizes=[{'kind': 'points', 'name': '200 积分', 'quantity': 2, 'amount': 200}]))
        url = 'https://app.example.test/app?tgWebAppStartParam=lottery_' + lid

        # Admin opens via deep link -> defaults to participant view
        page.goto(url)
        page.locator('#lot-join').wait_for()
        assert page.locator('#lot-to-manage').is_visible()
        assert not page.locator('#lot-records,#lot-draw').count()
        assert page.locator('#lot-join').inner_text() in ('立即参与', '收下我的幸运号码')

        # Admin takes participation screenshot
        page.screenshot(path=str(output / 'admin-join.png'), full_page=True)

        # Admin signs up
        page.locator('#lot-join').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()
        assert page.locator('#lot-join').inner_text() == '已报名 · 等待开奖'
        assert page.locator('#lot-to-manage').is_visible()

        # Admin clicks "管理本期" to switch into management mode
        page.locator('#lot-to-manage').click()
        page.locator('#lot-draw').wait_for()
        assert page.locator('#lot-records').is_visible()
        assert page.locator('#lot-to-participant').is_visible()

        # Admin switches back to participant view
        page.locator('#lot-to-participant').click()
        page.locator('#lot-join').wait_for()
        assert page.locator('#lot-to-manage').is_visible()

        # Switch to GUEST: verify guest has no manage button
        viewer['id'] = GUEST
        page.goto(url)
        page.locator('#lot-join').wait_for()
        assert not page.locator('#lot-to-manage,#lot-to-participant,#lot-records').count()

        browser.close()


def test_paid_lottery_ui_flow_and_modals(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    output = Path(os.getenv('LOTTERY_PARTICIPATION_ARTIFACTS', '/opt/openbear/workspace/artifacts/lottery/participation'))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
        page.set_default_timeout(8000)
        viewer = {'id': ADMIN}

        def bridge(route):
            r, u = route.request, urlsplit(route.request.url)
            if u.hostname == 'telegram.org':
                return route.fulfill(content_type='application/javascript',
                                     body=TELEGRAM_STUB % json.dumps(headers(viewer['id'])['X-Telegram-Init-Data']))
            if u.hostname != 'app.example.test':
                return route.abort()
            content = r.post_data_buffer
            res = env.client.request(r.method, u.path + ('?' + u.query if u.query else ''),
                                     content=content, auth=None,
                                     headers={**{k: v for k, v in r.headers.items()
                                                 if k.lower() in ('content-type', 'x-telegram-init-data')},
                                              'CF-Connecting-IP': env.ip_for(viewer['id'])})
            route.fulfill(status=res.status_code, body=res.content,
                          headers={k: v for k, v in res.headers.items() if k not in ('content-length', 'content-encoding')})
        page.route('**/*', bridge)

        # Admin creates paid lottery with 50 points fee
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').click()
        page.locator('#lot-new').click()
        page.locator('#lot-title').fill('积分大转盘')
        page.locator('#lot-audience').select_option('member')
        page.locator('#lot-fee').fill('50')
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('points')
        page.locator('#lot-prize-ok').click()
        page.locator('#lot-mode').select_option('manual')
        page.locator('#lot-save').click()
        page.locator('#lot-preview').wait_for()

        # Check UI screenshot for create draft
        page.screenshot(path=str(output / 'create-paid.png'), full_page=True)

        page.locator('#lot-preview').click()
        page.locator('#lot-publish').click()
        page.locator('#lot-draw').wait_for()
        lid = app.state.db.one('SELECT id FROM lotteries ORDER BY created_at DESC')['id']

        # Give Member 100 points
        env.service.points.add('u2', 100, 'admin.mint', 'test-mint')

        # Switch to Member
        viewer['id'] = MEMBER
        page.goto('https://app.example.test/app?tgWebAppStartParam=lottery_' + lid)
        page.locator('#lot-join').wait_for()
        assert page.locator('#lot-join').inner_text() == '支付 50 积分报名'
        assert '我的余额：100 积分' in page.locator('.lottery').inner_text()

        # Click payment join -> modal opens
        page.locator('#lot-join').click()
        page.locator('#lot-ok').wait_for()
        assert '50 积分' in page.locator('.lottery').inner_text()

        # Cancel confirmation
        page.locator('#lot-cancel').click()
        page.locator('#lot-join').wait_for()
        assert env.service.points.balance('u2') == 100

        # Confirm payment
        page.locator('#lot-join').click()
        page.locator('#lot-ok').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()
        assert env.service.points.balance('u2') == 50
        assert page.locator('#lot-join').inner_text() == '已报名 · 等待开奖'
        assert '我的余额：50 积分' in page.locator('.lottery').inner_text()

        # Settle lottery and take result screenshot
        draw(env, lid)
        page.locator('#lot-refresh').click()
        page.locator('.result-banner').wait_for()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(output / 'settled-result.png'), full_page=True)

        browser.close()
