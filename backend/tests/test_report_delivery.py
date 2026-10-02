"""Report delivery safety: no external requests, all recipients are synthetic."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.store import SettingsStore
from app.main import app
from app.modules import report_delivery as receipts
from app.modules.plugins_builtin import PluginContext, ViewingReportPlugin


@pytest.fixture
def report(tmp_path, monkeypatch):
    now = [time.mktime((2026, 10, 5, 12, 0, 0, 0, 0, -1))]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    people = [{'emby_user_id': 'one', 'tg_user_id': '101', 'username': 'Example One'},
              {'emby_user_id': 'two', 'tg_user_id': '102', 'username': 'Example Two'}]
    calls, errors = [], {}

    async def notify(member, text):
        uid = member['emby_user_id']
        calls.append(uid)
        error = errors.get(uid)
        if isinstance(error, Exception):
            raise error
        receipts.CALL_DELIVERY.set(error)
        return not error

    ctx = PluginContext(
        telegram=SimpleNamespace(enabled=True, notify_member=notify),
        members=SimpleNamespace(linked_telegram=lambda: people,
                                get=lambda uid: next((p for p in people if p['emby_user_id'] == uid), None)),
        stats=SimpleNamespace(member_detail=lambda *a, **kw: {'series': []}),
        store=SettingsStore(tmp_path / 'settings.json'))
    plugin = ViewingReportPlugin(ctx)

    async def no_poster(*args):
        return None

    monkeypatch.setattr(ViewingReportPlugin, '_poster', no_poster)
    return SimpleNamespace(plugin=plugin, ctx=ctx, calls=calls, errors=errors,
                           now=now, people=people, config={'period': 'weekly', 'hour': 0})


@pytest.mark.asyncio
async def test_permanent_failure_is_partial_and_not_scheduled_again(report):
    r = report
    r.errors['two'] = receipts.failure({'error_code': 403, 'description': 'bot was blocked'})
    result = await r.plugin.run(r.config)
    assert result['结果'] == '部分成功' and result['已送达'] == 1
    assert not r.plugin.due_today(r.config, r.now[0] + 60)
    await r.plugin.run(r.config)
    assert r.calls == ['one', 'two']
    public = r.plugin.delivery_status()
    assert public['retryable']
    assert 'tg_user_id' not in json.dumps(public)
    assert public['recipients'][1]['reason'] == '用户已屏蔽机器人'


@pytest.mark.asyncio
async def test_backoff_survives_restart_and_caps_attempts(report):
    r = report
    r.errors['two'] = receipts.failure({'error_code': 503})
    await r.plugin.run(r.config)
    for delay in receipts.BACKOFF:
        assert not r.plugin.due_today(r.config, r.now[0] + delay - 1)
        r.now[0] += delay
        r.ctx.store = SettingsStore(r.ctx.store._path)
        r.plugin = ViewingReportPlugin(r.ctx)
        assert r.plugin.due_today(r.config, r.now[0])
        await r.plugin.run(r.config)
    assert r.calls.count('one') == 1 and r.calls.count('two') == 4
    assert not r.plugin.due_today(r.config, r.now[0] + 7200)
    assert r.plugin.delivery_status()['recipients'][1]['state'] == 'failed'


@pytest.mark.asyncio
async def test_manual_retry_only_failed_preserves_success_and_binding(report):
    r = report
    r.errors['two'] = receipts.failure({'error_code': 403})
    await r.plugin.run(r.config)
    batch = r.plugin.delivery_status()['batch']
    r.people.append({'emby_user_id': 'new', 'tg_user_id': '103'})
    r.errors.clear()
    assert not (await r.plugin.run_failed(r.config, 'stale'))['ok']
    result = await r.plugin.run_failed(r.config, batch)
    assert result['ok'] and result['已送达'] == 2
    assert r.calls == ['one', 'two', 'two']
    await r.plugin.run_failed(r.config, batch)
    assert r.calls == ['one', 'two', 'two']


@pytest.mark.asyncio
async def test_rebound_recipient_is_not_sent_old_report(report):
    r = report
    r.errors['two'] = receipts.failure({'error_code': 403})
    await r.plugin.run(r.config)
    r.people[1]['tg_user_id'] = '999'
    r.errors.clear()
    await r.plugin.run_failed(r.config, r.plugin.delivery_status()['batch'])
    assert r.calls == ['one', 'two']
    assert '绑定已变化' in r.plugin.delivery_status()['recipients'][1]['reason']


@pytest.mark.asyncio
async def test_rate_limit_pauses_batch_and_honors_retry_after(report):
    r = report
    r.errors['one'] = receipts.failure({'error_code': 429, 'parameters': {'retry_after': 900}})
    await r.plugin.run(r.config)
    assert r.calls == ['one']
    assert not r.plugin.due_today(r.config, r.now[0] + 899)
    result = await r.plugin.run_failed(r.config, r.plugin.delivery_status()['batch'])
    assert not result['ok'] and r.calls == ['one']
    r.now[0] += 900
    r.errors.clear()
    assert (await r.plugin.run(r.config))['ok']
    assert r.calls == ['one', 'one', 'two']


@pytest.mark.asyncio
async def test_uncertain_delivery_never_auto_retries_or_falls_back(report):
    r = report
    photo_calls = []

    async def photo(*args):
        photo_calls.append(1)
        receipts.CALL_DELIVERY.set(receipts.failure(exc=httpx.ReadTimeout('private URL')))
        return False

    async def poster(*args):
        return b'fake'

    r.ctx.telegram.notify_member_photo = photo
    r.plugin._poster = poster
    result = await r.plugin.run(r.config)
    assert not result['ok'] and r.calls == [] and len(photo_calls) == 2
    assert not r.plugin.due_today(r.config, r.now[0] + 3600)
    assert all(x['state'] == 'unknown' for x in r.plugin.delivery_status()['recipients'])


@pytest.mark.asyncio
async def test_known_photo_content_rejection_can_fall_back_to_text(report):
    r = report

    async def photo(*args):
        receipts.CALL_DELIVERY.set(receipts.failure({'error_code': 400, 'description': 'bad photo'}))
        return False

    async def poster(*args):
        return b'fake'

    r.ctx.telegram.notify_member_photo = photo
    r.plugin._poster = poster
    assert (await r.plugin.run(r.config))['ok']
    assert r.calls == ['one', 'two']


@pytest.mark.asyncio
async def test_interrupted_sending_is_unknown_not_resent(report):
    r = report
    state = receipts.new_batch(r.people, r.config, r.now[0])
    state['recipients']['one']['state'] = 'sent'
    state['recipients']['two']['state'] = 'sending'
    r.ctx.set_state('viewing_report', state)
    assert r.plugin.due_today(r.config, r.now[0])
    await r.plugin.run(r.config)
    assert r.calls == []
    assert r.plugin.delivery_status()['recipients'][1]['state'] == 'unknown'


@pytest.mark.asyncio
async def test_legacy_success_receipts_are_not_reinterpreted_as_current_members(report):
    r = report
    state = {'batch': time.strftime('%Y-%m-%d', time.localtime(r.now[0])) + json.dumps(r.config), 'sent': ['one']}
    r.ctx.set_state('viewing_report', state)
    assert not r.plugin.due_today(r.config, r.now[0])
    await r.plugin.run(r.config)
    assert r.calls == [] and r.plugin.delivery_status()['legacy']
    assert not (await r.plugin.run_failed(r.config, state['batch']))['ok']
    r.now[0] += 7 * 86400
    assert r.plugin.due_today(r.config, r.now[0])
    assert (await r.plugin.run(r.config))['ok']


@pytest.mark.asyncio
async def test_config_changes_and_new_members_do_not_reset_same_day_receipts(report):
    r = report
    await r.plugin.run(r.config)
    r.people.append({'emby_user_id': 'new', 'tg_user_id': '103'})
    await r.plugin.run({'period': 'monthly', 'hour': 3})
    assert r.calls == ['one', 'two']
    assert r.plugin.delivery_status()['summary']['已送达'] == 2


@pytest.mark.parametrize('body,state', [
    ({'error_code': 403}, 'failed'), ({'error_code': 401}, 'failed'),
    ({'error_code': 400, 'description': 'chat not found'}, 'failed'),
    ({'error_code': 429, 'parameters': {'retry_after': 'bad'}}, 'retry'),
    ({'error_code': 502}, 'retry'), ([], 'unknown'),
])
def test_safe_failure_classification(body, state):
    result = receipts.failure(body)
    assert result['state'] == state
    assert 'description' not in result


def test_api_requires_auth_and_refuses_legacy_and_stale_batches():
    with TestClient(app) as client:
        url = '/api/plugins/viewing_report/retry-failed'
        assert client.post(url, json={'batch': 'example'}).status_code == 401
        assert client.post(url, json={'batch': 'example'}, auth=('admin', 'change-me')).status_code == 409
        state = receipts.new_batch([{'emby_user_id': 'one', 'tg_user_id': '101'}], {}, time.time())
        state['recipients']['one']['state'] = 'failed'
        plugin = app.state.plugins.get('viewing_report')
        plugin.ctx.set_state('viewing_report', state)
        assert client.post(url, json={'batch': 'old'}, auth=('admin', 'change-me')).status_code == 409
        data = client.get('/api/plugins/viewing_report', auth=('admin', 'change-me')).json()
        assert data['delivery']['retryable']


def test_ui_has_scoped_retry_and_escaped_receipts():
    source = (Path(__file__).parents[1] / 'app/static/ops.js').read_text()
    assert '仅重试失败对象' in source and 'pluginResultTag(last)' in source
    assert 'JSON.stringify({batch})' in source
    assert 'esc(r.username)' in source and 'esc(r.reason' in source
    assert '结果不明可能已送达' in source
    assert '不是已完成' in source


@pytest.mark.asyncio
@pytest.mark.parametrize('multipart', [False, True])
async def test_real_telegram_transport_preserves_safe_delivery_classification(multipart):
    from app.modules.telegram import TelegramBot
    credential = '1234567' + ':placeholder-not-real'
    bot = TelegramBot(lambda: {'enabled': True, 'bot_token': credential}, None)
    response = [{'ok': False, 'error_code': 429,
                 'description': 'rate limit ' + credential, 'parameters': {'retry_after': 321}}]
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(429, json=response[0])))

    async def get_client():
        return client

    bot._client = get_client
    try:
        if multipart:
            assert await bot._call_multipart('sendPhoto', {'chat_id': '101'},
                                             {'photo': ('fake.jpg', b'fake', 'image/jpeg')}) is None
        else:
            assert await bot._call('sendMessage', {'chat_id': '101'}) is None
        assert receipts.CALL_DELIVERY.get()['retry_after'] == 321
        assert credential not in str(receipts.CALL_DELIVERY.get())
        response[0] = {'ok': True, 'result': {'message_id': 42}}
        await bot._call('sendMessage', {'chat_id': '101'})
        assert receipts.CALL_DELIVERY.get() is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retries_cross_midnight_but_close_after_a_day_without_sending(report):
    from app.modules.plugins import PluginRegistry
    r = report
    r.now[0] = time.mktime((2026, 10, 5, 23, 59, 0, 0, 0, -1))
    r.config['hour'] = 23
    r.errors['two'] = receipts.failure({'error_code': 503})
    await r.plugin.run(r.config)
    registry = PluginRegistry(r.ctx.store, SimpleNamespace(one=lambda *args: None))
    registry.register(r.plugin)
    registry.save('viewing_report', True, r.config)
    r.now[0] += 300
    assert registry._due('viewing_report', r.now[0])
    await registry.run_now('viewing_report', trigger='schedule')
    assert r.calls == ['one', 'two', 'two']
    r.now[0] += 86400
    assert registry._due('viewing_report', r.now[0])
    await registry.run_now('viewing_report', trigger='schedule')
    assert r.calls == ['one', 'two', 'two']
    assert not registry._due('viewing_report', r.now[0] + 60)
    assert r.plugin.delivery_status()['recipients'][1]['state'] == 'failed'
