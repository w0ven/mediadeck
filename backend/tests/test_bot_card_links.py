"""Actual Bot menu/command flows with temporary cards and an in-memory transport."""
import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
from test_tg_interaction_context import ADMIN, GROUP, VIEWER, click, command
from test_tg_interaction_context import env as interaction_env  # noqa: F401


@pytest.fixture
def env(request):
    environment = request.getfixturevalue('interaction_env')
    environment.bot._check_bot_identity()
    environment.bot._bot_username = 'MediaDeckDemoBot'
    environment.documents = []

    async def document(method, fields, files, timeout=40):
        environment.documents.append((method, fields, files))
        return {'message_id': 9000}

    environment.bot._call_multipart = document
    return environment


def cards(e):
    return e.db.query('SELECT * FROM redeem_codes ORDER BY id')


def generated(e):
    return [payload['text'] for method, payload in e.tg.calls if method == 'sendMessage'
            and str(payload.get('chat_id')) == str(ADMIN) and '已生成' in payload.get('text', '')]


async def open_generator(e):
    mid = await command(e, '/manage', chat=ADMIN, user=ADMIN)
    await click(e, 'admin_code', mid, chat=ADMIN, user=ADMIN)
    return mid


def choice(e, mid, mode):
    return next(button['callback_data'] for button in e.tg.actions(ADMIN, mid)
                if button.get('callback_data', '').startswith('admin_code_mode:')
                and button['callback_data'].endswith(':' + mode))


@pytest.mark.parametrize('links', [False, True])
def test_screenshot_menu_flow_selects_format_before_typing_three_arguments(env, links):
    async def run():
        mid = await open_generator(env)
        assert '仅卡密' in env.tg.text(ADMIN, mid)
        assert any('卡密＋领取链接' in button['text'] for button in env.tg.actions(ADMIN, mid))
        if links:
            await click(env, choice(env, mid, 'links'), mid, chat=ADMIN, user=ADMIN)
        assert not cards(env)  # opening/selecting a format never mints a card
        await command(env, 'standard 30 1', chat=ADMIN, user=ADMIN)
        rows = cards(env)
        assert len(rows) == 1 and len(generated(env)) == 1
        body = generated(env)[0]
        assert rows[0]['code'] in body
        link = env.bot._start_link(rows[0]['code'])
        assert (link in body) is links
        assert str(ADMIN) not in env.bot._pending
        audit = str(env.db.query("SELECT detail FROM audit_log WHERE action='redeem.generate'"))
        assert rows[0]['code'] not in audit and link not in audit
    asyncio.run(run())


def test_switching_back_and_repeated_or_stale_callbacks_cannot_mint_cards(env):
    async def run():
        mid = await open_generator(env)
        old = choice(env, mid, 'links')
        await click(env, old, mid, chat=ADMIN, user=ADMIN)
        await click(env, old, mid, chat=ADMIN, user=ADMIN)
        await click(env, choice(env, mid, 'plain'), mid, chat=ADMIN, user=ADMIN)
        assert not cards(env)
        await command(env, 'standard 30 1', chat=ADMIN, user=ADMIN)
        assert len(cards(env)) == 1 and 'https://t.me/' not in generated(env)[0]
        await click(env, old, mid, chat=ADMIN, user=ADMIN)
        await command(env, 'standard 30 1', chat=ADMIN, user=ADMIN)
        assert len(cards(env)) == 1
    asyncio.run(run())


def test_returning_to_admin_cancels_card_input(env):
    async def run():
        mid = await open_generator(env)
        await click(env, choice(env, mid, 'links'), mid, chat=ADMIN, user=ADMIN)
        await click(env, 'admin', mid, chat=ADMIN, user=ADMIN)
        await command(env, 'standard 30 1', chat=ADMIN, user=ADMIN)
        assert not cards(env)
    asyncio.run(run())


def test_revoked_admin_cannot_select_or_submit_card_generation(env):
    async def run():
        mid = await open_generator(env)
        data = choice(env, mid, 'links')
        env.members.set_roles('admin', [])
        await click(env, data, mid, chat=ADMIN, user=ADMIN)
        await command(env, 'standard 30 1', chat=ADMIN, user=ADMIN)
        await command(env, '/code standard 30 1 links', chat=VIEWER, user=VIEWER)
        assert not cards(env)
    asyncio.run(run())


