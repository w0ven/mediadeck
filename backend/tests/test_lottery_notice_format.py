"""Native lottery emoji metadata and safe delivery; Telegram is fully mocked."""
import asyncio
import copy
import json
import re
import time

import pytest

from app.modules.lottery import EMOJI, LETTERS, normalize_notice_payload, notice_rejection
from app.modules.report_delivery import CALL_DELIVERY, failure
from app.modules.telegram import _CALL_ERROR
from tests.test_lottery import ADMIN, config, create, req
from tests.test_lottery import env as lottery_env  # noqa: F401

# Public metadata transcribed from the supplied original sticker manifest.
# Pack index identifies artwork, while `emoji` is the valid Telegram fallback.
PACK_METADATA = {
    '5364273836122463188': ('ColorfulFontEmoji', 11, '🔡'),
    '5368486821082715433': ('ColorfulFontEmoji', 20, '🔡'),
    '5364012727880668255': ('ColorfulFontEmoji', 2, '🔡'),
    '5364258799441957434': ('ColorfulFontEmoji', 10, '🔡'),
    '5381971708427132282': ('ColorfulFontEmoji', 24, '🔡'),
    '5235826903490764103': ('IslomjonAnimeEmoji', 136, '🎁'),
    '5830101874467938532': ('ZaiHuaNews', 42, '⭐️'),
    '5197304993920616826': ('IslomjonAnimeEmoji', 160, '📣'),
    '5350486389806868244': ('IslomjonAnimeEmoji', 1, '✅'),
}


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def notice(e, lid):
    return e.service.db.one('SELECT * FROM lottery_notices WHERE lottery_id=? AND kind=?', (lid, 'launch'))


def sends(e):
    return [(m, p) for m, p in e.calls if m in ('sendMessage', 'editMessageText')]


def old_payload(payload):
    result = copy.deepcopy(payload)
    for (eid, fallback), old in zip(LETTERS, 'LUCKY'):
        result['text'] = result['text'].replace(f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>',
                                                f'<tg-emoji emoji-id="{eid}">{old}</tg-emoji>')
    eid, fallback = EMOJI['star']
    result['text'] = result['text'].replace(f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>',
                                           f'<tg-emoji emoji-id="{eid}">⭐</tg-emoji>')
    return result


def test_native_fallbacks_match_original_sticker_metadata(e):
    assert dict(LETTERS + list(EMOJI.values())) == {eid: r[2] for eid, r in PACK_METADATA.items()}
    lid = create(e, config(title='公告 <b>不是 HTML</b> LUCKY'))
    launch = sends(e)[0][1]
    result = e.service.notice_payload(e.service.row(lid), 'result', [])
    for payload in (launch, result):
        tags = re.findall(r'<tg-emoji emoji-id="([0-9]+)">(.*?)</tg-emoji>', payload['text'])
        assert tags[:5] == LETTERS
        assert all(PACK_METADATA[eid][2] == alternative for eid, alternative in tags)
        assert '<img' not in payload['text'] and '&lt;b&gt;不是 HTML&lt;/b&gt;' in payload['text']
        button = payload['reply_markup']['inline_keyboard'][0][0]
        assert button['icon_custom_emoji_id'] in PACK_METADATA
        assert button['url'].endswith('startapp=lottery_' + lid)
    assert notice(e, lid)['state'] == 'sent' and len(sends(e)) == 1


@pytest.mark.parametrize('state,message_id', [('failed', None), ('unknown', 321), ('pending', None)])
def test_saved_legacy_payload_repaired_only_on_authorized_send(e, state, message_id):
    lid = create(e, config(title='冻结标题 LUCKY & <test>'))
    n = notice(e, lid)
    canonical = json.loads(n['payload_json'])
    legacy = old_payload(canonical)
    e.service.db.execute('UPDATE lottery_notices SET state=?,message_id=?,payload_json=? WHERE id=?',
                         (state, message_id, json.dumps(legacy, ensure_ascii=False), n['id']))
    before, sent_before = e.service.row(lid), len(sends(e))
    if state != 'pending':
        asyncio.run(e.service.deliver_notices(lid))
        assert len(sends(e)) == sent_before
        assert notice(e, lid)['state'] == state and json.loads(notice(e, lid)['payload_json']) == legacy
        response = req(e, 'POST', f'/{lid}/notices/{n["id"]}/recover', json={'message_id': message_id})
        assert response.status_code == 200
    else:
        asyncio.run(e.service.deliver_notices(lid))
    assert notice(e, lid)['state'] == 'sent' and len(sends(e)) == sent_before + 1
    method, actual = sends(e)[-1]
    expected = copy.deepcopy(canonical)
    if message_id:
        expected['message_id'] = message_id
        expected.pop('message_thread_id', None)
    assert method == ('editMessageText' if message_id else 'sendMessage') and actual == expected
    assert json.loads(notice(e, lid)['payload_json']) == canonical
    assert e.service.row(lid) == before and not e.service.db.query('SELECT * FROM lottery_awards')


