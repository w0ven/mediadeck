"""Shipped lottery forms must stay dark within either Telegram member theme."""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.modules.economy_rules import BEIJING
from tests.test_lottery import ADMIN, config, create
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import TELEGRAM_STUB, headers


def _styles(page):
    return page.evaluate("""() => {
      const style = (e, pseudo) => { const s=getComputedStyle(e,pseudo); return {
        background:s.backgroundColor, color:s.color, padding:s.padding,
        border:s.borderTopWidth, scheme:s.colorScheme, opacity:s.opacity,
        outline:s.outlineStyle, shadow:s.boxShadow}; };
      return {wrappers:[...document.querySelectorAll('.lottery .field,.lottery .lot-field')].map(e=>style(e)),
        controls:[...document.querySelectorAll('.lottery input:not([type=checkbox]),.lottery select,.lottery textarea')].map(e=>({
          ...style(e),placeholder:style(e,'::placeholder').color, id:e.id, labels:e.labels.length})),
        labels:[...document.querySelectorAll('.lottery label')].map(e=>style(e)),
        scheme:getComputedStyle(document.querySelector('.lottery')).colorScheme};
    }""")


def _rgb(value):
    return [float(n.strip()) for n in value[value.index('(') + 1:value.index(')')].split(',')][:3]


def _contrast(a, b):
    def luminance(value):
        components = [v / 255 for v in _rgb(value)]
        return sum(weight * (v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4)
                   for weight, v in zip((.2126, .7152, .0722), components))
    x, y = sorted((luminance(a), luminance(b)))
    return (y + .05) / (x + .05)


def _assert_forms(page):
    page.locator('.lottery .lot-field,.lottery .field').first.wait_for()
    data = _styles(page)
    assert data['wrappers'], data
    assert all(w['background'] == 'rgba(0, 0, 0, 0)' and w['padding'] == '0px' and w['border'] == '0px'
               for w in data['wrappers']), data
    # In the redesigned Mini App, .lottery follows the member panel theme variables.
    # The native form controls follow the active theme (light in light, dark in dark).
    assert data['scheme'] in ('dark', 'light', 'normal'), data
    theme = page.locator('html').get_attribute('data-theme')
    for control in data['controls']:
        assert control['scheme'] in ('dark', 'light') and control['labels'] >= 1, control
        if theme == 'light':
            assert max(_rgb(control['background'])) > 200, control
        else:
            assert max(_rgb(control['background'])) < 60, control
        assert _contrast(control['color'], control['background']) >= 4.5, control
        assert _contrast(control['placeholder'], control['background']) >= 4.5, control
    # Form labels must have >= 4.5 contrast against their text color context
    for label in data['labels']:
        assert label['color'] in ('rgb(20, 24, 38)', 'rgb(243, 245, 250)'), label

    assert page.locator('.lot-form-actions').evaluate('(e)=>getComputedStyle(e).position') == 'static'
    assert page.locator('input[type=checkbox]').first.evaluate("""e=>{
      const s=getComputedStyle(e);return (s.colorScheme==='dark'||s.colorScheme==='light') && s.appearance==='auto' && s.padding==='0px' && e.labels.length>0;
    }""")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.evaluate("document.querySelector('#screen').scrollWidth <= document.querySelector('#screen').clientWidth")
    assert not page.locator('.lottery .field').count()
    assert '\ufffd' not in page.locator('.lottery').inner_text()


def _visible_uncovered(page, selector):
    element = page.locator(selector)
    element.scroll_into_view_if_needed()
    element.focus()
    assert element.evaluate("""el => { const r=el.getBoundingClientRect();
      const top=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      return r.top>=0 && r.bottom<=innerHeight && (el===top || el.contains(top)); }"""), selector


