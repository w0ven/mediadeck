"""Real Chromium end-to-end tests for eligibility-v2 form linkage and prize pool UI.

Tests:
1. Mode switch (manual <-> timed):
   - manual hides deadline and reminder; clears deadline to null and reminder to false on submit.
   - timed displays deadline (required) and reminder.
   - switching with uncommitted prize edit is blocked without losing edits.
2. Audience linkage (member <-> all/guest):
   - member shows entry fee and threshold subpanel.
   - all/guest hides them, submits zeros, doesn't leave hidden values.
   - prize kinds in dropdown are filtered by audience.
   - switching audience with conflicting prizes in pool is blocked with clear error and preserves pool.
3. Prize tags & button state:
   - eligibility_version=2 lotteries render audience tags for prizes.
   - ineligibility by prize type (prize_eligible=False) disables join button with reason.
"""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.main import app
from tests.test_lottery import ADMIN, GUEST, MEMBER, config, create
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import TELEGRAM_STUB, headers


def test_lottery_conditional_form_linkage_and_prize_rules(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    output = Path(os.getenv('LOTTERY_CONDITIONAL_ARTIFACTS', str(tmp_path / 'screenshots')))
    output.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=2, reduced_motion='reduce')
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

        # 1. Open new activity form
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').click()
        page.locator('#lot-new').click()
        page.locator('#lot-title').fill('新联动规则活动')

        # Default audience is all -> fee and thresholds must be hidden
        assert not page.locator('#lot-fee').is_visible()
        assert not page.locator('#lot-min-level').is_visible()

        # Switch to member -> fee and thresholds become visible
        page.locator('#lot-audience').select_option('member')
        assert page.locator('#lot-fee').is_visible()
        assert page.locator('#lot-min-level').is_visible()
        page.locator('#lot-fee').fill('30')
        page.locator('#lot-min-level').select_option('2')
        page.locator('#lot-min-points').fill('100')
        page.locator('#lot-min-watch').fill('5')

        # Dropdown options for member: registration is excluded
        page.locator('#lot-add').click()
        kind_options = page.locator('#lot-kind option').all_inner_texts()
        assert not any('注册' in opt for opt in kind_options)
        assert any('积分' in opt for opt in kind_options)

        # Switch attempt while editing prize: should be blocked
        page.locator('#lot-mode').select_option('manual')
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        assert page.locator('#lot-mode').input_value() == 'timed'

        page.locator('#lot-audience').select_option('all')
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        assert page.locator('#lot-audience').input_value() == 'member'

        # Cancel editing prize
        page.locator('#lot-prize-cancel').click()
        assert page.locator('#lot-kind').count() == 0

        # Switch to all -> fee and thresholds hidden, and should clear on save
        page.locator('#lot-audience').select_option('all')
        assert not page.locator('#lot-fee').is_visible()
        assert not page.locator('#lot-min-level').is_visible()

        # Add registration prize and manual prize in 'all' audience
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('registration')
        page.locator('#lot-prize-ok').click()

        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('manual')
        page.locator('#lot-prize-name').fill('定制周边徽章')
        page.locator('#lot-instructions').fill('中奖后私聊群主领取')
        page.locator('#lot-prize-ok').click()
        assert page.locator('.prize-row').count() == 2

        # Conflict check: switching to 'member' when registration prize exists must be rejected
        page.locator('#lot-audience').select_option('member')
        assert '包含本人专属注册资格奖，与「仅已注册」冲突' in page.locator('#lot-form-error').inner_text()
        assert page.locator('#lot-audience').input_value() == 'all'
        assert page.locator('.prize-row').count() == 2  # not deleted

        # Manual draw switch: hides deadline and reminder
        assert page.locator('#lot-date').is_visible()
        assert page.locator('#lot-reminder').is_visible()
        page.locator('#lot-mode').select_option('manual')
        assert not page.locator('#lot-date').is_visible()
        assert not page.locator('#lot-reminder').is_visible()
        assert '开奖前均可报名' in page.locator('#lot-date-note').inner_text()

        # Save draft
        page.screenshot(path=str(output / 'create-linkage.png'), full_page=True)
        page.locator('#lot-save').click()
        page.locator('#lot-preview').wait_for()

        lid = app.state.db.one('SELECT id FROM lotteries ORDER BY created_at DESC')['id']
        row = env.service.row(lid)
        c = row['config']
        assert c['audience'] == 'all'
        assert c['draw_mode'] == 'manual'
        assert c['deadline'] is None
        assert c['reminder'] is False
        assert c['entry_fee'] == 0
        assert c['min_level'] == 0
        assert c['min_points'] == 0
        assert c['min_watch_hours'] == 0
        assert c['eligibility_version'] == 2

        # 2. Publish activity
        page.locator('#lot-preview').click()
        page.locator('#lot-publish').click()
        page.locator('#lot-draw').wait_for()

        # 3. Test prize tags in participant view
        url = f'https://app.example.test/app?tgWebAppStartParam=lottery_{lid}'
        page.goto(url)
        page.locator('#lot-join').wait_for()
        assert page.locator('.prize-tag.tag-guest').filter(has_text='仅未注册').count() == 1
        assert page.locator('.prize-tag.tag-all').filter(has_text='通用奖品').count() == 1
        page.screenshot(path=str(output / 'prize-tags.png'), full_page=True)

        # 4. Test ineligibility reason button state
        # Create an activity with only member prizes (points)
        member_only_lid = create(env, config(title='会员专享积分池', audience='all', draw_mode='manual',
                                              prizes=[{'kind': 'points', 'name': '100 积分', 'quantity': 1, 'amount': 100}]))
        # Guest visits member_only_lid -> prize_eligible is False
        viewer['id'] = GUEST
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{member_only_lid}')
        page.locator('#lot-join').wait_for()
        assert page.locator('#lot-join').is_disabled()
        assert '本期奖品仅限已注册者' in page.locator('#lot-join').inner_text()

        # Member visits member_only_lid -> prize_eligible is True
        viewer['id'] = MEMBER
        page.goto(f'https://app.example.test/app?tgWebAppStartParam=lottery_{member_only_lid}')
        page.locator('#lot-join').wait_for()
        assert not page.locator('#lot-join').is_disabled()
        assert page.locator('#lot-join').inner_text() in ('立即参与', '收下我的幸运号码')

        browser.close()