def test_normalization_is_exact_local_and_idempotent():
    eid = LETTERS[0][0]
    unrelated = (f' LUCKY &lt;tg-emoji emoji-id="{eid}"&gt;L&lt;/tg-emoji&gt; '
                 '<tg-emoji emoji-id="999">L</tg-emoji> '
                 f'<tg-emoji data-x="a" emoji-id="{eid}">L</tg-emoji> '
                 f'<tg-emoji emoji-id="{eid}">external text</tg-emoji>')
    payload = {'text': f'<tg-emoji emoji-id="{eid}">L</tg-emoji>' + unrelated,
               'chat_id': '-10000000001', 'message_thread_id': 5,
               'reply_markup': {'inline_keyboard': [[{'text': 'LUCKY', 'url': 'https://example.test/'}]]}}
    before = copy.deepcopy(payload)
    normalized = normalize_notice_payload(payload)
    assert payload == before
    assert normalized['text'] == f'<tg-emoji emoji-id="{eid}">🔡</tg-emoji>' + unrelated
    assert {k: v for k, v in normalized.items() if k != 'text'} == {k: v for k, v in payload.items() if k != 'text'}
    assert normalize_notice_payload(normalized) == normalized


@pytest.mark.parametrize('description,expected', [
    ('Bad Request: ENTITY_TEXT_INVALID', '实体替代文本（ENTITY_TEXT_INVALID）'),
    ('Bad Request: can\'t parse entities: Unsupported start tag', 'HTML 实体格式'),
    ('Bad Request: custom emoji entity alternative must be a valid emoji', '表情替代文本或实体格式'),
    ('Bad Request: BUTTON_ICON_DOCUMENT_INVALID', '按钮自定义图标'),
    ('Bad Request: BUTTON_URL_INVALID', '按钮链接'),
    ('Bad Request: message is too long', '正文过长'),
    ('Bad Request: BUTTON_TYPE_INVALID', '按钮结构'),
    ('Bad Request: unrecognized problem', '未识别的 400'),
])
def test_specific_400_diagnostics_no_generic_retry_or_sensitive_echo(e, description, expected):
    secret = 'PRIVATE-CODE mock-token https://api.example.test/botSECRET/sendMessage <private-payload>'
    e.replies.append({'error_code': 400, 'description': description + ' ' + secret})
    lid = create(e)
    n = notice(e, lid)
    assert n['state'] == 'failed' and expected in n['error']
    assert len(sends(e)) == 1
    assert all(s not in n['error'] for s in ('PRIVATE', 'SECRET', 'https://', 'mock-token', 'payload'))
    assert len(n['error']) < 150
    assert e.service.view(lid, str(ADMIN), True)['notices'][0]['error'] == n['error']
    asyncio.run(e.service.deliver_notices(lid))
    assert len(sends(e)) == 1


@pytest.mark.parametrize('description', ['CUSTOM_EMOJI_ID_INVALID', 'custom emoji not allowed', 'custom emoji premium required'])
def test_existing_explicit_emoji_rejection_downgrades_body_and_button_once(e, description):
    e.replies.append({'error_code': 400, 'description': description})
    lid = create(e)
    sent = sends(e)
    assert len(sent) == 2 and notice(e, lid)['state'] == 'sent'
    first, second = [p for _, p in sent]
    assert first['text'].count('<tg-emoji') > 5 and '<tg-emoji' not in second['text']
    assert 'icon_custom_emoji_id' in first['reply_markup']['inline_keyboard'][0][0]
    assert 'icon_custom_emoji_id' not in second['reply_markup']['inline_keyboard'][0][0]
    assert first['chat_id'] == second['chat_id'] and first['message_thread_id'] == second['message_thread_id']
    assert first['reply_markup']['inline_keyboard'][0][0]['url'] == second['reply_markup']['inline_keyboard'][0][0]['url']
    assert json.loads(notice(e, lid)['payload_json']) == second


def test_no_repeat_downgrade_after_rate_limit_and_new_sender(e):
    e.replies.extend([{'error_code': 400, 'description': 'custom emoji not allowed'},
                      {'error_code': 429, 'description': 'limited'}])
    lid = create(e)
    assert notice(e, lid)['state'] == 'pending' and len(sends(e)) == 2
    asyncio.run(e.service.deliver_notices(lid))
    assert len(sends(e)) == 2
    e.service.db.execute('UPDATE lottery_notices SET next_at=0 WHERE lottery_id=?', (lid,))
    e.replies.append({'error_code': 400, 'description': 'custom emoji not allowed'})
    other = copy.copy(e.service)
    asyncio.run(other.deliver_notices(lid))
    assert len(sends(e)) == 3 and notice(e, lid)['state'] == 'failed'


