"""Direct whitelist issuance stays a scoped, audited admin action."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from test_tg_interaction_context import ADMIN, GROUP, VIEWER, click, command
from test_tg_interaction_context import env as context_env  # noqa: F401

from app.core.errors import ConfigError


@pytest.fixture
def env(request):
    return request.getfixturevalue('context_env')


@pytest.mark.parametrize('selector', ['ViewerA', str(VIEWER), 'reply'])
def test_prouser_direct_issue_preserves_state_privileges_and_history(env, selector):
    env.members.set_status('u1', 'suspended')
    env.members.set_roles('u1', ['uploader'])
    env.members.set_overrides('u1', {'max_streams': 3, 'allow_download': 1})
    env.members.add_traffic('u1', 1234)
    async def run():
        if selector == 'reply':
            await env.bot._dispatch_update({'message': {
                'chat': {'id': GROUP, 'type': 'supergroup'}, 'from': {'id': ADMIN},
                'text': '/prouser', 'reply_to_message': {'from': {'id': VIEWER, 'is_bot': False}}}})
        else:
            await command(env, '/prouser ' + selector)
    asyncio.run(run())
    target = env.members.get('u1')
    assert target['group_id'] == 'whitelist'
    assert target['roles'] == ['uploader'] and target['status'] == 'suspended'
    assert target['overrides'] == {'max_streams': 3, 'allow_download': 1}
    assert target['traffic_used_bytes'] == 1234 and env.bot._points.balance('u1') == 50
    assert target['expires_at_effective'] is None
    text = '\n'.join(p.get('text', '') for m, p in env.tg.calls if m == 'sendMessage')
    assert 'ViewerA' in text and '白名单' in text and '签发' in text
    assert '确认换组' not in text


def test_prouser_already_whitelisted_is_noop_even_after_message_failure(env):
    async def run():
        env.tg.fail_send = True
        await command(env, '/prouser ViewerA')
        env.tg.fail_send = False
        await command(env, '/prouser ViewerA')
    asyncio.run(run())
    assert env.members.get('u1')['group_id'] == 'whitelist'
    assert len(env.db.query("SELECT * FROM audit_log WHERE action='telegram.prouser'")) == 1
    assert '已在白名单' in '\n'.join(p.get('text', '') for _, p in env.tg.calls)


@pytest.mark.parametrize('case', ['ordinary', 'anonymous', 'wrong_group', 'missing', 'ambiguous'])
def test_prouser_rejects_invalid_authority_or_target(env, case):
    if case == 'ambiguous':
        env.members.bind_telegram('u2', '904', 'ViewerA')
    async def run():
        if case == 'anonymous':
            await env.bot._dispatch_update({'message': {'chat': {'id': GROUP, 'type': 'supergroup'},
                'from': {'id': ADMIN}, 'sender_chat': {'id': GROUP}, 'text': '/prouser ViewerA'}})
        else:
            await command(env, '/prouser ' + ('Absent' if case == 'missing' else 'ViewerA'),
                          user=VIEWER if case == 'ordinary' else ADMIN,
                          chat=-100701 if case == 'wrong_group' else GROUP)
    asyncio.run(run())
    assert env.members.get('u1')['group_id'] == 'standard'
    assert env.members.get('u2')['group_id'] == 'standard'
    assert not env.db.query("SELECT * FROM audit_log WHERE action='telegram.prouser'")


def test_prouser_sync_failure_is_not_announced_as_remote_success(env):
    env.bot._on_member_changed = AsyncMock(return_value={'ok': False, 'remote_ok': False})
    asyncio.run(command(env, '/prouser ViewerA'))
    assert env.members.get('u1')['group_id'] == 'whitelist'
    text = '\n'.join(p.get('text', '') for _, p in env.tg.calls)
    assert '远端未确认' in text and '🎉' not in text
    env.bot._on_member_changed.assert_awaited_once_with('u1', 0)


def test_prouser_local_failure_never_produces_a_celebration(env, monkeypatch):
    def fail(*args, **kwargs):
        raise ConfigError('local test failure')
    monkeypatch.setattr(env.members, 'upsert', fail)
    asyncio.run(command(env, '/prouser ViewerA'))
    assert env.members.get('u1')['group_id'] == 'standard'
    assert '🎉' not in str(env.tg.calls)
    assert not env.db.query("SELECT * FROM audit_log WHERE action='telegram.prouser'")


def test_prouser_button_keeps_target_and_escapes_names(env):
    env.members.upsert('u1', '<b>Viewer</b>', {})
    env.members.upsert('admin', '<i>Operator</i>', {})
    async def run():
        mid = await command(env, '/kk ' + str(VIEWER))
        assert any(b.get('callback_data') == 'admin_prouser' for b in env.tg.actions(GROUP, mid))
        await click(env, 'admin_prouser', mid)
        text = env.tg.text(GROUP, mid)
        assert '&lt;b&gt;Viewer&lt;/b&gt;' in text
        assert '&lt;i&gt;Operator&lt;/i&gt;' in text
        assert 'admin_card' in str(env.tg.actions(GROUP, mid))
    asyncio.run(run())
    assert env.members.get('u1')['group_id'] == 'whitelist'
