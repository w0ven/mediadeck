"""Registration settings and optional card links through the actual web UI."""
from playwright.sync_api import expect
from test_audit_web_browser import browser, page, server  # noqa: F401


def test_optional_card_links_copy_and_export_are_visible(page):  # noqa: F811 - shared pytest fixture
    code = 'DEMOABCDEFGHJKLMNPQR'
    link = 'https://t.me/MediaDeckDemoBot?start=' + code
    requests = []

    def generated(route):
        requests.append(route.request.post_data_json)
        route.fulfill(json={'codes': [{'code': code, 'link': link, 'batch': 'demo'}], 'count': 1})

    page.route('**/api/redeem/generate', generated)
    page.evaluate("go('redeem')")
    expect(page.locator('#rd-links')).to_be_visible()
    assert not page.locator('#rd-links').is_checked()
    page.locator('#rd-count').fill('1')
    page.locator('#rd-links').check()
    page.locator('#rd-make').click()
    expect(page.locator('#rd-dump')).to_contain_text(link)
    assert requests and requests[-1]['include_links'] is True
    page.evaluate("""() => {
        navigator.clipboard.writeText = async value => { window.copiedCards = value; };
        window.open = url => { window.exportCardsUrl = url; };
    }""")
    page.locator('#rd-copy-links').click()
    page.wait_for_function('window.copiedCards !== undefined')
    assert page.evaluate('window.copiedCards') == link
    page.locator('#rd-dl').click()
    assert page.evaluate('window.exportCardsUrl') == '/api/redeem/export.csv?batch=demo&include_links=true'


def test_registration_notice_group_and_topic_save_from_settings(page, server):  # noqa: F811
    response = page.request.post(server + '/api/settings/telegram', data={
        'group_interaction_chats': ['-100700'],
        'registration_notify_chat_id': '', 'registration_notify_thread_id': None})
    assert response.ok
    page.evaluate("go('tgbot')")
    page.locator('[data-config-section="registration"]').click()
    expect(page.locator('#tg-registration-notify')).to_be_visible()
    page.locator('#tg-registration-notify').select_option('-100700')
    page.locator('#tg-registration-topic').fill('77')
    with page.expect_response(lambda response: response.url.endswith('/api/settings/telegram')
                              and response.request.method == 'POST') as saved:
        page.locator('#tg-save2').click()
    assert saved.value.ok
    result = saved.value.json()
    assert result['registration_notify_chat_id'] == '-100700'
    assert result['registration_notify_thread_id'] == 77
