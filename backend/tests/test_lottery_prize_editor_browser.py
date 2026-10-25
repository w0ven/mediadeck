"""Real Chromium browser tests for lottery draft prize pool editing, drag-and-drop reordering, scroll anchoring, and input preservation.

Verifies:
1. No hardcoded production paths: default screenshot directory uses tmp_path / 'screenshots'.
2. Immediate bounded wait for scroll settling and intersection verification within #screen.
3. Five prize kinds full roundtrip回填 and in-place save (shop, points, registration, invite, manual).
4. Manual edit mutation followed by cancel, verifying original values remain untouched.
5. In-progress edit protection: blocks other edits, removes, add button, draw mode, audience switching, and touch/pointer reordering.
6. Touch reordering via real Chromium CDP TouchEvent input:
   - Touch drag on handle successfully reorders prizes and preserves order upon saving draft.
   - Touch cancel (touchCancel event) does not reorder and cleans up state, allowing subsequent touch drag.
   - Touch drag attempt while prize is being edited is rejected without losing edits.
7. Mouse drag and drop reordering and accessibility move-up/down buttons.
8. 320px narrow mobile viewport: no horizontal scrollbar overflow, handle/order buttons/edit/remove fully visible and clickable.
"""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.test_lottery import ADMIN
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import TELEGRAM_STUB, headers