@pytest.mark.parametrize('entry', ['menu', 'command'])
def test_unknown_bot_identity_is_rejected_before_minting(env, entry):
    async def run():
        if entry == 'menu':
            mid = await open_generator(env)
            await click(env, choice(env, mid, 'links'), mid, chat=ADMIN, user=ADMIN)
        env.bot._bot_username = ''
        text = 'standard 30 1' if entry == 'menu' else '/code standard 30 1 links'
        await command(env, text, chat=ADMIN, user=ADMIN)
        assert not cards(env) and not env.documents
        assert any('未生成卡密' in payload.get('text', '') for method, payload in env.tg.calls)
    asyncio.run(run())


def test_command_links_resolve_to_the_existing_issued_cards(env):
    env.cfg['allow_redeem'] = True

    async def run():
        await command(env, '/code standard 30 3 links', chat=ADMIN, user=ADMIN)
        rows = cards(env)
        assert len(rows) == 3
        for card in rows:
            link = env.bot._start_link(card['code'])
            assert link in generated(env)[0]
            code = parse_qs(urlsplit(link).query)['start'][0]
            admission = env.bot._registration.resolve('987654', code)
            assert admission.allowed and admission.via == 'redeem'
            assert admission.credential == card['code'] and card['status'] == 'unused'
    asyncio.run(run())


def test_group_command_and_callback_never_generate_or_publish_credentials(env):
    async def run():
        mid = await command(env, '/code standard 30 1 links', chat=GROUP, user=ADMIN)
        await click(env, 'admin_code', mid, chat=GROUP, user=ADMIN)
        await click(env, 'admin_code_mode:forged:links', mid, chat=GROUP, user=ADMIN)
        assert not cards(env) and not env.documents
        assert not generated(env)
    asyncio.run(run())


@pytest.mark.parametrize('links', [False, True])
def test_large_batch_uses_one_complete_document_instead_of_truncated_messages(env, links):
    async def run():
        suffix = ' links' if links else ''
        await command(env, '/code standard 30 500' + suffix, chat=ADMIN, user=ADMIN)
        rows = cards(env)
        assert len(rows) == 500
        assert len(env.documents) == 1 and not generated(env)
        method, fields, files = env.documents[0]
        assert method == 'sendDocument' and fields['chat_id'] == str(ADMIN)
        _, content, mime = files['document']
        text = content.decode()
        assert mime.startswith('text/plain')
        for card in rows:
            assert card['code'] in text
            assert (env.bot._start_link(card['code']) in text) is links
    asyncio.run(run())


def test_menu_validation_keeps_format_selection_for_a_corrected_submission(env):
    async def run():
        mid = await open_generator(env)
        await click(env, choice(env, mid, 'links'), mid, chat=ADMIN, user=ADMIN)
        await command(env, 'missing-group 30 1', chat=ADMIN, user=ADMIN)
        assert not cards(env)
        await command(env, 'standard 30 1', chat=ADMIN, user=ADMIN)
        assert len(cards(env)) == 1
        assert env.bot._start_link(cards(env)[0]['code']) in generated(env)[0]
    asyncio.run(run())


