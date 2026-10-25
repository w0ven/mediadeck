"""Private Bot prerequisite uses server evidence, not registration/client claims."""
import asyncio
import json
import time

import pytest

from app.modules import telegram_private
from tests.lottery_legacy_helpers import legacy_create
from tests.lottery_private_helpers import private_start
from tests.test_lottery import ADMIN, GUEST, MEMBER, config, create, draw, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_lottery_count import counted, enter, fees, funded, saved, snapshot, worker
from tests.test_lottery_device import assert_public
from tests.test_lottery_eligibility import MANUAL, POINTS


@pytest.fixture
def e(request):
    env = request.getfixturevalue('lottery_env')
    env.service.db.execute("DELETE FROM meta WHERE key LIKE 'telegram.private:%' OR key LIKE 'telegram.panel:%'")
    return env


def update(e, tg=GUEST, status=None, number=100, date=None):
    event = {'chat': {'id': tg, 'type': 'private'}, 'date': int(time.time()) if date is None else date,
             'from': {'id': tg, 'is_bot': False, 'first_name': 'Fixture'}}
    if status:
        event['new_chat_member'] = {'status': status, 'user': {'id': int(telegram_private.identity(e.service.bot)), 'is_bot': True}}
        return {'update_id': number, 'my_chat_member': event}
    event.update(message_id=number + 1, text='/start')
    return {'update_id': number, 'message': event}


def observe(e, value):
    telegram_private.observe(e.service.db, telegram_private.identity(e.service.bot), value)


def ready(e, tg=GUEST):
    return telegram_private.ready(e.service.db, telegram_private.identity(e.service.bot), tg)


@pytest.mark.parametrize('tg,fee', [(GUEST, 0), (MEMBER, 0), (ADMIN, 0), (MEMBER, 40), (ADMIN, 40)])
def test_unknown_private_chat_blocks_before_any_debit_or_entry(e, tg, fee):
    funded(e)
    funded(e, 'u1')
    lid = create(e, counted(audience='member' if fee else 'all', entry_fee=fee))
    view = req(e, 'GET', '/' + lid, tg).json()
    assert view['bot_private_required'] and not view['bot_private_ready']
    assert view['bot_private_reason'] == telegram_private.REASON
    assert view['bot_start_url'].endswith('?start=lottery_' + lid)
    if tg != ADMIN:
        assert_public(view)
    before = snapshot(e, lid)
    for extra in ({}, {'bot_private_ready': True, 'started': True, 'require_private_start': False, 'legacy': True}):
        result = enter(e, lid, tg, expected_entry_fee=fee, **extra)
        assert result.status_code == 400 and result.json()['detail'] == telegram_private.REASON
    assert snapshot(e, lid) == before and not fees(e, lid)
    assert e.service.points.balance('u1') == e.service.points.balance('u2') == 100
    private_start(e.service, tg)
    assert enter(e, lid, tg, expected_entry_fee=fee).json()['joined']
    assert not req(e, 'GET', '/' + lid, tg).json()['bot_private_required']


def test_real_dispatch_private_start_links_back_and_survives_connection_restart(e):
    lid = create(e, counted())
    value = update(e)
    value['message']['text'] = '/start lottery_' + lid
    asyncio.run(e.service.bot._dispatch_update(value))
    assert ready(e)
    replies = [p for m, p in e.calls if m in ('sendMessage', 'editMessageText')]
    assert any('返回活动报名' in p['text'] and p['reply_markup']['inline_keyboard'][0][0]['url'].endswith(
        '?startapp=lottery_' + lid) for p in replies)
    with worker(e) as restarted:
        assert telegram_private.ready(restarted.db, telegram_private.identity(restarted.bot), GUEST)
    assert enter(e, lid, GUEST).status_code == 200


@pytest.mark.parametrize('bad', ['group', 'channel', 'missing_type', 'other_user', 'bot', 'anonymous', 'forward', 'date', 'id', 'boolean'])
def test_untrusted_or_nonprivate_events_are_not_evidence(e, bad):
    value = update(e)
    msg = value['message']
    if bad in ('group', 'channel'):
        msg['chat']['type'] = bad
    elif bad == 'missing_type':
        msg['chat'].pop('type')
    elif bad == 'other_user':
        msg['from']['id'] = MEMBER
    elif bad == 'bot':
        msg['from']['is_bot'] = True
    elif bad == 'anonymous':
        msg['sender_chat'] = {'id': -100123}
    elif bad == 'forward':
        msg['is_automatic_forward'] = True
    elif bad == 'date':
        msg['date'] = int(time.time()) + 3600
    elif bad == 'id':
        value['update_id'] = '100'
    else:
        msg['from']['id'] = True
    observe(e, value)
    assert not ready(e)


def test_private_block_ordering_and_restoration_overrides_legacy_menu(e):
    bot_id = telegram_private.identity(e.service.bot)
    e.service.db.execute('INSERT INTO meta(key,value) VALUES(?,?)', (f'telegram.panel:{bot_id}:{GUEST}', '123'))
    assert ready(e)
    block = update(e, status='kicked', number=200)
    asyncio.run(e.service.bot._dispatch_update(block))  # private event is recorded before group-membership handling
    assert not ready(e)
    observe(e, update(e, number=199, date=block['my_chat_member']['date']))
    assert not ready(e)
    observe(e, update(e, status='member', number=201, date=block['my_chat_member']['date']))
    assert ready(e)
    observe(e, block)  # delayed duplicate must not revoke the newer interaction
    assert ready(e)


