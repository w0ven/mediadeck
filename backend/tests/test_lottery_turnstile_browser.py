"""Browser end-to-end tests for Cloudflare Turnstile managed verification in lottery UI."""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.main import app
from tests.lottery_captcha_helpers import answer
from tests.lottery_private_helpers import private_start
from tests.test_lottery import ADMIN, GUEST, MEMBER, config, create
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import TELEGRAM_STUB, headers

MOCK_TURNSTILE_SCRIPT = """
(function() {
  window.__turnstileRenderCalls = [];
  window.__turnstileWidgets = new Map();
  var nextId = 1;

  window.turnstile = {
    render: function(container, options) {
      var id = 'mock-widget-' + (nextId++);
      var target = (typeof container === 'string') ? document.querySelector(container) : container;
      if (!target) return null;
      var el = document.createElement('div');
      el.id = id;
      el.className = 'mock-cf-turnstile';
      var isCompact = (options && options.size === 'compact');
      var width = isCompact ? '130px' : '300px';
      var height = isCompact ? '120px' : '65px';
      el.innerHTML = '<iframe class="cf-turnstile-iframe" style="width:' + width + ';height:' + height + ';border:none" title="Turnstile Mock"></iframe>';
      target.appendChild(el);

      var entry = { id: id, container: target, element: el, options: options, removed: false };
      window.__turnstileWidgets.set(id, entry);
      window.__turnstileRenderCalls.push({ id: id, options: options });
      if (window.__turnstileAutoAnswer && options && typeof options.callback === 'function') {
        var token = 'synthetic-auto-token-' + id;
        if (window.__turnstileTokens) window.__turnstileTokens.push({ token: token, cdata: options.cData });
        setTimeout(function() {
          if (!entry.removed) options.callback(token);
        }, 10);
      }
      return id;
    },
    reset: function(id) {
      var entry = window.__turnstileWidgets.get(id);
      if (entry && !entry.removed) {
        // Clear token state
      }
    },
    remove: function(id) {
      var entry = window.__turnstileWidgets.get(id);
      if (entry) {
        entry.removed = true;
        if (entry.element && entry.element.parentNode) {
          entry.element.parentNode.removeChild(entry.element);
        }
        window.__turnstileWidgets.delete(id);
      }
    },
    getResponse: function(id) {
      return 'mock-token';
    },
    isExpired: function(id) {
      return false;
    },
    ready: function(cb) {
      if (cb) cb();
    }
  };
})();
"""


