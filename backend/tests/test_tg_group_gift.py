"""Allowed-group gift -> recipient-bound private registration, using real services."""
import asyncio
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from test_tg_interaction_context import ADMIN, GROUP, SECOND, VIEWER, click, command
from test_tg_interaction_context import env as context_env  # noqa: F401

TARGET = 955
OTHER = 956


@pytest.fixture
def env(request):
    return request.getfixturevalue('context_env')


def grant(env, target=TARGET):
    return env.bot._registration.get_grant(str(target))


def audit_count(env):
    return env.db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action='registration.gift'")['n']


async def confirm_screen(env, target=TARGET, user=ADMIN, chat=GROUP):
    mid = await command(env, '/kk ' + str(target), chat=chat, user=user)
    assert any(b.get('callback_data') == 'admin_gift' for b in env.tg.actions(chat, mid))
    await click(env, 'admin_gift', mid, chat=chat, user=user)
    action = next(b['callback_data'] for b in env.tg.actions(chat, mid) if b.get('callback_data', '').startswith('admin_gift_ok:'))
    return mid, action


async def issue(env, target=TARGET, user=ADMIN, chat=GROUP):
    mid, action = await confirm_screen(env, target, user, chat)
    await click(env, action, mid, chat=chat, user=user)
    return mid, action


async def confirm_registration_password(env, target=TARGET):
    for action in ('random', 'confirm'):
        p = env.bot._pending[str(target)][2]
        await click(env, f"pw:{p['nonce']}:{action}", p['message_id'], chat=target, user=target)
    return env.bot._panel[str(target)]


def setup_gate(env):
    env.cfg['membership_rules'] = {'targets': [{'chat_id': str(GROUP), 'enabled': True,
        'title': 'Local Group', 'type': 'supergroup', 'join_url': 'https://t.me/local_group',
        'verification': 'ready'}], 'gate_enabled': True, 'delete_enabled': False,
        'generation': 'local', 'enabled_since': 0}
    state = {'status': 'left'}
    original = env.bot._call
    async def call(method, payload=None, timeout=20):
        if method == 'getChat':
            return {'id': GROUP, 'type': 'supergroup', 'title': 'Local Group'}
        if method == 'getChatMember':
            uid = str(payload['user_id'])
            return {'user': {'id': int(uid)}, 'status': 'administrator' if uid == '123' else state['status']}
        return await original(method, payload, timeout)
    env.bot._call = call
    return state


def test_same_group_message_compact_bound_url_and_no_public_code(env):
    env.cfg['register_days'] = 45
    async def run():
        mid, action = await issue(env)
        row = env.tg.message(GROUP, mid)
        text = row['text']
        code = grant(env)['gift_code']
        assert row['message_id'] == mid and row['parse_mode'] == 'HTML'
        assert 'tg://user?id=901' in text and 'tg://user?id=955' in text
        assert '赠送了一份注册资格' in text
        assert all(s not in text for s in (code, '领取码', '密码', '用户组', '账号：', '《', 'https://t.me/'))
        buttons = env.tg.actions(GROUP, mid)
        assert len(buttons) == 1 and buttons[0]['text'] == '领取并注册'
        assert parse_qs(urlsplit(buttons[0]['url']).query)['start'] == [code]
        assert grant(env)['gift_days'] == 45 and not grant(env)['used_at']
        assert not env.members.find_by_telegram(str(TARGET))
        await click(env, action, mid)
        assert audit_count(env) == 1 and grant(env)['gift_code'] == code
        assert env.tg.text(GROUP, mid) == text
    asyncio.run(run())


def test_recipient_private_registers_once_with_saved_terms_and_binding(env):
    env.cfg['register_days'] = 37
    async def run():
        await issue(env)
        code = grant(env)['gift_code']
        env.cfg['register_days'] = 99
        mid = await command(env, '/start ' + code, chat=TARGET, user=TARGET)
        assert '用户名' in env.tg.text(TARGET, mid) and not grant(env)['used_at']
        await command(env, 'GiftRecipient', chat=TARGET, user=TARGET)
        await confirm_registration_password(env)
        member = env.members.find_by_telegram(str(TARGET))
        assert member and member['username'] == 'GiftRecipient' and member['group_id'] == 'standard'
        assert abs(member['expires_at'] - time.time() - 37 * 86400) < 10
        assert grant(env)['used_at']
        mid = await command(env, '/start ' + code, chat=TARGET, user=TARGET)
        assert '已经有账号' in env.tg.text(TARGET, mid)
        assert env.members.find_by_telegram(str(TARGET))['emby_user_id'] == member['emby_user_id']
    asyncio.run(run())


