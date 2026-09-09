"""Temporary real application API; no live Telegram polling or credentials."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ConfigError
from app.main import app
from app.modules.telegram import TelegramBot

AUTH = ('admin', 'change-me')
CHAT = '-100333'


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(TelegramBot, 'start', lambda self: None)
    with TestClient(app) as client:
        bot = app.state.telegram
        state = SimpleNamespace(permission='administrator', membership='member', deleted=[], calls=[], block_user=False)
        async def call(method, payload=None, timeout=20):
            state.calls.append((method, payload))
            if method == 'getChat':
                return {'id': int(CHAT), 'type': 'channel', 'title': 'Actual <channel>'}
            if method == 'getChatMember':
                uid = payload['user_id']
                while uid != 123 and state.block_user:
                    await asyncio.sleep(.01)
                return {'user': {'id': uid}, 'status': state.permission if uid == 123 else state.membership}
            return True
        bot._call = call
        response = client.post('/api/settings/telegram', auth=AUTH,
            json={'bot_token': '123:local-placeholder', 'enabled': True,
                  'require_group': '@legacy_required', 'group_interaction_chats': ['-100444']})
        assert response.status_code == 200
        app.state.members.upsert('group-test-user', 'GroupUser', {'group_id': 'standard'})
        app.state.members.bind_telegram('group-test-user', '9001')
        async def users():
            return [{'Id': 'group-test-user', 'Policy': {'IsAdministrator': False}}]
        async def delete(uid):
            state.deleted.append(uid)
            return True
        bot._emby.list_users = users
        bot._emby.delete_user = delete
        yield client, bot, state


def rules(delete=False, gate=False):
    return {'targets': [{'chat_id': '@some_channel', 'enabled': True, 'join_url': 'https://t.me/local_channel'}],
            'delete_enabled': delete, 'gate_enabled': gate}


def test_defaults_off_no_legacy_configuration_migration(api):
    client, bot, state = api
    payload = client.get('/api/settings/telegram', auth=AUTH).json()
    assert payload['require_group'] == '@legacy_required'
    assert payload['group_interaction_chats'] == ['-100444']
    assert payload['membership_rules']['targets'] == []
    assert payload['membership_rules']['delete_enabled'] is False
    assert payload['membership_schedule']['enabled'] is False
    assert not state.deleted and bot.membership._scan_task is None


def test_verify_resolves_actual_identity_and_save_preserves_original_group_semantics(api):
    client, bot, _state = api
    verified = client.post('/api/telegram/membership/verify', auth=AUTH, json=rules()).json()
    assert verified['targets'][0]['chat_id'] == CHAT
    assert verified['targets'][0]['title'] == 'Actual <channel>'
    assert verified['targets'][0]['type'] == 'channel'
    response = client.post('/api/settings/telegram', auth=AUTH, json={'membership_rules': rules(True, True)})
    assert response.status_code == 200
    payload = response.json()
    assert payload['membership_rules']['delete_enabled'] and payload['membership_rules']['generation']
    assert payload['require_group'] == '@legacy_required' and payload['group_interaction_chats'] == ['-100444']
    assert 'local-placeholder' not in response.text
    assert bot.membership._scan_task is None  # saving/enabling never runs a migration scan
    audit = app.state.db.one("SELECT * FROM audit_log WHERE action='settings.telegram.membership'")
    assert audit and 'gate=True delete=True' in str(audit) and CHAT in str(audit)


@pytest.mark.parametrize('failure', ['permission', 'empty', 'bad_link', 'bad_port', 'not_object', 'not_bool', 'spoof_metadata'])
def test_enable_rejects_missing_requirements_even_if_browser_claims_verified(api, failure):
    client, _bot, state = api
    raw = rules(True)
    if failure == 'permission':
        state.permission = 'member'
    elif failure == 'empty':
        raw['targets'] = []
    elif failure == 'bad_link':
        raw['targets'][0]['join_url'] = 'javascript:alert(1)'
    elif failure == 'bad_port':
        raw['targets'][0]['join_url'] = 'https://t.me:invalid/local'
    elif failure == 'not_object':
        raw = None
    elif failure == 'not_bool':
        raw['delete_enabled'] = 'false'
    else:
        state.permission = 'left'
        raw['targets'][0].update(verification='ready', title='Fake Verified')
    response = client.post('/api/settings/telegram', auth=AUTH, json={'membership_rules': raw})
    assert response.status_code == 422
    assert client.get('/api/settings/telegram', auth=AUTH).json()['membership_rules']['delete_enabled'] is False
    assert not state.deleted


def test_settings_service_internal_verification_is_not_a_payload_flag(api):
    _client, _bot, _state = api
    raw = rules(True)
    raw['targets'][0]['verification'] = 'ready'
    with pytest.raises(ConfigError):
        app.state.settings_service.save_telegram({'membership_rules': raw, 'membership_verified': True})


@pytest.mark.parametrize('path,method', [('/api/telegram/membership', 'get'),
    ('/api/telegram/membership/verify', 'post'), ('/api/telegram/membership/scan', 'post')])
def test_group_endpoints_require_existing_admin_auth(api, path, method):
    client, _bot, state = api
    response = getattr(client, method)(path)
    assert response.status_code == 401 and not state.deleted


@pytest.mark.parametrize('delete', [False, True])
def test_manual_api_scan_is_async_and_obeys_global_switch_for_existing_member(api, delete):
    client, _bot, state = api
    state.membership = 'left'
    saved = client.post('/api/settings/telegram', auth=AUTH, json={'membership_rules': rules(delete)})
    assert saved.status_code == 200
    start = client.post('/api/telegram/membership/scan', auth=AUTH)
    assert start.status_code == 200 and start.json()['id']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get('/api/telegram/membership', auth=AUTH).json()['scan']
        if not result['running']:
            break
        time.sleep(.01)
    assert not result['running']
    row = next(r for r in result['rows'] if r['user_id'] == 'group-test-user')
    assert row['action'] == ('deleted' if delete else 'detected')
    assert state.deleted == (['group-test-user'] if delete else [])


@pytest.mark.parametrize('keep_gate', [False, True])
def test_disabling_rules_does_not_wait_for_telegram_permission_queries(api, keep_gate):
    client, _bot, state = api
    response = client.post('/api/settings/telegram', auth=AUTH, json={'membership_rules': rules(True, True)})
    assert response.status_code == 200
    current = response.json()['membership_rules']
    current.update(gate_enabled=keep_gate, delete_enabled=False)
    state.calls.clear()
    state.permission = 'left'
    response = client.post('/api/settings/telegram', auth=AUTH, json={'membership_rules': current})
    assert response.status_code == 200
    assert response.json()['membership_rules']['delete_enabled'] is False
    assert not state.calls


def test_disabled_draft_can_be_saved_but_token_change_requires_reverification(api):
    client, _bot, state = api
    state.permission = 'member'
    assert client.post('/api/settings/telegram', auth=AUTH, json={'membership_rules': rules()}).status_code == 200
    state.permission = 'administrator'
    assert client.post('/api/settings/telegram', auth=AUTH, json={'membership_rules': rules(True)}).status_code == 200
    response = client.post('/api/settings/telegram', auth=AUTH, json={'bot_token': '124:other-local'})
    assert response.status_code == 422
    assert client.get('/api/settings/telegram', auth=AUTH).json()['membership_rules']['delete_enabled']
