"""Interaction acceptance paths: target identity, navigation, privacy and transport."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from test_tg_interaction_context import ADMIN, GROUP, SECOND, VIEWER, click, command
from test_tg_interaction_context import env as context_env  # noqa: F401

from app.modules.bot_views import POEMS, poetry_line


@pytest.fixture
def env(request):
    return request.getfixturevalue('context_env')


@pytest.mark.parametrize('chat', [GROUP, ADMIN])
def test_target_refresh_group_change_renew_failure_and_cancel_stay_scoped(env, chat):
    async def run():
        mid = await command(env, '/kk ViewerA', chat=chat)
        await click(env, 'admin_usage', mid, chat=chat)
        assert 'ViewerA' in env.tg.text(chat, mid)
        await click(env, 'admin_card', mid, chat=chat)
        await click(env, 'admin_groups', mid, chat=chat)
        await click(env, 'admin_group_pick:vip', mid, chat=chat)
        assert 'ViewerA' in env.tg.text(chat, mid)
        await click(env, 'admin_card', mid, chat=chat)  # cancel the group preview
        assert env.members.get('u1')['group_id'] == 'standard'
        await click(env, 'admin_groups', mid, chat=chat)
        await click(env, 'admin_group_pick:vip', mid, chat=chat)
        key = env.bot._session_key(chat, ADMIN, group=chat < 0)
        nonce = env.bot._pending[key][2]['group_confirm']['nonce']
        await click(env, 'admin_group_apply:keep:' + nonce, mid, chat=chat)
        assert env.members.get('u1')['group_id'] == 'vip'
        assert 'ViewerA' in env.tg.text(chat, mid)
        await click(env, 'admin_group_apply:keep:' + nonce, mid, chat=chat)  # replay is inert
        await click(env, 'admin_renew', mid, chat=chat)
        await command(env, '7', chat=chat, reply=mid if chat < 0 else None)
        assert '永久' in env.tg.text(chat, mid)
        assert env.members.get('u1')['expires_at_effective'] is None
        await command(env, '/cancel', chat=chat, reply=mid if chat < 0 else None)
        assert 'ViewerA' in env.tg.text(chat, mid)
        assert 'Operator' not in env.tg.text(chat, mid)
    asyncio.run(run())


def test_two_admins_multiple_targets_and_private_group_inputs_do_not_cross(env):
    a_expiry = env.members.get('u1')['expires_at_effective']
    b_expiry = env.members.get('u2')['expires_at_effective']
    async def run():
        a, b, private = await asyncio.gather(
            command(env, '/kk ViewerA', user=ADMIN),
            command(env, '/kk ViewerB', user=SECOND),
            command(env, '/kk ViewerB', chat=ADMIN, user=ADMIN))
        await click(env, 'admin_renew', a)
        await click(env, 'admin_score', b, user=SECOND)
        await click(env, 'admin_renew', private, chat=ADMIN)
        await asyncio.gather(command(env, '3', reply=a),
                             command(env, '+9', user=SECOND, reply=b),
                             command(env, '5', chat=ADMIN))
        assert 'ViewerA' in env.tg.text(GROUP, a)
        assert 'ViewerB' in env.tg.text(GROUP, b)
        assert 'ViewerB' in env.tg.text(ADMIN, private)
    asyncio.run(run())
    assert env.members.get('u1')['expires_at_effective'] == a_expiry + 3 * 86400
    assert env.members.get('u2')['expires_at_effective'] == b_expiry + 5 * 86400
    assert env.bot._points.balance('u1') == 50 and env.bot._points.balance('u2') == 9


@pytest.mark.parametrize('case', ['bystander', 'other_admin', 'demoted', 'rebound_admin', 'wrong_topic', 'expired', 'unknown_message'])
def test_unauthorized_or_stale_card_does_not_edit_or_change_any_account(env, case):
    async def run():
        mid = await command(env, '/kk ViewerA', thread=3)
        user, thread, selected = ADMIN, 3, mid
        if case == 'bystander':
            user = VIEWER
        elif case == 'other_admin':
            user = SECOND
        elif case == 'demoted':
            env.members.set_roles('admin', [])
        elif case == 'rebound_admin':
            env.members.bind_telegram('admin2', str(ADMIN))
        elif case == 'wrong_topic':
            thread = 4
        elif case == 'expired':
            env.bot._admin_panels[(str(GROUP), mid)]['expires'] = time.time() - 1
        elif case == 'unknown_message':
            selected += 999
        before = env.members.get('u1')
        env.tg.calls.clear()
        await click(env, 'admin_prouser', selected, user=user, thread=thread)
        # Role-aware slash menus are independently refreshed for this sender;
        # denial must not edit/delete/send a card or mutate any account.
        assert all(method in ('answerCallbackQuery', 'setMyCommands') for method, _ in env.tg.calls)
        assert sum(method == 'answerCallbackQuery' for method, _ in env.tg.calls) == 1
        for method, payload in env.tg.calls:
            if method == 'setMyCommands':
                assert payload['scope'] == {'type': 'chat_member', 'chat_id': GROUP, 'user_id': user}
        assert env.members.get('u1') == before
    asyncio.run(run())


@pytest.mark.parametrize('used', [None, 0, 1024])
def test_group_measurement_unknown_is_not_zero_or_legacy_estimate(env, used):
    class Metering:
        def snapshot(self, uid):
            return {'measured_used_bytes': used, 'coverage': {'degraded': True,
                'nodes': [{'name': 'private-node-label', 'ok': False}]}}
    env.members.bind_metering(Metering(), cutover=True)
    env.members.add_traffic('u1', 987654)
    mid = asyncio.run(command(env, '/me', user=VIEWER))
    text = env.tg.text(GROUP, mid)
    assert '实测账本' in text and 'private-node-label' not in text
    assert '987654' not in text and '旧估算' not in text
    if used is None:
        assert '暂未测得' in text and '本月剩余：暂无法确认' in text
    elif used == 0:
        assert '0 B' in text and '本月剩余：1.0 TiB' in text
    else:
        assert '1.0 KiB' in text


@pytest.mark.parametrize('action', ['devices', 'invite_new', 'buyok:1', 'admin_code', 'admin_gift', 'resetpw', 'rebind', 'watch_recent', 'orders', 'me_nodes', 'transfer_ok'])
def test_group_sensitive_callbacks_are_private_only_and_do_not_write(env, action):
    async def run():
        mid = await command(env, '/kk ViewerA')
        before = env.db.query('SELECT * FROM members')
        env.tg.calls.clear()
        await click(env, action, mid)
        assert env.db.query('SELECT * FROM members') == before
        assert all(method == 'answerCallbackQuery' for method, _ in env.tg.calls)
        assert '私聊' in str(env.tg.calls)
    asyncio.run(run())


def test_private_link_keeps_target_and_requires_current_admin(env):
    async def run():
        mid = await command(env, '/kk ViewerA')
        link = next(b['url'] for b in env.tg.actions(GROUP, mid) if 'url' in b)
        private = await command(env, '/start ' + link.split('start=')[1], chat=ADMIN)
        assert 'ViewerA' in env.tg.text(ADMIN, private)
        assert '管理目标' in env.tg.text(ADMIN, private)
        denied = await command(env, '/start manage_u1', chat=VIEWER, user=VIEWER)
        assert 'ViewerA' not in env.tg.text(VIEWER, denied)
        assert '仅管理员' in env.tg.text(VIEWER, denied)
    asyncio.run(run())


def test_not_modified_uses_original_message_without_send(env):
    async def run():
        mid = await command(env, '/kk ViewerA')
        env.tg.edit_error = 'Bad Request: message is not modified'
        env.tg.calls.clear()
        after = await click(env, 'admin_card', mid)
        assert after == mid
        assert not any(m == 'sendMessage' for m, _ in env.tg.calls)
    asyncio.run(run())


def test_uneditable_result_replaces_once_without_reapplying_group(env, monkeypatch):
    original = env.members.upsert
    changes = []
    def upsert(*args, **kwargs):
        changes.append(args)
        return original(*args, **kwargs)
    async def run():
        mid = await command(env, '/kk ViewerA')
        await click(env, 'admin_groups', mid)
        await click(env, 'admin_group_pick:vip', mid)
        nonce = env.bot._pending[f'g:{GROUP}:0:{ADMIN}'][2]['group_confirm']['nonce']
        monkeypatch.setattr(env.members, 'upsert', upsert)
        env.tg.edit_error = "Bad Request: message can't be edited"
        env.tg.calls.clear()
        new = await click(env, 'admin_group_apply:keep:' + nonce, mid)
        assert new != mid
        assert len([m for m, _ in env.tg.calls if m == 'sendMessage']) == 1
        await click(env, 'admin_group_apply:keep:' + nonce, mid)
        await click(env, 'admin_group_apply:keep:' + nonce, new)
        assert len(changes) == 1
        assert env.members.get('u1')['group_id'] == 'vip'
    asyncio.run(run())


def test_network_failure_after_renew_cannot_reexecute_on_repeated_input(env):
    before = env.members.get('u1')['expires_at_effective']
    async def run():
        mid = await command(env, '/kk ViewerA')
        await click(env, 'admin_renew', mid)
        env.tg.edit_error = 'ReadTimeout: 请求失败'
        env.tg.calls.clear()
        await command(env, '7', reply=mid)
        await command(env, '7', reply=mid)
        assert not any(m == 'sendMessage' for m, _ in env.tg.calls)
        env.tg.edit_error = ''
        await click(env, 'admin_card', mid)
        assert 'ViewerA' in env.tg.text(GROUP, mid)
    asyncio.run(run())
    assert env.members.get('u1')['expires_at_effective'] == before + 7 * 86400


def test_failed_replacement_send_does_not_create_a_ghost_panel(env):
    async def run():
        mid = await command(env, '/kk ViewerA')
        env.tg.edit_error = 'Bad Request: message to edit not found'
        env.tg.fail_send = True
        after = await click(env, 'admin_usage', mid)
        assert after == mid
        assert set(env.bot._admin_panels) == {(str(GROUP), mid)}
        assert not env.bot._retired_panels
    asyncio.run(run())


def test_private_home_refresh_edits_existing_text_even_with_logo(env):
    async def run():
        mid = await command(env, '/me', chat=VIEWER, user=VIEWER)
        env.cfg['menu_logo_url'] = 'https://images.example/logo.png'
        env.tg.calls.clear()
        after = await click(env, 'home', mid, chat=VIEWER, user=VIEWER)
        assert after == mid
        assert not any(m in ('sendMessage', 'sendPhoto') for m, _ in env.tg.calls)
        assert any(m == 'editMessageText' for m, _ in env.tg.calls)
    asyncio.run(run())


def test_outbound_notice_does_not_change_admin_panel_or_copy_group_topic(env):
    async def run():
        mid = await command(env, '/kk ViewerA', thread=3)
        with env.bot._bind_session(GROUP, ADMIN, group=True, thread_id=3):
            await env.bot.send_message(VIEWER, 'Local notice', [[{'text': 'Claim', 'callback_data': 'req_claim:1'}]])
            await env.bot._edit(VIEWER, 99, 'Local notice changed')
        assert env.bot._panel[f'g:{GROUP}:3:{ADMIN}'] == mid
        notice = next(p for m, p in env.tg.calls if m == 'sendMessage' and p['chat_id'] == VIEWER)
        assert 'message_thread_id' not in notice
    asyncio.run(run())


def test_poetry_is_local_attributed_and_stable_for_card_refresh(env):
    assert len(POEMS) >= 15 and len(set(POEMS)) == len(POEMS)
    assert all('《' in author and len(verse) > 8 for verse, author in POEMS)
    env.members.upsert('u1', 'ViewerA', {'group_id': 'whitelist'})
    first = env.bot._brief_card(env.members.get('u1'))
    second = env.bot._brief_card(env.members.get('u1'))
    assert poetry_line('u1') in first and first == second


def test_all_rendered_callbacks_fit_telegram_limit_and_escape_external_text(env):
    env.members.upsert('u1', '<b>Viewer</b>', {})
    async def run():
        mid = await command(env, '/kk ' + str(VIEWER))
        assert '&lt;b&gt;Viewer&lt;/b&gt;' in env.tg.text(GROUP, mid)
        await click(env, 'admin_groups', mid)
        await click(env, 'admin_group_pick:whitelist', mid)
        await click(env, 'admin_card', mid)
        await click(env, 'admin_rm', mid)
        for _method, payload in env.tg.calls:
            for row in payload.get('reply_markup', {}).get('inline_keyboard', []):
                for button in row:
                    if 'callback_data' in button:
                        assert 0 < len(button['callback_data'].encode()) <= 64
        assert env.members.get('u1')['group_id'] == 'standard'
    asyncio.run(run())


def test_local_success_remote_failure_remains_on_target_card(env):
    env.bot._on_member_changed = AsyncMock(return_value={'ok': False, 'remote_ok': False})
    before = env.members.get('u1')['expires_at_effective']
    async def run():
        mid = await command(env, '/kk ViewerA')
        await click(env, 'admin_renew', mid)
        await command(env, '3', reply=mid)
        assert '远端未确认' in env.tg.text(GROUP, mid) and 'ViewerA' in env.tg.text(GROUP, mid)
        await click(env, 'admin_card', mid)
        assert 'ViewerA' in env.tg.text(GROUP, mid)
    asyncio.run(run())
    assert env.members.get('u1')['expires_at_effective'] == before + 3 * 86400


def test_group_cancel_command_without_reply_returns_current_target(env):
    before = env.members.get('u1')['expires_at_effective']
    async def run():
        mid = await command(env, '/kk ViewerA')
        await click(env, 'admin_renew', mid)
        await command(env, '/cancel')
        assert 'ViewerA' in env.tg.text(GROUP, mid) and '已取消' in env.tg.text(GROUP, mid)
        await command(env, '7', reply=mid)
    asyncio.run(run())
    assert env.members.get('u1')['expires_at_effective'] == before


@pytest.mark.parametrize('chat', [GROUP, ADMIN])
def test_legacy_amount_button_cannot_replay_renewal(env, chat):
    before = env.members.get('u1')['expires_at_effective']
    async def run():
        mid = await command(env, '/kk ViewerA', chat=chat)
        env.tg.calls.clear()
        await click(env, 'admin_renew:30', mid, chat=chat)
        await click(env, 'admin_renew:30', mid, chat=chat)
        assert all(method == 'answerCallbackQuery' for method, _ in env.tg.calls)
        assert '旧版快捷续期已失效' in str(env.tg.calls)
        assert env.members.get('u1')['expires_at_effective'] == before
    asyncio.run(run())


@pytest.mark.parametrize('chat', [GROUP, ADMIN])
def test_new_command_cancels_previous_confirmation_but_keeps_target(env, chat):
    async def run():
        mid = await command(env, '/kk ViewerA', chat=chat)
        await click(env, 'admin_groups', mid, chat=chat)
        await click(env, 'admin_group_pick:vip', mid, chat=chat)
        key = env.bot._session_key(chat, ADMIN, group=chat < 0)
        nonce = env.bot._pending[key][2]['group_confirm']['nonce']
        await command(env, '/me', chat=chat)
        await click(env, 'admin_group_apply:keep:' + nonce, mid, chat=chat)
        assert env.members.get('u1')['group_id'] == 'standard'
        await click(env, 'admin_card', mid, chat=chat)
        assert 'ViewerA' in env.tg.text(chat, mid)
        assert '管理目标' in env.tg.text(chat, mid)
    asyncio.run(run())


@pytest.mark.parametrize('kind,back', [('transfer', 'bag'), ('req_new', 'request_center')])
def test_private_return_to_parent_cancels_hidden_input_state(env, kind, back):
    from types import SimpleNamespace
    env.bot._plugins = SimpleNamespace(enabled=lambda name: True)
    async def run():
        mid = await command(env, '/me', chat=VIEWER, user=VIEWER)
        await click(env, kind, mid, chat=VIEWER, user=VIEWER)
        assert env.bot._pending.get(str(VIEWER))
        await click(env, back, mid, chat=VIEWER, user=VIEWER)
        assert env.bot._pending.get(str(VIEWER)) is None
    asyncio.run(run())