@pytest.mark.parametrize('own_grant', [False, True])
def test_forwarded_link_rejected_without_consuming_either_grant(env, own_grant):
    async def run():
        await issue(env)
        if own_grant:
            env.bot._registration.issue_gift(str(OTHER), 'test')
        setup_gate(env)
        mid = await command(env, '/start ' + grant(env)['gift_code'], chat=OTHER, user=OTHER)
        assert '不属于' in env.tg.text(OTHER, mid)
        assert '重新核实' not in str(env.tg.actions(OTHER, mid))
        assert not grant(env)['used_at'] and not env.bot._gift_claims
        assert not env.bot._pending.get(str(OTHER))
        if own_grant:
            assert not grant(env, OTHER)['used_at']
    asyncio.run(run())


@pytest.mark.parametrize('case', ['revoked', 'used', 'channel_closed', 'group_removed', 'registered'])
def test_private_claim_freshly_rejects_invalid_qualification(env, case):
    async def run():
        await issue(env)
        code = grant(env)['gift_code']
        if case == 'revoked':
            env.bot._registration.revoke_grant(str(TARGET))
        elif case == 'used':
            env.bot._registration.consume(env.bot._registration.resolve(str(TARGET), code), 'already')
        elif case == 'channel_closed':
            env.cfg['allow_admin_grant'] = False
        elif case == 'group_removed':
            env.db.execute("UPDATE admin_grants SET gift_group_id='missing'")
        else:
            env.members.upsert('created', 'ExistingGift', {'group_id': 'standard'})
            env.members.bind_telegram('created', str(TARGET))
        mid = await command(env, '/start ' + code, chat=TARGET, user=TARGET)
        assert '❌' in env.tg.text(TARGET, mid)
        assert str(TARGET) not in env.bot._pending
    asyncio.run(run())


@pytest.mark.parametrize('case', ['other_admin', 'ordinary', 'demoted', 'wrong_group', 'anonymous', 'wrong_message', 'expired'])
def test_gift_confirmation_authority_and_context_fail_closed(env, case):
    async def run():
        mid, action = await confirm_screen(env)
        user = SECOND if case == 'other_admin' else VIEWER if case == 'ordinary' else ADMIN
        chat = GROUP - 1 if case == 'wrong_group' else GROUP
        if case == 'demoted':
            env.members.upsert('admin', 'Operator', {'roles': []})
        if case == 'expired':
            panel = env.bot._admin_panels[(str(GROUP), mid)]
            pending = panel['pending']
            panel['pending'] = (pending[0], time.time() - 1, pending[2])
        if case == 'anonymous':
            await env.bot._dispatch_update({'callback_query': {'id': 'cb', 'data': action,
                'from': {'id': ADMIN}, 'message': {'chat': {'id': GROUP, 'type': 'supergroup'},
                'message_id': mid, 'sender_chat': {'id': GROUP}}}})
        else:
            await click(env, action, mid + 1 if case == 'wrong_message' else mid, chat=chat, user=user)
        assert grant(env) is None and audit_count(env) == 0
        if case in ('wrong_group', 'anonymous', 'demoted', 'other_admin', 'ordinary', 'wrong_message'):
            assert any(p.get('text') for method, p in env.tg.calls if method == 'answerCallbackQuery')
    asyncio.run(run())


def test_existing_account_card_rejects_gift_without_writes(env):
    async def run():
        mid = await command(env, '/kk ViewerA')
        before = env.db.query('SELECT * FROM members')
        env.tg.calls.clear()
        await click(env, 'admin_gift', mid)
        assert env.db.query('SELECT * FROM members') == before
        assert not env.bot._registration.list_grants() and audit_count(env) == 0
        assert all(method == 'answerCallbackQuery' for method, _ in env.tg.calls)
        assert '已有账号' in str(env.tg.calls)
    asyncio.run(run())