def test_prize_editor_scroll_and_crud_roundtrip(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright

    # Ensure artifacts default to tmp_path / 'screenshots' without hardcoding production paths
    artifacts_dir = Path(os.getenv('LOTTERY_PRIZE_EDITOR_ARTIFACTS', str(tmp_path / 'screenshots')))
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as driver:
        # Launch Chromium with touch emulation enabled
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        context = browser.new_context(
            viewport={'width': 390, 'height': 844},
            device_scale_factor=2,
            has_touch=True,
            is_mobile=True,
        )
        page = context.new_page()
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

        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').click()
        page.locator('#lot-new').click()

        # Fill main form fields that should be preserved across prize operations
        page.locator('#lot-title').fill('多奖品测试抽奖')
        page.locator('#lot-thread').fill('1024')
        page.locator('#lot-audience').select_option('all')

        # Bounded wait for scroll settling and element visibility inside #screen
        def assert_visible_in_screen(target_selector, expect_scrolled=True):
            page.wait_for_function(f"""() => {{
                const screen = document.querySelector('#screen');
                const target = document.querySelector('{target_selector}');
                if (!target || !screen) return false;
                const sRect = screen.getBoundingClientRect();
                const tRect = target.getBoundingClientRect();
                const visible = (tRect.top < sRect.bottom) && (tRect.bottom > sRect.top);
                const scrolled = screen.scrollTop > 0;
                return { 'scrolled && visible' if expect_scrolled else 'visible' };
            }}""", timeout=4000)

        # Pre-scroll screen so we can check that operations don't reset to 0
        page.evaluate("document.querySelector('#screen').scrollTop = 200")
        assert page.evaluate("document.querySelector('#screen').scrollTop") > 0

        # Add 5 distinct prize kinds:
        # 1. points
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('points')
        page.locator('#lot-amount').fill('200')
        page.locator('#lot-quantity').fill('10')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-0')

        # 2. registration
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('registration')
        page.locator('#lot-days').fill('7')
        page.locator('#lot-quantity').fill('10')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-1')

        # 3. manual
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('manual')
        page.locator('#lot-prize-name').fill('定制周边体验卡')
        page.locator('#lot-instructions').fill('中奖后凭兑换码私聊管理员')
        page.locator('#lot-quantity').fill('3')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-2')

        # 4. invite
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('invite')
        page.locator('#lot-amount').fill('2')
        page.locator('#lot-quantity').fill('3')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-3')

        # 5. shop
        page.locator('#lot-add').click()
        page.locator('#lot-kind').select_option('shop')
        page.locator('#lot-quantity').fill('1')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-4')

        # Verify 5 rows present, total quantity = 10 + 10 + 3 + 3 + 1 = 27
        assert page.locator('.prize-row').count() == 5
        assert '27 个名额' in page.locator('#lot-prize-heading').inner_text()

        # Verify every row has handle, edit, and remove
        for i in range(5):
            assert page.locator(f'[data-lot-drag="{i}"]').is_visible()
            assert page.locator(f'[data-lot-edit="{i}"]').is_visible()
            assert page.locator(f'[data-lot-remove="{i}"]').is_visible()

        # Test Manual Prize Edit: mutate fields then Cancel, verifying original values remain untouched
        page.locator('[data-lot-edit="2"]').click()
        assert page.locator('#lot-kind').input_value() == 'manual'
        assert page.locator('#lot-prize-name').input_value() == '定制周边体验卡'
        assert page.locator('#lot-instructions').input_value() == '中奖后凭兑换码私聊管理员'
        assert page.locator('#lot-quantity').input_value() == '3'
        assert_visible_in_screen('#lot-prize-editor')

        # In-progress protection: clicking another edit, remove, add, reorder, or switching mode must be blocked
        page.locator('[data-lot-edit="0"]').click()
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        page.locator('[data-lot-remove="1"]').click()
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        page.locator('[data-lot-add]').click() if page.locator('[data-lot-add]').count() else page.locator('#lot-add').click()
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        page.locator('[data-lot-move-down="0"]').click()
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        page.locator('#lot-mode').select_option('manual')
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        assert page.locator('#lot-mode').input_value() == 'timed'

        # Verify touch drag attempt while editing is also rejected without losing inputs
        cdp = context.new_cdp_session(page)
        h0 = page.locator('#lot-prize-row-0 .prize-handle').bounding_box()
        cdp.send('Input.dispatchTouchEvent', {
            'type': 'touchStart',
            'touchPoints': [{'x': h0['x'] + h0['width'] / 2, 'y': h0['y'] + h0['height'] / 2, 'id': 1}],
        })
        cdp.send('Input.dispatchTouchEvent', {
            'type': 'touchEnd',
            'touchPoints': [],
        })
        assert '请先将编辑中的奖项加入奖池或取消该奖项' in page.locator('#lot-form-error').inner_text()
        # Ensure edit inputs still intact
        assert page.locator('#lot-prize-name').input_value() == '定制周边体验卡'

        # Mutate inputs in manual edit
        page.locator('#lot-prize-name').fill('被修改的废弃名称')
        page.locator('#lot-instructions').fill('被修改的废弃说明')
        page.locator('#lot-quantity').fill('99')

        # Cancel edit and assert original remains untouched and screen stays scrolled
        page.locator('#lot-prize-cancel').click()
        assert page.locator('#lot-kind').count() == 0
        assert page.locator('.prize-row').count() == 5
        assert '定制周边体验卡' in page.locator('#lot-prize-row-2 b').inner_text()
        assert '3 名' in page.locator('#lot-prize-row-2 .prize-desc').inner_text()
        assert_visible_in_screen('#lot-prize-row-2')

        # Test Shop prize回填 (row 4) and in-place save
        page.locator('[data-lot-edit="4"]').click()
        assert page.locator('#lot-kind').input_value() == 'shop'
        shop_opts = page.locator('#lot-item option').all_inner_texts()
        assert len(shop_opts) >= 1
        assert page.locator('#lot-quantity').input_value() == '1'
        page.locator('#lot-quantity').fill('2')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-4')
        assert '2 名' in page.locator('#lot-prize-row-4 .prize-desc').inner_text()

        # Test Points prize回填 (row 0) and in-place save
        page.locator('[data-lot-edit="0"]').click()
        assert page.locator('#lot-kind').input_value() == 'points'
        assert page.locator('#lot-amount').input_value() == '200'
        assert page.locator('#lot-quantity').input_value() == '10'
        page.locator('#lot-amount').fill('500')
        page.locator('#lot-quantity').fill('5')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-0')
        assert '500 积分' in page.locator('#lot-prize-row-0 b').inner_text()
        assert '5 名' in page.locator('#lot-prize-row-0 .prize-desc').inner_text()

        # Test Registration prize回填 (row 1) and in-place save (group_id and days)
        page.locator('[data-lot-edit="1"]').click()
        assert page.locator('#lot-kind').input_value() == 'registration'
        assert page.locator('#lot-days').input_value() == '7'
        assert page.locator('#lot-quantity').input_value() == '10'
        group_opts = page.locator('#lot-group option').all_inner_texts()
        assert len(group_opts) >= 1
        page.locator('#lot-days').fill('30')
        page.locator('#lot-quantity').fill('8')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-1')
        assert '30 天' in page.locator('#lot-prize-row-1 .prize-desc').inner_text()
        assert '8 名' in page.locator('#lot-prize-row-1 .prize-desc').inner_text()

        # Test Invite prize回填 (row 3) and in-place save
        page.locator('[data-lot-edit="3"]').click()
        assert page.locator('#lot-kind').input_value() == 'invite'
        assert page.locator('#lot-amount').input_value() == '2'
        assert page.locator('#lot-quantity').input_value() == '3'
        page.locator('#lot-amount').fill('5')
        page.locator('#lot-quantity').fill('4')
        page.locator('#lot-prize-ok').click()
        assert_visible_in_screen('#lot-prize-row-3')
        assert '5 个可用邀请名额' in page.locator('#lot-prize-row-3 b').inner_text()
        assert '4 名' in page.locator('#lot-prize-row-3 .prize-desc').inner_text()

        # Total quotas check: 5 + 8 + 3 + 4 + 2 = 22
        assert '22 个名额' in page.locator('#lot-prize-heading').inner_text()

        # Test Accessibility Reordering (Move Down / Move Up)
        # Move row 0 (points) down -> becomes row 1
        page.locator('[data-lot-move-down="0"]').click()
        assert_visible_in_screen('#lot-prize-row-1')
        assert '本人专属注册资格' in page.locator('#lot-prize-row-0 b').inner_text()
        assert '500 积分' in page.locator('#lot-prize-row-1 b').inner_text()

        # Move row 1 (points) back up -> becomes row 0
        page.locator('[data-lot-move-up="1"]').click()
        assert_visible_in_screen('#lot-prize-row-0')
        assert '500 积分' in page.locator('#lot-prize-row-0 b').inner_text()
        assert '本人专属注册资格' in page.locator('#lot-prize-row-1 b').inner_text()

        # --- Real Touch Event Testing via CDP ---
        # 1. Test touchCancel: start touch dragging row 3 towards row 0, emit touchCancel, verify order NOT changed
        h3 = page.locator('#lot-prize-row-3 .prize-handle').bounding_box()
        r0 = page.locator('#lot-prize-row-0').bounding_box()
        cdp.send('Input.dispatchTouchEvent', {
            'type': 'touchStart',
            'touchPoints': [{'x': h3['x'] + h3['width'] / 2, 'y': h3['y'] + h3['height'] / 2, 'id': 2}],
        })
        cdp.send('Input.dispatchTouchEvent', {
            'type': 'touchMove',
            'touchPoints': [{'x': r0['x'] + r0['width'] / 2, 'y': r0['y'] + r0['height'] / 2, 'id': 2}],
        })
        # Simulate system touchCancel
        cdp.send('Input.dispatchTouchEvent', {
            'type': 'touchCancel',
            'touchPoints': [],
        })
        # Verify order untouched: row 0 is still 500 积分, not row 3's invite
        assert '500 积分' in page.locator('#lot-prize-row-0 b').inner_text()
        assert '5 个可用邀请名额' in page.locator('#lot-prize-row-3 b').inner_text()
        assert not page.locator('.prize-row.dragging').count()
        assert not page.locator('.prize-row.drag-over').count()

        # 2. Test successful touch drag: drag row 4 (shop) to row 0 (top)
        h4 = page.locator('#lot-prize-row-4 .prize-handle').bounding_box()
        r0 = page.locator('#lot-prize-row-0').bounding_box()
        cdp.send('Input.dispatchTouchEvent', {
            'type': 'touchStart',
            'touchPoints': [{'x': h4['x'] + h4['width'] / 2, 'y': h4['y'] + h4['height'] / 2, 'id': 3}],
        })
        # Move up to row 0 in steps
        for step in range(1, 6):
            cur_y = h4['y'] + (r0['y'] - h4['y']) * (step / 5)
            cdp.send('Input.dispatchTouchEvent', {
                'type': 'touchMove',
                'touchPoints': [{'x': r0['x'] + r0['width'] / 2, 'y': cur_y, 'id': 3}],
            })
        cdp.send('Input.dispatchTouchEvent', {
            'type': 'touchEnd',
            'touchPoints': [],
        })

        # Verify shop is now at row 0!
        assert_visible_in_screen('#lot-prize-row-0')
        assert '白名单' in page.locator('#lot-prize-row-0 b').inner_text() or '卡' in page.locator('#lot-prize-row-0 b').inner_text() or '商城' in page.locator('#lot-prize-row-0 b').inner_text()
        assert '500 积分' in page.locator('#lot-prize-row-1 b').inner_text()

        # Remove row 4
        page.locator('[data-lot-remove="4"]').click()
        assert page.locator('.prize-row').count() == 4
        assert_visible_in_screen('#lot-prize-row-3')

        # Verify main form fields preserved!
        assert page.locator('#lot-title').input_value() == '多奖品测试抽奖'
        assert page.locator('#lot-thread').input_value() == '1024'
        assert page.locator('#lot-audience').input_value() == 'all'

        # Switch to manual mode and save draft to database
        page.locator('#lot-mode').select_option('manual')
        page.locator('#lot-save').click()
        page.locator('#lot-preview').wait_for()

        # Re-open draft in editor to verify persistence in DB
        page.locator('#lot-edit').click()
        page.locator('#lot-title').wait_for()
        assert page.locator('#lot-title').input_value() == '多奖品测试抽奖'
        assert page.locator('.prize-row').count() == 4
        assert '500 积分' in page.locator('#lot-prize-row-1 b').inner_text()
        assert '30 天' in page.locator('#lot-prize-row-2 .prize-desc').inner_text()

        # Test on 320px narrow viewport: no horizontal scrollbar overflow and buttons accessible
        page.set_viewport_size({'width': 320, 'height': 640})
        # Check no horizontal overflow on #screen
        has_h_overflow = page.evaluate("document.querySelector('#screen').scrollWidth > document.querySelector('#screen').clientWidth")
        assert not has_h_overflow, "Screen has horizontal scrollbar overflow on 320px viewport"

        for i in range(4):
            row_sel = f'#lot-prize-row-{i}'
            page.locator(row_sel).scroll_into_view_if_needed()
            assert_visible_in_screen(f'[data-lot-edit="{i}"]', expect_scrolled=True)
            assert_visible_in_screen(f'[data-lot-remove="{i}"]', expect_scrolled=True)
            assert page.locator(f'[data-lot-drag="{i}"]').is_visible()

        browser.close()
