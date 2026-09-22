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
