"""Playback-line pointer/keyboard ordering against isolated API + real Chromium."""
from playwright.sync_api import expect
from test_audit_web_browser import browser, hold, page, server  # noqa: F401

LINES = [
    {'label': 'Line A', 'url': 'https://a.example.test', 'hint': 'first'},
    {'label': 'Line B', 'url': 'https://b.example.test', 'hint': 'second',
     'whitelist_only': True},
    {'label': 'Line C', 'url': 'https://c.example.test', 'hint': 'third'},
]


def open_lines(page, server, lines=None):  # noqa: F811 - shared fixtures
    response = page.request.post(server + '/api/settings/telegram', data={
        'playback_lines': LINES if lines is None else lines,
        'playback_lines_note': 'Keep this note', 'playback_lines_show_load': False,
        'playback_routing_rules': 'DOMAIN,example.test,DIRECT',
    })
    assert response.ok
    page.set_viewport_size({'width': 1440, 'height': 1500})
    page.evaluate("go('tgbot?section=lines')")
    expect(page.locator('.line-drag-handle').first).to_be_visible()
    page.wait_for_function('state.pageReady')
    assert not page.evaluate('configDirty()')


def labels(page):  # noqa: F811
    return [row['label'] for row in page.locator('#tg-lines').evaluate('el=>JSON.parse(el.value)')]


def drag(page, source, destination, cancel=None):  # noqa: F811
    handle = page.locator('.line-drag-handle').nth(source).bounding_box()
    row = page.locator('.line-row').nth(destination).bounding_box()
    x = handle['x'] + handle['width'] / 2
    y = row['y'] + (row['height'] - 12 if destination > source else 12)
    page.mouse.move(x, handle['y'] + handle['height'] / 2)
    page.mouse.down()
    page.mouse.move(x, y, steps=12)
    if cancel == 'escape':
        page.keyboard.press('Escape')
    elif cancel == 'outside':
        page.mouse.move(2, y)
    page.mouse.up()


def test_drag_preserves_edits_preview_failed_save_and_reload(page, server):  # noqa: F811
    open_lines(page, server)
    writes = []
    page.on('request', lambda r: writes.append(r.post_data_json)
            if r.method == 'POST' and r.url.endswith('/api/settings/telegram') else None)
    page.locator('#tg-line-label-0').fill('Edited A')
    drag(page, 0, 2)
    assert labels(page) == ['Line B', 'Line C', 'Edited A']
    assert page.locator('#tg-line-preview strong').all_text_contents() == labels(page)
    assert page.locator('#tg-line-whitelist-0').is_checked()
    expect(page.locator('#tg-line-hint-0')).to_have_value('second')
    expect(page.locator('#tg-line-url-2')).to_have_value('https://a.example.test')
    assert page.evaluate('configDirty()')
    assert not writes
    page.route('**/api/settings/telegram', lambda r: r.fulfill(
        status=503, json={'detail': 'simulated failure'}) if r.request.method == 'POST'
        else r.continue_())
    page.locator('#tg-save-lines').click()
    expect(page.locator('.config-section:visible .save-feedback')).to_contain_text('保存失败')
    assert labels(page) == ['Line B', 'Line C', 'Edited A']
    assert page.evaluate('configDirty()')
    page.unroute('**/api/settings/telegram')
    page.locator('#tg-save-lines').click()
    page.wait_for_function('!configDirty()')
    saved = page.request.get(server + '/api/settings/telegram').json()
    assert [x['label'] for x in saved['playback_lines']] == labels(page)
    assert saved['playback_lines'][0] == LINES[1]
    assert saved['playback_lines_note'] == 'Keep this note'
    assert saved['playback_lines_show_load'] is False
    assert saved['playback_routing_rules'] == 'DOMAIN,example.test,DIRECT'
    page.reload()
    expect(page.locator('#tg-line-label-0')).to_have_value('Line B')
    assert labels(page) == ['Line B', 'Line C', 'Edited A']