@pytest.mark.parametrize('theme,width,height', [
    ('light', 390, 844), ('dark', 390, 844), ('light', 320, 640),
    ('light', 420, 700), ('dark', 320, 480),
])
def test_lottery_form_themes(request, tmp_path, theme, width, height):
    env = request.getfixturevalue('lottery_env')
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    output = Path(os.getenv('LOTTERY_THEME_ARTIFACTS', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
        page.set_default_timeout(8000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def bridge(route):
            r, u = route.request, urlsplit(route.request.url)
            if u.hostname == 'telegram.org':
                stub = (TELEGRAM_STUB % json.dumps(headers(ADMIN)['X-Telegram-Init-Data'])).replace(
                    "colorScheme: 'dark'", f"colorScheme: '{theme}'")
                return route.fulfill(content_type='application/javascript', body=stub)
            if u.hostname != 'app.example.test':
                return route.abort()
            res = env.client.request(r.method, u.path + ('?' + u.query if u.query else ''),
                                     content=r.post_data_buffer, auth=None,
                                     headers={**{k: v for k, v in r.headers.items()
                                                 if k.lower() in ('content-type', 'x-telegram-init-data')},
                                              'CF-Connecting-IP': env.ip_for(ADMIN)})
            route.fulfill(status=res.status_code, body=res.content,
                          headers={k: v for k, v in res.headers.items() if k not in ('content-length', 'content-encoding')})
        page.route('**/*', bridge)
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').wait_for()
        member_background = page.locator('.list').first.evaluate('(e)=>getComputedStyle(e).backgroundColor')
        if theme == 'light':
            assert member_background == 'rgba(255, 255, 255, 0.85)'
            assert page.locator('body').evaluate('(e)=>getComputedStyle(e).color') == 'rgb(20, 24, 38)'
        page.locator('[data-go="lottery"]').click()
        page.locator('#lot-new').click()
        page.locator('#lot-title').wait_for()
        # Before changing product CSS, this records and fails on the real light-theme regression.
        print('FORM_COMPUTED', theme, width, height, json.dumps(_styles(page)))
        _assert_forms(page)
        assert page.locator('html').get_attribute('data-theme') == theme
        assert page.locator('#lot-anti-multi').is_checked()
        assert page.locator('#lot-chat').input_value() == config()['chat_id']
        page.screenshot(path=str(output / f'create-{theme}-{width}.png'), full_page=True)
        page.locator('#lot-title').fill('主题回归抽奖')
        page.locator('#lot-date').fill(datetime.fromtimestamp(config()['deadline'], BEIJING).strftime('%Y-%m-%dT%H:%M'))
        page.locator('#lot-title').focus()
        focused = page.locator('#lot-title').evaluate('(e)=>getComputedStyle(e).borderColor')
        if theme == 'light':
            assert focused == 'rgb(88, 101, 242)'
        else:
            assert focused == 'rgb(123, 140, 255)'
        page.locator('#lot-thread').evaluate('(e)=>e.disabled=true')
        _assert_forms(page)
        page.locator('#lot-thread').evaluate('(e)=>e.disabled=false')
        # Every native reward control (select, number, text, textarea) shares the isolation.
        for kind in ('shop', 'points', 'invite', 'registration', 'manual'):
            page.locator('#lot-add').click()
            page.locator('#lot-kind').select_option(kind)
            _assert_forms(page)
            if kind == 'manual':
                page.locator('#lot-prize-name').fill('人工奖品')
                page.locator('#lot-instructions').fill('仅中奖者可见的说明')
                page.locator('#lot-prize-editor').evaluate("e=>e.scrollIntoView({block:'start'})")
                page.screenshot(path=str(output / f'prize-{theme}-{width}.png'), full_page=True)
                # Telegram retains stable height while its current viewport shrinks with the keyboard.
                page.evaluate("h=>document.documentElement.style.setProperty('--tg-viewport-stable-height',h+'px')", height)
                page.set_viewport_size({'width': width, 'height': 360})
                _visible_uncovered(page, '#lot-instructions')
                _assert_forms(page)
                page.screenshot(path=str(output / f'prize-keyboard-{theme}-{width}.png'), full_page=True)
                _visible_uncovered(page, '#lot-prize-ok')
            page.locator('#lot-prize-ok').click()
            page.set_viewport_size({'width': width, 'height': height})
            page.evaluate("document.documentElement.style.removeProperty('--tg-viewport-stable-height')")
        assert page.locator('.prize-row').count() == 5
        _visible_uncovered(page, '#lot-save')
        assert page.locator('.lot-form-actions').evaluate('(e)=>e.getBoundingClientRect().top >= e.previousElementSibling.getBoundingClientRect().bottom')
        page.screenshot(path=str(output / f'create-actions-{theme}-{width}.png'), full_page=True)
        page.locator('#lot-save').click()
        page.locator('#lot-edit').click()
        _assert_forms(page)
        assert page.locator('#lot-title').input_value() == '主题回归抽奖'
        assert page.locator('.prize-row').count() == 5
        page.locator('#lot-title').fill('修改后的抽奖')
        page.locator('#lot-save').click()
        page.locator('#lot-edit').click()
        assert page.locator('#lot-title').input_value() == '修改后的抽奖'
        page.locator('#lot-cancel').click()
        page.locator('#lot-edit').wait_for()
        # Recover known failed mock notices: exercise the same form styling and actual action.
        lid = create(env, config(draw_mode='manual'))
        env.service.db.execute("UPDATE lottery_notices SET state='failed',error='Mock delivery failure' WHERE lottery_id=?", (lid,))
        page.goto('https://app.example.test/app?tgWebAppStartParam=lottery_' + lid)
        page.locator('#lot-to-manage').click()
        page.locator('[data-lot-recover]').click()
        _assert_forms(page)
        page.set_viewport_size({'width': width, 'height': 360})
        _visible_uncovered(page, '#lot-mid')
        page.locator('#lot-mid').fill('123')
        _visible_uncovered(page, '#lot-absent')
        page.locator('#lot-absent').focus()
        page.keyboard.press('Space')
        assert page.locator('#lot-absent').is_checked()
        page.locator('#lot-absent').uncheck()
        page.screenshot(path=str(output / f'recover-{theme}-{width}.png'), full_page=True)
        _visible_uncovered(page, '#lot-recover-ok')
        page.locator('#lot-recover-ok').click()
        page.locator('#lot-draw').wait_for()
        assert env.service.db.one('SELECT state FROM lottery_notices WHERE lottery_id=?', (lid,))['state'] == 'sent'
        # The dark component never writes the member theme or its shared input styling.
        page.goto('https://app.example.test/app')
        page.locator('[data-go="lottery"]').wait_for()
        assert page.locator('html').get_attribute('data-theme') == theme
        assert page.locator('.list').first.evaluate('(e)=>getComputedStyle(e).backgroundColor') == member_background
        page.locator('[data-go="password"]').click()
        page.locator('[data-mode="custom"]').click()
        assert page.locator('#pw1').evaluate('(e)=>getComputedStyle(e).backgroundColor') == member_background
        assert page.locator('#pw1').evaluate('(e)=>getComputedStyle(e).padding') == '10px 12px'
        assert not errors
        browser.close()