def test_lottery_turnstile_managed_flow_and_regressions(request, tmp_path):
    env = request.getfixturevalue('lottery_env')
    captcha_state = env.turnstile
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright

    artifacts_dir = Path(os.getenv('LOTTERY_TURNSTILE_ARTIFACTS', str(tmp_path / 'screenshots')))
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
        page.set_default_timeout(8000)
        viewer = {'id': ADMIN}

        script_fail_target = {'fail': False}

        tg_theme = {'colorScheme': 'dark'}

        def bridge(route):
            r, u = route.request, urlsplit(route.request.url)
            if u.hostname == 'telegram.org':
                body = (TELEGRAM_STUB % json.dumps(headers(viewer['id'])['X-Telegram-Init-Data'])).replace("colorScheme: 'dark'", f"colorScheme: '{tg_theme['colorScheme']}'")
                return route.fulfill(content_type='application/javascript', body=body)
            if u.hostname == 'challenges.cloudflare.com':
                if script_fail_target['fail']:
                    return route.abort('failed')
                return route.fulfill(content_type='application/javascript', body=MOCK_TURNSTILE_SCRIPT)
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
        # 1. Create a published activity (automatically requires captcha & private start)
        # Use a manual prize so ADMIN, GUEST and MEMBER all have prize eligibility
        # -------------------------------------------------------------
        lid = create(env, config(title='CF Turnstile 托管防刷抽奖', draw_mode='manual',
                                prizes=[{'kind': 'manual', 'name': '全员纪念限定周边', 'quantity': 3, 'instructions': '中奖后联系兑领'}]))
        url = 'https://app.example.test/app?tgWebAppStartParam=lottery_' + lid

        # -------------------------------------------------------------
        # 2. Script load failure & retry without deadlock
        # -------------------------------------------------------------
        script_fail_target['fail'] = True
        page.goto(url)
        # ADMIN is registered and private_started in fixture
        # When script fails to load, UI shows error banner and retry button
        page.locator('#lot-captcha-area').wait_for()
        assert page.locator('#lot-join').is_disabled()
        assert '请先完成人机验证' in page.locator('#lot-join').inner_text()
        assert page.locator('#lot-captcha-reissue').is_visible()

        # Fix script and click retry -> loads successfully
        script_fail_target['fail'] = False
        page.locator('#lot-captcha-reissue').click()
        page.locator('.mock-cf-turnstile').wait_for()

        # Check render params recorded by mock Turnstile
        render_calls = page.evaluate("window.__turnstileRenderCalls")
        assert len(render_calls) >= 1
        last_render = render_calls[-1]['options']
        assert last_render['sitekey'] == 'synthetic-public-site-key'
        assert last_render['action'] == 'lottery_join'
        assert len(last_render['cData']) >= 16

        # Still disabled before callback execution
        assert page.locator('#lot-join').is_disabled()
        assert '请先完成人机验证' in page.locator('#lot-join').inner_text()

        # -------------------------------------------------------------
        # 3. Trigger Expired and Timeout callbacks -> button remains disabled with warning & retry button
        # -------------------------------------------------------------
        widget_id = page.evaluate("window.__turnstileRenderCalls[window.__turnstileRenderCalls.length-1].id")
        # Save old callback before widget is removed by reissue
        page.evaluate(f"window.__oldCallback = window.__turnstileWidgets.get('{widget_id}').options.callback")

        page.evaluate(f"window.__turnstileWidgets.get('{widget_id}').options['expired-callback']()")
        assert page.locator('#lot-join').is_disabled()
        assert '验证已过期' in page.locator('#lot-join').inner_text()
        assert page.locator('#lot-captcha-reissue').is_visible()
        assert '验证已过期，请点击重新验证' in page.locator('#lot-captcha-area').inner_text()

        # Click reissue to get a new challenge & recreate widget (this removes old widget!)
        page.locator('#lot-captcha-reissue').click()
        page.locator('.mock-cf-turnstile').wait_for()

        new_widget_id = page.evaluate("window.__turnstileRenderCalls[window.__turnstileRenderCalls.length-1].id")
        assert new_widget_id != widget_id

        # Invoking old callback from old widget must NOT unlock join (generation check)
        page.evaluate("window.__oldCallback('stale-token')")
        assert page.locator('#lot-join').is_disabled()
        assert '请先完成人机验证' in page.locator('#lot-join').inner_text()

        # Simulate timeout callback on new widget
        page.evaluate(f"window.__turnstileWidgets.get('{new_widget_id}').options['timeout-callback']()")
        assert page.locator('#lot-join').is_disabled()
        assert '验证超时，请重新验证' in page.locator('#lot-captcha-area').inner_text()
        assert page.locator('#lot-captcha-reissue').is_visible()

        # Click reissue again
        page.locator('#lot-captcha-reissue').click()
        page.locator('.mock-cf-turnstile').wait_for()
        widget_id = page.evaluate("window.__turnstileRenderCalls[window.__turnstileRenderCalls.length-1].id")

        # -------------------------------------------------------------
        # 4. Trigger Success callback -> unlocks join button
        # -------------------------------------------------------------
        # Find the active challenge matching the current widget render cData
        current_cdata = page.evaluate("window.__turnstileRenderCalls[window.__turnstileRenderCalls.length-1].options.cData")
        active_ch = app.state.db.one(
            "SELECT id, cdata FROM lottery_captcha_challenges WHERE lottery_id=? AND tg_id=? AND cdata=? AND state='issued'",
            (lid, str(ADMIN), current_cdata)
        )
        assert active_ch is not None
        # Use official lottery_captcha_helpers.answer to bind valid token in mock provider
        token_info = answer(captcha_state, {'challenge_id': active_ch['id'], 'cdata': active_ch['cdata']})

        page.evaluate(f"window.__turnstileWidgets.get('{widget_id}').options.callback('{token_info['captcha_token']}')")
        page.wait_for_function("!document.querySelector('#lot-join').disabled")
        assert page.locator('#lot-join').inner_text() == '立即参与'

        # -------------------------------------------------------------
        # 5. Admin joins with token -> verify backend consumption and DB entry
        # -------------------------------------------------------------
        page.locator('#lot-join').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()
        assert app.state.db.one('SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lid, str(ADMIN))) is not None

        # Admin is now joined: refresh/re-enter must NOT show captcha widget
        page.goto(url)
        page.locator('#lot-join').wait_for()
        assert page.locator('#lot-join').inner_text() == '已报名 · 等待开奖'
        assert not page.locator('#lot-captcha-area').count()

        # -------------------------------------------------------------
        # 6. Switching views / activities properly cleans up active widget
        # -------------------------------------------------------------
        page.locator('#lot-to-manage').click()
        page.locator('.lot-role-bar').wait_for()
        assert not page.locator('#lot-captcha-area').count()

        # -------------------------------------------------------------
        # 7. Unverified Guest (needs private start first): captcha must NOT display before private start!
        # -------------------------------------------------------------
        bot_id = env.service.bot._token().split(':', 1)[0]
        app.state.db.execute('DELETE FROM meta WHERE key=?', (f'telegram.private:{bot_id}:{GUEST}',))
        viewer['id'] = GUEST
        page.goto(url)
        page.locator('#lot-start-bot').wait_for()
        # Captcha must NOT be rendered when private start is blocked
        assert not page.locator('#lot-captcha-area').count()

        # Guest completes private start in Telegram
        private_start(env.service, GUEST)
        page.locator('#lot-retry-private').click()

        # Now private start satisfied -> Turnstile widget appears
        page.locator('.mock-cf-turnstile').wait_for()
        assert page.locator('#lot-join').is_disabled()
        assert '请先完成人机验证' in page.locator('#lot-join').inner_text()

        # Guest answers challenge and joins
        guest_widget_id = page.evaluate("window.__turnstileRenderCalls[window.__turnstileRenderCalls.length-1].id")
        ch_guest = app.state.db.one(
            "SELECT id, cdata FROM lottery_captcha_challenges WHERE lottery_id=? AND tg_id=? AND state='issued' ORDER BY created_at DESC",
            (lid, str(GUEST))
        )
        assert ch_guest is not None
        guest_token_info = answer(captcha_state, {'challenge_id': ch_guest['id'], 'cdata': ch_guest['cdata']})

        page.evaluate(f"window.__turnstileWidgets.get('{guest_widget_id}').options.callback('{guest_token_info['captcha_token']}')")
        page.wait_for_function("!document.querySelector('#lot-join').disabled")
        page.locator('#lot-join').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()

        # -------------------------------------------------------------
        # 8. Paid lottery with Turnstile: modal confirmation cancel does NOT consume token or debit
        # -------------------------------------------------------------
        paid_lid = create(env, config(title='收费转盘', draw_mode='manual', entry_fee=30, audience='member',
                                     prizes=[{'kind': 'points', 'name': '300 积分', 'quantity': 1, 'amount': 300}]))
        paid_url = 'https://app.example.test/app?tgWebAppStartParam=lottery_' + paid_lid

        # Member has 100 points
        app.state.points.add('u2', 100, 'admin.mint', 'test-mint')
        viewer['id'] = MEMBER

        page.goto(paid_url)
        page.locator('.mock-cf-turnstile').wait_for()
        assert page.locator('#lot-join').is_disabled()

        # Member completes captcha
        member_widget_id = page.evaluate("window.__turnstileRenderCalls[window.__turnstileRenderCalls.length-1].id")
        ch_member = app.state.db.one(
            "SELECT id, cdata FROM lottery_captcha_challenges WHERE lottery_id=? AND tg_id=? AND state='issued' ORDER BY created_at DESC",
            (paid_lid, str(MEMBER))
        )
        assert ch_member is not None
        member_token_info = answer(captcha_state, {'challenge_id': ch_member['id'], 'cdata': ch_member['cdata']})

        page.evaluate(f"window.__turnstileWidgets.get('{member_widget_id}').options.callback('{member_token_info['captcha_token']}')")
        page.wait_for_function("!document.querySelector('#lot-join').disabled")
        assert page.locator('#lot-join').inner_text() == '支付 30 积分报名'

        # Click payment button -> confirmation bottom sheet opens
        page.locator('#lot-join').click()
        page.locator('#lot-ok').wait_for()
        assert '30 积分' in page.locator('.lot-sheet-card').inner_text()

        # Click Cancel in modal -> NO debit, NO join
        page.locator('#lot-cancel').click()
        page.wait_for_function("document.querySelector('#lot-ok') === null")
        assert app.state.points.balance('u2') == 100
        assert not app.state.db.one('SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (paid_lid, str(MEMBER)))

        # Confirm payment now -> joins and debits
        page.locator('#lot-join').click()
        page.locator('#lot-ok').click()
        page.locator('.ticket strong', has_text='报名已记录').wait_for()
        assert app.state.points.balance('u2') == 70

        # -------------------------------------------------------------
        # 9. Narrow screen 320px vs 390px viewport, compact vs flexible sizing, light & dark theme
        # -------------------------------------------------------------
        test_lid = create(env, config(title='响应式与主题测试', draw_mode='manual',
                                     prizes=[{'kind': 'manual', 'name': '全员纪念徽章', 'quantity': 1, 'instructions': '联系客服'}]))
        test_url = 'https://app.example.test/app?tgWebAppStartParam=lottery_' + test_lid

        # Test on 320px viewport in dark mode
        page.set_viewport_size({'width': 320, 'height': 640})
        page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        viewer['id'] = GUEST
        private_start(env.service, GUEST)
        page.goto(test_url)
        page.locator('.mock-cf-turnstile').wait_for()

        last_render = page.evaluate("window.__turnstileRenderCalls[window.__turnstileRenderCalls.length-1].options")
        assert last_render['size'] == 'compact'
        assert last_render['theme'] == 'dark'
        iframe_style = page.locator('.mock-cf-turnstile iframe').get_attribute('style')
        assert '130px' in iframe_style

        # Test on 390px viewport in light mode
        page.set_viewport_size({'width': 390, 'height': 844})
        tg_theme['colorScheme'] = 'light'
        page.goto(test_url)
        page.locator('.mock-cf-turnstile').wait_for()

        last_render_390 = page.evaluate("window.__turnstileRenderCalls[window.__turnstileRenderCalls.length-1].options")
        assert last_render_390['size'] == 'flexible'
        assert last_render_390['theme'] == 'light'
        iframe_style_390 = page.locator('.mock-cf-turnstile iframe').get_attribute('style')
        assert '300px' in iframe_style_390

        # Verify no horizontal scroll
        assert not page.evaluate("""() => {
          const screen = document.querySelector('#screen') || document.body;
          return screen.scrollWidth > screen.clientWidth;
        }""")

        # Optional single screenshot
        page.screenshot(path=str(artifacts_dir / 'turnstile-verified.png'), full_page=True)

        browser.close()