def test_interactive_code_picker_and_admin_preset_buttons_and_library_stats(env):
    async def run():
        # 1. Interactive code picker mints without typing
        mid = await open_generator(env)
        actions = env.tg.actions(ADMIN, mid)
        nonce = next(b['callback_data'].split(':')[1] for b in actions if b.get('callback_data', '').startswith('admin_code_set:'))
        await click(env, f'admin_code_set:{nonce}:g:whitelist', mid, chat=ADMIN, user=ADMIN)
        await click(env, f'admin_code_set:{nonce}:d:90', mid, chat=ADMIN, user=ADMIN)
        await click(env, f'admin_code_set:{nonce}:c:5', mid, chat=ADMIN, user=ADMIN)
        await click(env, f'admin_code_mode:{nonce}:links', mid, chat=ADMIN, user=ADMIN)
        await click(env, f'admin_code_mint:{nonce}', mid, chat=ADMIN, user=ADMIN)
        rows = cards(env)
        assert len(rows) == 5
        assert all(r['group_id'] == 'whitelist' and r['days'] == 90 for r in rows)
        # Double-clicking mint after completion is rejected
        await click(env, f'admin_code_mint:{nonce}', mid, chat=ADMIN, user=ADMIN)
        assert len(cards(env)) == 5

        # 2. Admin preset renew & score buttons on /kk card
        before_exp = env.members.get('u1')['expires_at_effective']
        before_pts = env.bot._points.balance('u1')
        kmid = await command(env, '/kk ViewerA', chat=ADMIN, user=ADMIN)
        await click(env, 'admin_renew', kmid, chat=ADMIN, user=ADMIN)
        r_btn = next(b['callback_data'] for b in env.tg.actions(ADMIN, kmid)
                     if b.get('callback_data', '').startswith('admin_renew_pick:') and b['callback_data'].endswith(':15'))
        await click(env, r_btn, kmid, chat=ADMIN, user=ADMIN)
        assert env.members.get('u1')['expires_at_effective'] == before_exp + 15 * 86400
        # Replay of consumed preset renew button does nothing
        await click(env, r_btn, kmid, chat=ADMIN, user=ADMIN)
        assert env.members.get('u1')['expires_at_effective'] == before_exp + 15 * 86400

        await click(env, 'admin_score', kmid, chat=ADMIN, user=ADMIN)
        s_btn = next(b['callback_data'] for b in env.tg.actions(ADMIN, kmid)
                     if b.get('callback_data', '').startswith('admin_score_pick:') and b['callback_data'].endswith(':300'))
        await click(env, s_btn, kmid, chat=ADMIN, user=ADMIN)
        assert env.bot._points.balance('u1') == before_pts + 300
        await click(env, s_btn, kmid, chat=ADMIN, user=ADMIN)
        assert env.bot._points.balance('u1') == before_pts + 300

        # 3. Private prompt input message is auto-deleted to keep chat clean
        await click(env, 'admin_find', kmid, chat=ADMIN, user=ADMIN)
        env.tg.calls.clear()
        await env.bot._dispatch_update({
            'message': {
                'message_id': 7788,
                'chat': {'id': ADMIN, 'type': 'private'},
                'from': {'id': ADMIN, 'is_bot': False},
                'text': 'ViewerA',
            }
        })
        assert any(m == 'deleteMessage' and p == {'chat_id': ADMIN, 'message_id': 7788} for m, p in env.tg.calls)

        # 4. Library stats view from nodes page and 3x2 home grid
        umid = await command(env, '/start', chat=VIEWER, user=VIEWER)
        home_rows = [[b['text'] for b in row] for row in env.tg.message(VIEWER, umid)['reply_markup']['inline_keyboard']]
        assert home_rows == [
            ['👤 个人信息', '📡 服务器线路'],
            ['📊 媒体库统计', '🏆 观影/积分榜'],
            ['🎬 求片中心', '🎒 背包与商城'],
        ]
        await click(env, 'me_nodes', umid, chat=VIEWER, user=VIEWER)
        nodes_text = env.tg.text(VIEWER, umid)
        assert '🟢 状态：<b>在线</b>' in nodes_text
        await click(env, 'library_stats', umid, chat=VIEWER, user=VIEWER)
        stats_text = env.tg.text(VIEWER, umid)
        assert '📊 <b>媒体库统计</b>' in stats_text and '影视总计' in stats_text and 'Emby' in stats_text

        # 5. WebP logo URL automatically falls back to .png sibling when Telegram refuses WebP sendPhoto
        env.cfg['menu_logo_url'] = 'https://images.example/peekpeek-M1-tg-banner-wide-plus.webp'
        orig_call = env.bot._call
        seen_photos = []
        async def webp_rejecting_call(method, payload=None, timeout=20):
            if method == 'sendPhoto':
                seen_photos.append(payload.get('photo'))
                if str(payload.get('photo', '')).endswith('.webp'):
                    return None
            return await orig_call(method, payload, timeout)
        env.bot._call = webp_rejecting_call
        pmid = await command(env, '/start', chat=VIEWER, user=VIEWER)
        assert seen_photos == [
            'https://images.example/peekpeek-M1-tg-banner-wide-plus.webp',
            'https://images.example/peekpeek-M1-tg-banner-wide-plus.png',
        ]
        assert (str(VIEWER), pmid) in env.bot._photo_panels

        # Subsequent /start reuses cached fallback without probing .webp again
        seen_photos.clear()
        pmid2 = await command(env, '/start', chat=VIEWER, user=VIEWER)
        assert seen_photos == ['https://images.example/peekpeek-M1-tg-banner-wide-plus.png']
        assert pmid2 != pmid
    asyncio.run(run())