def test_existing_account_between_confirmation_and_issue_is_clear_rejection(env):
    async def run():
        mid, action = await confirm_screen(env)
        env.members.upsert('new', 'ExistingGift', {'group_id': 'standard'})
        env.members.bind_telegram('new', str(TARGET))
        await click(env, action, mid)
        assert '已有账号' in env.tg.text(GROUP, mid) and not grant(env)
    asyncio.run(run())


@pytest.mark.parametrize('change', ['days', 'group', 'bot_username'])
def test_missing_link_or_changed_terms_do_not_issue(env, change):
    async def run():
        mid, action = await confirm_screen(env)
        if change == 'days':
            env.cfg['register_days'] = 123
        elif change == 'group':
            env.cfg['default_group_id'] = 'whitelist'
        else:
            env.bot._bot_username = ''
        await click(env, action, mid)
        assert not grant(env)
        assert '未发放' in env.tg.text(GROUP, mid) or '已变化' in env.tg.text(GROUP, mid)
    asyncio.run(run())


def test_duplicate_admin_issue_and_failed_announcement_reuse_same_grant(env):
    async def run():
        mid, action = await confirm_screen(env)
        env.tg.edit_error = 'ReadTimeout: 请求失败'
        await click(env, action, mid)
        code = grant(env)['gift_code']
        await click(env, action, mid)
        assert audit_count(env) == 1
        env.tg.edit_error = ''
        new, _ = await issue(env, user=SECOND)
        assert grant(env)['gift_code'] == code and audit_count(env) == 1
        assert len(env.tg.actions(GROUP, new)) == 1
    asyncio.run(run())


def test_private_admin_legacy_entry_still_works(env):
    async def run():
        mid, _ = await issue(env, chat=ADMIN)
        assert '赠送资格已准备好' in env.tg.text(ADMIN, mid)
        assert grant(env)['gift_code'] in env.tg.text(ADMIN, mid)
    asyncio.run(run())


def test_mention_label_html_escaped_without_invented_recipient_name(env):
    env.db.execute('UPDATE members SET tg_username=? WHERE emby_user_id=?', ('bad<&"name', 'admin'))
    async def run():
        mid, _ = await issue(env)
        text = env.tg.text(GROUP, mid)
        assert '@bad&lt;&amp;&quot;name' in text
        assert '>TG 955</a>' in text
    asyncio.run(run())


def test_membership_recheck_resumes_same_gift_and_only_consumes_after_registration(env):
    async def run():
        await issue(env)
        state = setup_gate(env)
        code = grant(env)['gift_code']
        mid = await command(env, '/start ' + code, chat=TARGET, user=TARGET)
        assert '请先加入' in env.tg.text(TARGET, mid) and not grant(env)['used_at']
        await click(env, 'membership_recheck', mid, chat=TARGET, user=TARGET)
        assert '请先加入' in env.tg.text(TARGET, mid) and not grant(env)['used_at']
        state['status'] = 'member'
        resumed = await click(env, 'membership_recheck', mid, chat=TARGET, user=TARGET)
        assert resumed == mid and '用户名' in env.tg.text(TARGET, mid)
        assert not env.bot._gift_claims and not grant(env)['used_at']
        await command(env, 'JoinedGift', chat=TARGET, user=TARGET)
        await confirm_registration_password(env)
        assert grant(env)['used_at'] and env.members.find_by_telegram(str(TARGET))
    asyncio.run(run())


@pytest.mark.parametrize('case', ['expired', 'revoked', 'used', 'cancelled', 'foreign_tap'])
def test_gate_resume_revalidates_intent_and_cannot_steal_or_replay(env, case):
    async def run():
        await issue(env)
        state = setup_gate(env)
        mid = await command(env, '/start ' + grant(env)['gift_code'], chat=TARGET, user=TARGET)
        key = (str(TARGET), mid)
        if case == 'expired':
            c = env.bot._gift_claims[key]
            env.bot._gift_claims[key] = (time.time() - 1, c[1], c[2])
        elif case == 'revoked':
            env.bot._registration.revoke_grant(str(TARGET))
        elif case == 'used':
            env.bot._registration.consume(env.bot._registration.resolve(str(TARGET)), 'already')
        elif case == 'cancelled':
            await command(env, '/cancel', chat=TARGET, user=TARGET)
        state['status'] = 'member'
        await click(env, 'membership_recheck', mid, chat=TARGET, user=OTHER if case == 'foreign_tap' else TARGET)
        assert str(TARGET) not in env.bot._pending
        assert not env.members.find_by_telegram(str(TARGET))
        if case in ('expired', 'revoked', 'used', 'foreign_tap'):
            assert '❌' in env.tg.text(TARGET, mid)
    asyncio.run(run())


