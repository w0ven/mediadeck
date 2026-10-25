"""Browser tests for count draw mode, early draw buttons, and private start requirements."""
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.main import app
from tests.lottery_private_helpers import private_start
from tests.test_lottery import ADMIN, GUEST, MEMBER, config, create
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import TELEGRAM_STUB, headers


def test_lottery_count_mode_early_draw_and_private_start(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright

    artifacts_dir = Path(os.getenv('LOTTERY_COUNT_DRAW_ARTIFACTS', str(tmp_path / 'screenshots')))
    artifacts_dir.mkdir(parents=True, exist_ok=True)

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

        # -------------------------------------------------------------
        # 1. Editor verification: Switch modes & invalid max_entries validation
        # -------------------------------------------------------------
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').click()
        page.locator('#lot-new').click()

        # Default is timed: date is required and visible; reminder checkbox visible
        page.locator('#lot-title').fill('满员开奖测试活动')
        page.locator('#lot-thread').fill('2048')
        assert page.locator('#lot-date').is_visible()
        assert page.locator('#lot-reminder').is_visible()
        assert not page.locator('#lot-max-entries').count()

        # Add a manual prize so GUEST, MEMBER and ADMIN all have prize eligibility (audience=all)
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('manual')
        page.locator('#lot-prize-name').fill('通用限定周边')
        page.locator('#lot-instructions').fill('中奖后联系管理员兑领')
        page.locator('#lot-quantity').fill('2')

        # In-progress edit protection: changing mode while editing prize must be blocked
        page.locator('#lot-mode').select_option('count')
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        assert page.locator('#lot-mode').input_value() == 'timed'

        # Join prize into pool
        page.locator('#lot-prize-ok').click()
        assert page.locator('.prize-row').count() == 1

        # Now switch to 'count' (满员开奖)
        page.locator('#lot-mode').select_option('count')
        assert page.locator('#lot-max-entries').is_visible()
        assert page.locator('#lot-max-entries').input_value() == '100'
        assert not page.locator('#lot-date').count()
        assert not page.locator('#lot-reminder').count()
        assert '满 X 人自动开奖' in page.locator('#lot-date-note').inner_text()

        # Test invalid values for max_entries: 0, 100001, 1.5, empty
        # Must show error, not silently submit, and not overwrite user input with 100!
        for invalid_val in ('0', '100001', '1.5', ''):
            page.locator('#lot-max-entries').fill(invalid_val)
            page.locator('#lot-save').click()
            assert '满员开奖人数须为 1–100000 之间的整数' in page.locator('#lot-form-error').inner_text()
            # Verify input was not silently overwritten
            assert page.locator('#lot-max-entries').input_value() == invalid_val
            # Verify still in editor, not in preview/saved
            assert page.locator('#lot-max-entries').is_visible()

        # Test boundary values: 1 and 100000 are valid
        page.locator('#lot-max-entries').fill('100000')
        page.locator('#lot-title').focus()  # trigger blur/collect
        page.locator('#lot-max-entries').fill('1')
        page.locator('#lot-title').focus()
        assert page.locator('#lot-max-entries').input_value() == '1'

        # Switch to 'manual'
        page.locator('#lot-mode').select_option('manual')
        assert not page.locator('#lot-date').count()
        assert not page.locator('#lot-max-entries').count()
        assert not page.locator('#lot-reminder').count()
        assert '管理员开奖前均可报名' in page.locator('#lot-date-note').inner_text()

        # Switch back to 'count', verify other fields preserved, set to 3 for multi-entry test
        page.locator('#lot-mode').select_option('count')
        page.locator('#lot-max-entries').fill('3')
        assert page.locator('#lot-title').input_value() == '满员开奖测试活动'
        assert page.locator('#lot-thread').input_value() == '2048'
        assert page.locator('.prize-row').count() == 1

        # Save draft
        page.locator('#lot-save').click()
        page.locator('#lot-preview').wait_for()

        # Check DB config of the saved draft: max_entries=3, deadline=None, reminder=False
        draft_row = app.state.db.one('SELECT * FROM lotteries WHERE state="draft" ORDER BY id DESC')
        cfg = json.loads(draft_row['config_json'])
        assert cfg['draw_mode'] == 'count'
        assert cfg['max_entries'] == 3
        assert cfg['deadline'] is None
        assert cfg['reminder'] is False
        assert cfg['thread_id'] == 2048

        # -------------------------------------------------------------
        # 2. Publish and check UI info
        # -------------------------------------------------------------
        page.locator('#lot-preview').click()
        page.locator('#lot-publish').click()
        page.locator('#lot-draw').wait_for()
        lid = draft_row['id']

        # In admin management view:
        # Check hero text: displays "满 3 人自动开奖 · 进度 0/3 人"
        assert '满 3 人自动开奖 · 进度 0/3 人' in page.locator('.hero-bottom').inner_text()
        assert page.locator('#lot-draw').is_visible()
        assert page.locator('#lot-draw').inner_text() == '立即开奖'

        # Optional single screenshot
        page.screenshot(path=str(artifacts_dir / 'count-draw-manage.png'), full_page=True)

        # -------------------------------------------------------------
        # 3. Timed activity early draw verification
        # -------------------------------------------------------------
        # Create a timed activity with future deadline (e.g. 1 hour later)
        timed_lid = create(env, config(
            title='未来定时抽奖',
            draw_mode='timed',
            deadline=int(time.time()) + 7200,
            prizes=[{'kind': 'points', 'name': '50 积分', 'quantity': 1, 'amount': 50}]
        ))

        # Admin opens timed activity in manage mode
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').click()
        page.locator(f'[data-lot-open="{timed_lid}"]').click()
        page.locator('#lot-draw').wait_for()

        # Timed activity NOT expired yet, but admin still has "立即开奖" button!
        assert page.locator('#lot-draw').is_visible()

        # Click "立即开奖" -> modal pops up with correct warning
        page.locator('#lot-draw').click()
        page.locator('#lot-ok').wait_for()
        assert '立即停止报名并开奖' in page.locator('.lot-sheet-desc').inner_text()

        # Click Cancel: draw is NOT called
        page.locator('#lot-cancel').click()
        page.locator('#lot-draw').wait_for()
        assert app.state.db.one('SELECT state FROM lotteries WHERE id=?', (timed_lid,))['state'] == 'published'

        # Click confirm: settles immediately
        page.locator('#lot-draw').click()
        page.locator('#lot-ok').click()
        page.wait_for_function("document.querySelector('#lot-draw') === null")
        assert app.state.db.one('SELECT state FROM lotteries WHERE id=?', (timed_lid,))['state'] == 'settled'

        # -------------------------------------------------------------
        # 4. Count mode participation, progress and auto-draw on threshold
        # -------------------------------------------------------------
        count_url = 'https://app.example.test/app?tgWebAppStartParam=lottery_' + lid

        # User 1: ADMIN signs up (private_start already set in fixture for ADMIN)
        page.goto(count_url)
        page.locator('#lot-join').wait_for()
        # Ordinary participant view doesn't have draw button
        assert not page.locator('#lot-draw').count()
        assert '满 3 人自动开奖 · 进度 0/3 人' in page.locator('.hero-bottom').inner_text()

        page.locator('#lot-join').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()

        # User 2: GUEST (without private_start yet -> test private start requirement!)
        # Clear private marker for GUEST to test requirement
        bot_id = env.service.bot._token().split(':', 1)[0]
        app.state.db.execute('DELETE FROM meta WHERE key=?', (f'telegram.private:{bot_id}:{GUEST}',))

        viewer['id'] = GUEST
        page.goto(count_url)
        page.locator('#lot-start-bot').wait_for()

        # Verify "请先私聊启动 Bot" prompt and buttons
        assert '请先私聊启动 Bot' in page.locator('#lot-private-banner').inner_text()
        assert page.locator('#lot-start-bot').is_visible()
        assert page.locator('#lot-retry-private').is_visible()
        assert not page.locator('#lot-join').count()

        # Verify clicking "#lot-start-bot" invokes openBot with exact bot_start_url
        page.evaluate("window.__opened = []")
        page.locator('#lot-start-bot').click()
        opened = page.evaluate("window.__opened")
        assert len(opened) == 1
        assert f'https://t.me/example_lottery_bot?start=lottery_{lid}' == opened[0]

        # Simulate user sends /start in Telegram private chat
        private_start(env.service, GUEST)

        # Click "我已启动，刷新报名"
        page.locator('#lot-retry-private').click()
        page.locator('#lot-join').wait_for()
        assert '立即参与' in page.locator('#lot-join').inner_text()
        assert '进度 1/3 人' in page.locator('.hero-bottom').inner_text()

        # Guest signs up
        page.locator('#lot-join').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()

        # User 3: MEMBER (already private_started in fixture)
        # Signs up -> this is the 3rd participant, reaching max_entries=3!
        viewer['id'] = MEMBER
        page.goto(count_url)
        page.locator('#lot-join').wait_for()
        assert '进度 2/3 人' in page.locator('.hero-bottom').inner_text()

        # Member signs up (3rd entry triggers immediate synchronous auto-settle on server)
        page.locator('#lot-join').click()

        # Should immediately show settled results (e.g. 幸运名单 or consolation-card or award)
        page.locator('.result-banner').wait_for()
        assert app.state.db.one('SELECT state FROM lotteries WHERE id=?', (lid,))['state'] == 'settled'

        # Check all 3 entered
        entry_row = app.state.db.one('SELECT COUNT(*) AS c FROM lottery_entries WHERE lottery_id=?', (lid,))
        assert entry_row['c'] == 3

        # Verify Member (the last participant) is in the entries
        member_entry = app.state.db.one('SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lid, str(MEMBER)))
        assert member_entry is not None

        # Verify admin view also shows settled without draw button
        viewer['id'] = ADMIN
        page.goto(count_url)
        page.locator('#lot-to-manage').click()
        page.locator('.lot-role-bar').wait_for()
        assert not page.locator('#lot-draw').count()
        assert '已开奖' in page.locator('.hero-top-row').inner_text()

        # -------------------------------------------------------------
        # 5. Non-participant non-private user visiting settled / cancelled / expired timed
        # Must show results/refresh, NOT "先私聊启动 Bot"
        # -------------------------------------------------------------
        # 888000999 is an unverified user without private_start
        UNREGISTERED_GUEST = 888000999
        viewer['id'] = UNREGISTERED_GUEST

        # 5.1 Settled activity (count-mode settled)
        page.goto(count_url)
        page.locator('#lot-result-refresh').wait_for()
        assert not page.locator('#lot-start-bot').count()
        assert not page.locator('#lot-retry-private').count()
        assert not page.locator('#lot-private-banner').count()
        assert '本期抽奖已结束' in page.locator('.consolation-card').inner_text()

        # 5.2 Cancelled activity
        canc_lid = create(env, config(title='将被取消的活动', draw_mode='manual'))
        app.state.lottery.cancel(canc_lid, actor='admin', confirm=True)
        canc_url = 'https://app.example.test/app?tgWebAppStartParam=lottery_' + canc_lid
        page.goto(canc_url)
        page.locator('.consolation-card').wait_for()
        assert not page.locator('#lot-start-bot').count()
        assert not page.locator('#lot-retry-private').count()
        assert not page.locator('#lot-private-banner').count()
        assert '本期活动已取消' in page.locator('.consolation-card').inner_text()

        # 5.3 Expired timed activity: create active then simulate time passage in DB
        exp_lid = create(env, config(title='已过期定时活动', draw_mode='timed', deadline=int(time.time()) + 3600))
        # Update deadline to 100s in the past in DB
        cur_cfg = json.loads(app.state.db.one('SELECT config_json FROM lotteries WHERE id=?', (exp_lid,))['config_json'])
        cur_cfg['deadline'] = int(time.time()) - 100
        app.state.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(cur_cfg), exp_lid))
        exp_url = 'https://app.example.test/app?tgWebAppStartParam=lottery_' + exp_lid
        page.goto(exp_url)
        page.locator('#lot-join').wait_for()
        assert not page.locator('#lot-start-bot').count()
        assert not page.locator('#lot-retry-private').count()
        assert not page.locator('#lot-private-banner').count()
        assert '报名已截止' in page.locator('#lot-join').inner_text()
        assert page.locator('#lot-join').is_disabled()

        browser.close()