def test_private_expired_admin_panel_and_buried_menus_always_reply_at_bottom(env):
    import time

    async def run():
        # 1. Expired private /kk card no longer traps subsequent plain-text messages
        kmid = await command(env, '/kk ViewerA', chat=ADMIN, user=ADMIN)
        env.bot._admin_panels[(str(ADMIN), kmid)]['expires'] = time.time() - 5
        await env.bot.send(ADMIN, '🔔 系统通知 1')
        env.tg.calls.clear()
        new_home = await command(env, '菜单', chat=ADMIN, user=ADMIN)
        assert new_home != kmid
        assert (str(ADMIN), kmid) not in env.bot._admin_panels
        assert any(m == 'sendMessage' for m, _ in env.tg.calls)

        # 2. Explicit reply to an expired admin card sends a fresh bottom card with admin buttons
        kmid2 = await command(env, '/kk ViewerA', chat=ADMIN, user=ADMIN)
        env.bot._admin_panels[(str(ADMIN), kmid2)]['expires'] = time.time() - 5
        env.tg.calls.clear()
        exp_mid = await command(env, '30', chat=ADMIN, user=ADMIN, reply=kmid2)
        assert exp_mid != kmid2
        assert '管理会话已过期' in env.tg.text(ADMIN, exp_mid)
        assert any(b.get('callback_data') == 'admin_find' for b in env.tg.actions(ADMIN, exp_mid))

        # 3. Buried admin_find prompt surfaces result at the bottom after intervening notices
        await click(env, 'admin_find', exp_mid, chat=ADMIN, user=ADMIN)
        notice_mid = await env.bot.send_message(ADMIN, '📋 新求片待审')
        assert notice_mid > exp_mid
        env.tg.calls.clear()
        await env.bot._dispatch_update({
            'message': {
                'message_id': 9901,
                'chat': {'id': ADMIN, 'type': 'private'},
                'from': {'id': ADMIN, 'is_bot': False},
                'text': 'ViewerA',
            }
        })
        cur_mid = env.bot._panel[str(ADMIN)]
        assert cur_mid > notice_mid
        assert 'ViewerA' in env.tg.text(ADMIN, cur_mid)
        assert any(m == 'deleteMessage' and p == {'chat_id': ADMIN, 'message_id': 9901} for m, p in env.tg.calls)
    asyncio.run(run())


def test_transport_retries_transient_reset_and_command_sync_does_not_hold_chat_lock(env):
    import httpx

    from app.modules.telegram import TelegramBot

    async def run():
        # 1. Transient connection reset on _call closes pooled client and retries once
        bot = TelegramBot(lambda: {'enabled': True, 'bot_token': '123456:synthetic-token'}, env.members)
        attempts = 0

        class FakeResp:
            def json(self):
                return {'ok': True, 'result': {'id': 123456, 'username': 'demo_bot'}}

        class FakeClient:
            def __init__(self, fail_first: bool):
                self.fail_first = fail_first
                self.closed = False

            async def post(self, url, json=None, timeout=20):
                nonlocal attempts
                attempts += 1
                if self.fail_first:
                    raise httpx.RemoteProtocolError('Server disconnected without sending a response.')
                return FakeResp()

            async def aclose(self):
                self.closed = True

        clients = [FakeClient(True), FakeClient(False)]
        idx = 0

        async def fake_get_client():
            nonlocal idx
            if bot._http is None:
                bot._http = clients[idx]
                idx += 1
            return bot._http

        bot._client = fake_get_client
        res = await bot._call('sendMessage', {'chat_id': 1, 'text': 'hi'})
        assert res == {'id': 123456, 'username': 'demo_bot'}
        assert attempts == 2 and clients[0].closed is True

        # 2. _sync_chat_commands runs outside _chat_locks[chat_key]
        lock_held_during_sync = None
        orig_sync = env.bot._sync_chat_commands

        async def probe_sync(*args, **kwargs):
            nonlocal lock_held_during_sync
            lock_held_during_sync = env.bot._lock_for(str(VIEWER)).locked()
            return await orig_sync(*args, **kwargs)

        env.bot._sync_chat_commands = probe_sync
        await env.bot._dispatch_update({
            'message': {
                'message_id': 9902,
                'chat': {'id': VIEWER, 'type': 'private'},
                'from': {'id': VIEWER, 'is_bot': False, 'first_name': 'V'},
                'text': '/start',
            }
        })
        assert lock_held_during_sync is False
    asyncio.run(run())