def test_anonymous_kk_in_allowed_group_is_explicit_refusal(env):
    async def run():
        await env.bot._dispatch_update({'message': {'chat': {'id': GROUP, 'type': 'supergroup'},
            'from': {'id': ADMIN}, 'sender_chat': {'id': GROUP}, 'text': '/kk 955', 'message_id': 22}})
        assert not grant(env)
        assert any('匿名管理身份' in p.get('text', '') for _, p in env.tg.calls)
        assert not env.bot._admin_panels
    asyncio.run(run())


def test_two_admins_same_target_issue_only_one_qualification(env):
    async def run():
        a, b = await asyncio.gather(confirm_screen(env), confirm_screen(env, user=SECOND))
        await asyncio.gather(click(env, a[1], a[0]), click(env, b[1], b[0], user=SECOND))
        assert len(env.bot._registration.list_grants()) == 1 and audit_count(env) == 1
        assert env.tg.actions(GROUP, a[0])[0]['url'] == env.tg.actions(GROUP, b[0])[0]['url']
    asyncio.run(run())


def test_emby_create_failure_retains_gift_for_retry(env, monkeypatch):
    original = env.bot._emby.create_user
    async def fail(*args, **kwargs):
        raise RuntimeError('local simulated creation failure')
    async def run():
        await issue(env)
        code = grant(env)['gift_code']
        await command(env, '/start ' + code, chat=TARGET, user=TARGET)
        monkeypatch.setattr(env.bot._emby, 'create_user', fail)
        await command(env, 'FailedGift', chat=TARGET, user=TARGET)
        await confirm_registration_password(env)
        assert not grant(env)['used_at'] and not env.members.find_by_telegram(str(TARGET))
        monkeypatch.setattr(env.bot._emby, 'create_user', original)
        await command(env, '/start ' + code, chat=TARGET, user=TARGET)
        await command(env, 'RetriedGift', chat=TARGET, user=TARGET)
        await confirm_registration_password(env)
        assert grant(env)['used_at'] and env.members.find_by_telegram(str(TARGET))
    asyncio.run(run())


def test_unknown_gate_and_deleted_guide_keep_recipient_intent(env):
    async def run():
        await issue(env)
        state = setup_gate(env)
        state['status'] = 'unexpected'
        mid = await command(env, '/start ' + grant(env)['gift_code'], chat=TARGET, user=TARGET)
        assert '无法核实' in env.tg.text(TARGET, mid)
        env.tg.edit_error = 'Bad Request: message to edit not found'
        new = await click(env, 'membership_recheck', mid, chat=TARGET, user=TARGET)
        assert new != mid and (str(TARGET), new) in env.bot._gift_claims
        env.tg.edit_error = ''
        state['status'] = 'member'
        await click(env, 'membership_recheck', new, chat=TARGET, user=TARGET)
        assert '用户名' in env.tg.text(TARGET, new) and not grant(env)['used_at']
    asyncio.run(run())


def test_username_interrupted_by_gate_restores_gift_not_username_side_effect(env):
    async def run():
        await issue(env)
        code = grant(env)['gift_code']
        mid = await command(env, '/start ' + code, chat=TARGET, user=TARGET)
        state = setup_gate(env)
        await command(env, 'NotYetCreated', chat=TARGET, user=TARGET)
        assert not env.members.find_by_telegram(str(TARGET))
        state['status'] = 'member'
        await click(env, 'membership_recheck', mid, chat=TARGET, user=TARGET)
        assert '用户名' in env.tg.text(TARGET, mid)
        assert not grant(env)['used_at']
        await command(env, 'ReallyCreated', chat=TARGET, user=TARGET)
        await confirm_registration_password(env)
        assert env.members.find_by_telegram(str(TARGET))['username'] == 'ReallyCreated'
    asyncio.run(run())