def test_keyboard_reorder_add_remove_and_single_line(page, server):  # noqa: F811
    open_lines(page, server)
    page.locator('.line-drag-handle').nth(2).press('Home')
    assert labels(page) == ['Line C', 'Line A', 'Line B']
    expect(page.locator('.line-drag-handle').nth(0)).to_be_focused()
    page.keyboard.press('ArrowDown')
    assert labels(page) == ['Line A', 'Line C', 'Line B']
    page.keyboard.press('End')
    assert labels(page) == ['Line A', 'Line B', 'Line C']
    assert not page.evaluate('configDirty()')
    page.locator('.line-remove').nth(1).click()
    assert labels(page) == ['Line A', 'Line C']
    page.locator('#tg-line-add').click()
    page.locator('#tg-line-label-2').fill('New D')
    page.locator('#tg-line-url-2').fill('https://d.example.test')
    page.locator('.line-drag-handle').nth(2).press('Home')
    assert labels(page) == ['New D', 'Line A', 'Line C']
    page.locator('.line-remove').nth(2).click()
    page.locator('.line-remove').nth(1).click()
    expect(page.locator('.line-drag-handle')).to_be_disabled()
    assert labels(page) == ['New D']


def test_cancel_and_saving_do_not_reorder(page, server):  # noqa: F811
    open_lines(page, server)
    original = labels(page)
    drag(page, 0, 2, cancel='escape')
    assert labels(page) == original
    assert not page.evaluate('configDirty()')
    drag(page, 0, 2, cancel='outside')
    assert labels(page) == original
    assert not page.evaluate('configDirty()')
    page.locator('#tg-line-label-0').fill('Saving A')
    hold(page, '/api/settings/telegram', 'POST')
    page.locator('#tg-save-lines').click()
    page.wait_for_function('auditWaiting')
    page.locator('.line-drag-handle').first.press('End')
    assert labels(page) == ['Saving A', 'Line B', 'Line C']
    page.evaluate('auditRelease()')
    page.wait_for_function('!configDirty()')
    expect(page.locator('.line-dragging,.line-drop-before,.line-drop-after')).to_have_count(0)


def test_touch_reorders_without_horizontal_overflow(page, server):  # noqa: F811
    open_lines(page, server, LINES[:2])
    page.set_viewport_size({'width': 390, 'height': 1000})
    page.locator('.line-row').first.scroll_into_view_if_needed()
    client = page.context.new_cdp_session(page)
    client.send('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 1})
    page.evaluate("window.scrollBy(0, document.querySelector('.line-row').getBoundingClientRect().top - 100)")
    source = page.locator('.line-drag-handle').nth(1).bounding_box()
    assert source['height'] >= 44 and source['y'] + source['height'] < 1000
    target = page.locator('.line-row').first.bounding_box()
    x = source['x'] + source['width'] / 2
    y = source['y'] + source['height'] / 2
    end_y = target['y'] + 12
    client.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y}]})
    for step in range(1, 11):
        client.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [
            {'x': x, 'y': y + (end_y - y) * step / 10}]})
    client.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    assert labels(page) == ['Line B', 'Line A']
    assert page.locator('#tg-line-whitelist-0').is_checked()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert page.evaluate('configDirty()')
    client.detach()


def test_drag_auto_scrolls_long_list(page, server):  # noqa: F811
    many = [{'label': f'Line {i}', 'url': f'https://n{i}.example.test'} for i in range(8)]
    open_lines(page, server, many)
    page.set_viewport_size({'width': 1440, 'height': 800})
    handle = page.locator('.line-drag-handle').first
    handle.scroll_into_view_if_needed()
    box = handle.bounding_box()
    x = box['x'] + box['width'] / 2
    start = page.evaluate('scrollY')
    page.mouse.move(x, box['y'] + box['height'] / 2)
    page.mouse.down()
    page.mouse.move(x, 780, steps=10)
    page.wait_for_function('''(start)=>scrollY>start+400 &&
        [...document.querySelectorAll('.line-row')].findIndex(el=>el.classList.contains('line-drop-before'))>=3''', arg=start)
    page.mouse.up()
    assert labels(page).index('Line 0') >= 2
    expect(page.locator('.line-dragging,.line-drop-before,.line-drop-after')).to_have_count(0)