@pytest.mark.parametrize('key_prefix', ['other_bot', 'group', 'plain_member'])
def test_old_evidence_is_bot_and_private_chat_scoped(e, key_prefix):
    bot_id = telegram_private.identity(e.service.bot)
    name = {'other_bot': f'telegram.panel:999999:{GUEST}', 'group': f'telegram.panel:{bot_id}:g:-100:{GUEST}',
            'plain_member': 'unrelated'}[key_prefix]
    e.service.db.execute('INSERT INTO meta(key,value) VALUES(?,?)', (name, '123'))
    assert not ready(e)
    assert not ready(e, MEMBER)  # registered is not equivalent to having a private Bot chat


def test_in_transaction_recheck_when_blocked_during_group_lookup(e, monkeypatch):
    private_start(e.service, MEMBER)
    funded(e)
    lid = create(e, counted(audience='member', entry_fee=40))
    original = e.service.bot._call

    async def blocking(method, payload=None, **kw):
        result = await original(method, payload, **kw)
        if method == 'getChatMember':
            observe(e, update(e, MEMBER, status='kicked', number=200))
        return result
    monkeypatch.setattr(e.service.bot, '_call', blocking)
    assert enter(e, lid, MEMBER, expected_entry_fee=40).status_code == 400
    assert not fees(e, lid) and not snapshot(e, lid)['lottery_entries']
    assert e.service.points.balance('u2') == 100


def test_already_joined_and_awarded_user_not_recharged_or_revoked_after_block(e):
    private_start(e.service, MEMBER)
    funded(e)
    lid = create(e, counted(audience='member', entry_fee=40, prizes=[POINTS]))
    assert enter(e, lid, MEMBER, expected_entry_fee=40).json()['joined']
    observe(e, update(e, MEMBER, status='kicked', number=200))
    retry = enter(e, lid, MEMBER, expected_entry_fee=0).json()
    assert retry['joined'] and not retry['bot_private_required'] and not retry['bot_private_ready']
    assert len(fees(e, lid)) == 1 and e.service.points.balance('u2') == 60
    e.service.draw(lid)
    award = snapshot(e, lid)['lottery_awards']
    e.service.deliver_awards(lid)
    e.replies.append({'error_code': 403, 'description': 'Forbidden: bot was blocked by the user'})
    asyncio.run(e.service.deliver_dms(lid))
    assert snapshot(e, lid)['lottery_dms'][0]['state'] == 'unreachable'
    assert snapshot(e, lid)['lottery_awards'][0]['id'] == award[0]['id']
    assert e.service.points.balance('u2') == 70 and not ready(e, MEMBER)
    private_start(e.service, MEMBER, update_id=201)
    assert ready(e, MEMBER)


@pytest.mark.parametrize('action', ['new', 'copy', 'save'])
def test_server_owns_policy_client_cannot_disable_new_gate(e, action):
    raw = counted(require_private_start=False, legacy=True)
    if action == 'new':
        lid = create(e, raw)
    else:
        old = legacy_create(e, config(prizes=[MANUAL]), publish=False)
        if action == 'copy':
            lid = req(e, 'POST', f'/{old}/copy', json={'require_private_start': False}).json()['id']
        else:
            lid = old
        result = req(e, 'PUT', '/' + lid, json={**raw, 'revision': 1})
        assert result.status_code == 200
        assert req(e, 'POST', f'/{lid}/publish', json={'revision': 2, 'require_private_start': False}).status_code == 200
    assert e.service.row(lid)['config']['require_private_start'] is True
    assert enter(e, lid, GUEST).status_code == 400


@pytest.mark.parametrize('state', ['draft', 'published', 'settled'])
def test_old_activities_are_not_retrofitted(e, state):
    lid = legacy_create(e, config(prizes=[MANUAL]), publish=state != 'draft')
    if state == 'draft':
        assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1, 'require_private_start': True}).status_code == 200
    before = saved(e, lid)
    notices = snapshot(e, lid)['lottery_notices']
    assert 'require_private_start' not in json.loads(before['config_json'])
    view = req(e, 'GET', '/' + lid, GUEST).json()
    assert not view['bot_private_required'] and not view['bot_private_ready'] and not view['bot_private_reason']
    assert '先私聊' not in view['rules']
    assert enter(e, lid, GUEST).status_code == 200
    if state == 'settled':
        draw(e, lid)
    else:
        assert saved(e, lid) == before
    assert snapshot(e, lid)['lottery_notices'][:1] == notices


def test_delayed_delivery_failure_does_not_override_new_private_start(e):
    private_start(e.service, GUEST)
    bot_id = telegram_private.identity(e.service.bot)
    original = telegram_private.marker(e.service.db, bot_id, GUEST)
    private_start(e.service, GUEST, update_id=200)
    telegram_private.delivery_blocked(e.service.db, bot_id, GUEST, original, int(time.time()))
    assert ready(e)


def test_update_provenance_does_not_grant_access_to_a_different_bot(e, monkeypatch):
    async def noop(*args, **kw):
        return None
    monkeypatch.setattr(e.service.bot, '_handle_message', noop)
    monkeypatch.setattr(e.service.bot, '_sync_chat_commands', noop)
    asyncio.run(e.service.bot._dispatch_update(update(e), source_bot_id='999999'))
    assert not ready(e)
    assert telegram_private.ready(e.service.db, '999999', GUEST)