@pytest.mark.parametrize('error_code', [400, 500])
def test_second_attempt_failure_never_third_send(e, error_code):
    e.replies.extend([{'error_code': 400, 'description': 'custom emoji not allowed'},
                      {'error_code': error_code, 'description': 'CUSTOM_EMOJI_ID_INVALID'}])
    lid = create(e)
    assert notice(e, lid)['state'] == ('failed' if error_code == 400 else 'unknown')
    assert len(sends(e)) == 2
    asyncio.run(e.service.deliver_notices(lid))
    assert len(sends(e)) == 2


@pytest.mark.parametrize('code', [500, 502, 503])
def test_even_emoji_shaped_server_error_never_downgrades(e, code):
    e.replies.append({'error_code': code, 'description': 'ENTITY_TEXT_INVALID custom emoji unsupported'})
    lid = create(e)
    assert len(sends(e)) == 1 and notice(e, lid)['state'] == 'unknown'
    asyncio.run(e.service.deliver_notices(lid))
    assert len(sends(e)) == 1


def test_transport_timeout_does_not_gain_retry_permission():
    reason, retry = notice_rejection('Timeout: custom emoji not allowed', {'state': 'unknown'})
    assert not retry and '人工核验' in reason


def test_expired_pending_launch_is_not_resent_or_redrawn(e):
    e.replies.append({'error_code': 429, 'description': 'limited'})
    lid = create(e, config(draw_mode='manual'))
    c = e.service.row(lid)['config']
    c['deadline'] = int(time.time()) - 1
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))
    e.service.db.execute('UPDATE lottery_notices SET next_at=0 WHERE lottery_id=?', (lid,))
    before = e.service.row(lid)
    asyncio.run(e.service.deliver_notices(lid))
    assert len(sends(e)) == 1 and notice(e, lid)['state'] == 'failed' and '未补发' in notice(e, lid)['error']
    assert e.service.row(lid) == before and not e.service.db.query('SELECT * FROM lottery_awards')


def test_failed_and_unknown_jobs_not_requeued_by_upgrade(e):
    for code, state in ((400, 'failed'), (500, 'unknown')):
        e.replies.append({'error_code': code, 'description': 'rejection'})
        lid = create(e)
        n = notice(e, lid)
        legacy = old_payload(json.loads(n['payload_json']))
        e.service.db.execute('UPDATE lottery_notices SET payload_json=? WHERE id=?', (json.dumps(legacy), n['id']))
        count = len(sends(e))
        asyncio.run(e.service.deliver_notices())
        assert notice(e, lid)['state'] == state and len(sends(e)) == count
        assert json.loads(notice(e, lid)['payload_json']) == legacy


def test_edit_not_modified_remains_successful(e, monkeypatch):
    lid = create(e)
    n = notice(e, lid)
    e.service.db.execute("UPDATE lottery_notices SET state='unknown',message_id=321 WHERE id=?", (n['id'],))
    original = e.service.bot._call

    async def no_change(method, payload=None, **kw):
        if method == 'editMessageText':
            CALL_DELIVERY.set(failure({'error_code': 400, 'description': 'message is not modified'}))
            _CALL_ERROR.set('message is not modified')
            return None
        return await original(method, payload, **kw)
    monkeypatch.setattr(e.service.bot, '_call', no_change)
    result = req(e, 'POST', f'/{lid}/notices/{n["id"]}/recover', json={})
    assert result.status_code == 200 and notice(e, lid)['state'] == 'sent' and notice(e, lid)['message_id'] == 321


def test_definite_rejection_arriving_at_deadline_does_not_send_fallback(e, monkeypatch):
    lid = create(e, publish=False)
    deadline = e.service.row(lid)['config']['deadline']
    original = e.service.bot._call
    attempts = []

    async def delayed_rejection(method, payload=None, **kw):
        if method == 'sendMessage':
            attempts.append(copy.deepcopy(payload))
            monkeypatch.setattr(time, 'time', lambda: deadline)
            CALL_DELIVERY.set(failure({'error_code': 400, 'description': 'custom emoji not allowed'}))
            _CALL_ERROR.set('custom emoji not allowed')
            return None
        return await original(method, payload, **kw)
    monkeypatch.setattr(e.service.bot, '_call', delayed_rejection)
    response = req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
    assert response.status_code == 200 and len(attempts) == 1
    assert notice(e, lid)['state'] == 'failed' and '报名期已结束，未补发' in notice(e, lid)['error']
    assert '<tg-emoji' in json.loads(notice(e, lid)['payload_json'])['text']
    assert e.service.row(lid)['state'] == 'published'
    assert not e.service.db.query('SELECT * FROM lottery_awards')
