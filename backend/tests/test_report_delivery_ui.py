"""Exercise the actual automation card in Chromium with a mocked panel API."""
import shutil
from pathlib import Path

import pytest


def test_report_card_retry_preserves_drafts_and_displays_receipts():
    playwright = pytest.importorskip('playwright.sync_api')
    chromium = shutil.which('chromium')
    if not chromium:
        pytest.skip('system Chromium unavailable')
    source = (Path(__file__).parents[1] / 'app/static/ops.js').read_text()
    card = {'id': 'viewing_report', 'name': '观影报告', 'description': 'Example',
            'hour': 0, 'running': False, 'enabled': True,
            'fields': [{'key': 'hour', 'label': '时间', 'kind': 'int', 'default': 0}],
            'config': {'hour': 0},
            'last_run': {'ok': False, 'started_at': 1, 'duration_ms': 1,
                         'summary': {'结果': '部分成功', '已送达': 1}},
            'delivery': {'batch': 'example-batch', 'retryable': True,
                         'summary': {'已送达': 1}, 'recipients': [
                             {'username': '<img src=x onerror=alert(1)>', 'state': 'failed',
                              'attempts': 1, 'reason': '用户已屏蔽机器人'}]}}
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium, headless=True,
                                    args=['--no-sandbox'])
        page = browser.new_page()
        page.set_content('<div id="view"></div>')
        page.add_script_tag(content="""
            const PAGES = {}, state = {};
            const $ = s => document.querySelector(s);
            const esc = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
            const fmtAgeTs = s => String(s), fmtAge = s => String(s);
            const configFeedback = (el, s) => { el.dataset.feedback = s; };
            const toast = () => {};
            let confirmed = false, writes = [], gets = 0;
            const deckConfirm = async () => confirmed;
        """)
        page.add_script_tag(content=source)
        page.evaluate('c => { window.card = c; $("#view").innerHTML = pluginCard(c); bindPluginCard(c); }', card)
        page.evaluate("""() => {
            window.api = async (url, options) => {
                if (options?.method === 'POST') {
                    writes.push({url, body:JSON.parse(options.body)});
                    return {ok:true, card:{...card, running:true}};
                }
                gets++;
                return {...card, running:false};
            };
        }""")
        page.fill('#pl-viewing_report-hour', '7')
        page.locator('[data-act="retry-failed"]').click()
        assert page.evaluate('writes.length') == 0
        page.evaluate('confirmed = true')
        page.locator('[data-act="retry-failed"]').click()
        page.wait_for_function('gets >= 1')
        assert page.evaluate('writes') == [
            {'url': '/api/plugins/viewing_report/retry-failed', 'body': {'batch': 'example-batch'}}]
        assert page.input_value('#pl-viewing_report-hour') == '7'
        assert page.locator('.plugin-result').inner_text().startswith('部分成功')
        assert page.locator('.plugin-delivery img').count() == 0
        page.locator('.plugin-delivery summary').click()
        assert '用户已屏蔽机器人' in page.locator('.plugin-delivery').inner_text()
        page.evaluate('card.delivery = {legacy:true,sent:3,note:"旧批次不自动补发"}; $("#view").innerHTML=pluginCard(card); bindPluginCard(card);')
        assert page.locator('[data-act="retry-failed"]').is_disabled()
        assert '旧批次不自动补发' in page.locator('.plugin-delivery').inner_text()
        browser.close()
