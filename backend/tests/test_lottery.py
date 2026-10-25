"""Lottery integration: real SQLite/services/APIs, mock Telegram only."""
import asyncio
import copy
import hashlib
import ipaddress
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.lottery import LotteryService
from app.modules.report_delivery import CALL_DELIVERY, failure
from app.modules.telegram import _CALL_ERROR, TelegramBot
from tests.lottery_captcha_helpers import captcha_args, install
from tests.lottery_legacy_helpers import legacy_create
from tests.lottery_private_helpers import private_start
from tests.test_miniapp import TOKEN, headers

ADMIN, MEMBER, GUEST = 888000111, 888000222, 888000333
CHAT = '-1001234567890'
BASE = '/api/app/lottery'


def synthetic_public_ip():
    # Generated fixture data, never contacted; no real deployment/IP fixture.
    while True:
        ip = ipaddress.IPv4Address(secrets.randbits(32))
        if ip.is_global and not ip.is_multicast and not ip.is_reserved:
            return str(ip)


def browser_id(user):
    return hashlib.sha256(f'local-test-browser:{user}'.encode()).hexdigest()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('MEDIADECK_LOTTERY_IP_MODE', 'cloudflare_protected')
    turnstile = install(monkeypatch)
    ips = {}

    def ip_for(tg):
        return ips.setdefault(str(tg), synthetic_public_ip())
    calls, replies = [], []
    ctl = SimpleNamespace(main=True, membership={'status': 'member'}, forum=True)

    async def fake(self, method, payload=None, **kw):
        calls.append((method, copy.deepcopy(payload)))
        CALL_DELIVERY.set(None)
        _CALL_ERROR.set('')
        if method == 'getMe':
            return {'id': 123456, 'username': 'example_lottery_bot', 'has_main_web_app': ctl.main}
        if method == 'getChat':
            return {'id': int(CHAT), 'type': 'supergroup', 'is_forum': ctl.forum}
        if method == 'getChatMember':
            return {'status': 'administrator'} if payload['user_id'] == 123456 else ctl.membership
        if method in ('sendMessage', 'editMessageText'):
            if replies:
                body = replies.pop(0)
                CALL_DELIVERY.set(failure(body))
                _CALL_ERROR.set(body.get('description', ''))
                return None
            return {'message_id': payload.get('message_id', 100 + len(calls))}
        return None

    monkeypatch.setattr(TelegramBot, '_call', fake)
    with TestClient(app) as client:
        client.auth = ('admin', 'change-me')
        assert client.post('/api/settings/telegram', json={
            'bot_token': TOKEN, 'enabled': True, 'allow_admin_grant': True,
            'miniapp_url': 'https://app.example.test/app',
            'group_interaction_chats': [CHAT]}).status_code == 200
        for uid, tid, roles in [('u1', ADMIN, ['admin']), ('u2', MEMBER, [])]:
            app.state.members.upsert(uid, 'private-account-' + uid, {'group_id': 'standard', 'roles': roles})
            app.state.db.execute('UPDATE members SET tg_user_id=? WHERE emby_user_id=?', (str(tid), uid))
        app.state.db.execute('UPDATE shop_items SET enabled=1')
        # These existing fixture identities have explicitly started the test Bot.
        # Private-admission tests clear this evidence and exercise unknown users.
        for tg in (ADMIN, MEMBER, GUEST, 888000444, 888000555, 888000666, 888000777, 888000888, 777000444):
            private_start(app.state.lottery, tg)
        app.state.telegram._test_turnstile = turnstile
        yield SimpleNamespace(client=client, service=app.state.lottery, calls=calls, replies=replies, ctl=ctl,
                              ip_for=ip_for, ips=ips, turnstile=turnstile, auto_captcha=True)


def config(**kw):
    # Pre-consent helpers: the consented device check is covered in test_lottery_device.py.
    return {'title': '星光福利', 'audience': 'all', 'chat_id': CHAT, 'thread_id': 9, 'device_check': False,
            'draw_mode': 'timed', 'deadline': int(time.time()) + 3600,
            'prizes': [{'kind': 'points', 'name': '500 积分', 'quantity': 3, 'amount': 500}], **kw}


def req(e, method, path, user=ADMIN, **kw):
    signed = {**headers(user), 'CF-Connecting-IP': e.ip_for(user)}
    if path.endswith('/join') and 'json' not in kw:
        kw['json'] = {'browser_id': browser_id(user)}
    if path.endswith('/join') and e.auto_captcha and 'captcha_token' not in kw.get('json', {}):
        kw['json'] = {**kw.get('json', {}), **captcha_args(e.service, path.split('/')[1], user)}
    return e.client.request(method, BASE + path, headers=signed, **kw)


def create(e, payload=None, publish=True):
    r = req(e, 'POST', '/admin/drafts', json=payload or config())
    assert r.status_code == 200, r.text
    lid = r.json()['id']
    if publish:
        r = req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
        assert r.status_code == 200, r.text
        assert r.json()['state'] == 'published'
    return lid


def join(e, lid, user=GUEST):
    r = req(e, 'POST', f'/{lid}/join', user)
    assert r.status_code == 200, r.text
    return r.json()


def draw(e, lid):
    r = req(e, 'POST', f'/{lid}/draw')
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_guest_is_not_a_member_or_admin(env):
    e = env
    assert e.client.get(BASE + '/context').status_code == 401
    assert req(e, 'GET', '/context', GUEST).json()['registered'] is False
    assert req(e, 'GET', '/context', ADMIN).json()['is_admin'] is True
    for path in ['/me', '/items', '/invites', '/shop']:
        assert e.client.get('/api/app' + path, headers=headers(GUEST)).status_code == 403
    for user in (MEMBER, GUEST):
        assert req(e, 'GET', '/admin/options', user).status_code == 403
        assert req(e, 'POST', '/admin/drafts', user, json={**config(), 'uid': 'u1', 'is_admin': True}).status_code == 403
    lid = create(e, publish=False)
    assert req(e, 'GET', '/' + lid, GUEST).status_code == 404
    assert req(e, 'POST', '/' + lid + '/draw', MEMBER).status_code == 403
    signed = headers(GUEST)
    signed['X-Telegram-Init-Data'] += '&uid=u1'
    assert e.client.get(BASE + '/context', headers=signed).status_code == 401


@pytest.mark.parametrize('patch', [
    {'audience': 'nobody'}, {'chat_id': '-1009999999999'}, {'deadline': 1}, {'deadline': True},
    {'draw_mode': 'other'}, {'thread_id': -1}, {'prizes': []},
    {'prizes': [{'kind': 'points', 'name': 'P', 'quantity': 1.5, 'amount': 5}]},
    {'prizes': [{'kind': 'points', 'name': 'P', 'quantity': 1, 'amount': -1}]},
    {'prizes': [{'kind': 'registration', 'name': 'R', 'quantity': 1, 'group_id': 'missing', 'days': 30}]},
    {'prizes': [{'kind': 'invite', 'name': 'I', 'quantity': 201, 'amount': 1}]},
    {'prizes': [{'kind': 'manual', 'name': 'M', 'quantity': 1, 'instructions': ''}]},
])
def test_config_server_validation(env, patch):
    assert req(env, 'POST', '/admin/drafts', json=config(**patch)).status_code == 400


def test_publish_freezes_revision_and_requires_main_app(env):
    e = env
    lid = create(e, publish=False)
    assert req(e, 'POST', f'/{lid}/preview').status_code == 200
    assert not [p for m, p in e.calls if m == 'sendMessage']
    e.ctl.main = False
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1}).status_code == 400
    assert e.service.row(lid)['state'] == 'draft'
    e.ctl.main = True
    assert req(e, 'PUT', '/' + lid, json={**config(title='修改后'), 'revision': 1}).status_code == 200
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1}).status_code == 400
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 2}).status_code == 200
    assert req(e, 'PUT', '/' + lid, json={**config(title='偷偷改奖'), 'revision': 2}).status_code == 400
    req(e, 'POST', f'/{lid}/publish', json={'revision': 2})
    sends = [p for m, p in e.calls if m == 'sendMessage']
    assert len(sends) == 1 and sends[0]['message_thread_id'] == 9
    assert '<tg-emoji emoji-id=' in sends[0]['text'] and '<img' not in sends[0]['text']
    button = sends[0]['reply_markup']['inline_keyboard'][0][0]
    assert button['url'].endswith('?startapp=lottery_' + lid)
    assert button['icon_custom_emoji_id'] and 'web_app' not in button


@pytest.mark.parametrize('membership', [None, {'status': 'left'}, {'status': 'kicked'}, {'status': 'restricted', 'is_member': False}])
def test_group_check_never_fails_open(env, membership):
    lid = create(env)
    env.ctl.membership = membership
    assert req(env, 'POST', f'/{lid}/join', GUEST).status_code == 400
    assert env.service.view(lid, str(GUEST))['count'] == 0


@pytest.mark.parametrize('aud,ok,no', [('guest', GUEST, MEMBER), ('member', MEMBER, GUEST)])
def test_audience(env, aud, ok, no):
    lid = create(env, config(audience=aud, prizes=[{'kind': 'manual', 'name': '人工奖', 'quantity': 1, 'instructions': '私密'}]))
    join(env, lid, ok)
    assert req(env, 'POST', f'/{lid}/join', no).status_code == 400


def test_concurrent_join_draw_claim_and_ledger(env):
    e = env
    lid = legacy_create(e)  # old guest account awards must still bind/claim exactly once
    # Independent requests exercise the guarded SQL primary key under contention.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: req(e, 'POST', f'/{lid}/join', GUEST).status_code, range(24)))
    assert results == [200] * 24
    join(e, lid, MEMBER)
    assert e.service.view(lid, str(GUEST))['count'] == 2
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: e.service.draw(lid), range(12)))
    winners = e.service.db.query('SELECT * FROM lottery_awards WHERE lottery_id=?', (lid,))
    assert len(winners) == 2  # fewer entries than slots
    e.service.deliver_awards(lid)
    assert app.state.points.balance('u2') == 500
    guest_award = next(w for w in winners if w['tg_id'] == str(GUEST))
    assert req(e, 'POST', f'/{lid}/claim', GUEST).json()['my_award']['state'] == 'binding'
    app.state.members.upsert('u3', 'private-new-account', {'group_id': 'standard'})
    e.service.db.execute('UPDATE members SET tg_user_id=? WHERE emby_user_id=\'u3\'', (str(GUEST),))
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: e.service.deliver_award(guest_award['id']), range(20)))
    assert app.state.points.balance('u3') == 500
    assert len(e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.award'")) == 2
    assert req(e, 'POST', f'/{lid}/claim', 777000444).status_code == 404
    assert req(e, 'POST', f'/{lid}/join', 777000444).status_code == 400


def test_deadline_and_timer_restart(env, monkeypatch):
    e = env
    lid = create(e)
    join(e, lid, MEMBER)
    deadline = e.service.row(lid)['config']['deadline']
    monkeypatch.setattr(time, 'time', lambda: deadline)
    assert req(e, 'POST', f'/{lid}/join', MEMBER).status_code == 200  # already joined: receipt retry
    assert req(e, 'POST', f'/{lid}/join', ADMIN).status_code == 400  # new entries remain closed
    # A second service / SQLite connection models restart & competing worker.
    other_db = Database(e.service.db._path)
    other = LotteryService(other_db, e.service.members, e.service.points, e.service.shop,
                           e.service.registration, e.service.groups, e.service.bot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda s: s.draw(lid, timed=True), [e.service, other]))
    snapshot = e.service.db.query('SELECT * FROM lottery_awards WHERE lottery_id=?', (lid,))
    asyncio.run(other.tick())
    asyncio.run(e.service.tick())
    assert e.service.db.query('SELECT tg_id FROM lottery_awards WHERE lottery_id=?', (lid,)) == [{'tg_id': snapshot[0]['tg_id']}]
    assert len(e.service.db.query('SELECT * FROM lottery_notices WHERE lottery_id=?', (lid,))) == 2
    other_db.close()


@pytest.mark.parametrize('kind', ['invite', 'shop', 'manual'])
def test_awards_snapshot_manual_privacy_and_idempotence(env, kind):
    e = env
    p = {'kind': kind, 'name': '奖品', 'quantity': 1, 'amount': 2, 'instructions': 'PRIVATE-CLAIM-CODE'}
    if kind == 'shop':
        item = e.service.shop.items()[0]
        p['item_id'] = item['id']
    lid = create(e, config(prizes=[p]))
    join(e, lid, MEMBER)
    before = app.state.points.balance('u2')
    d = draw(e, lid)
    a = d['awards'][0]
    if kind == 'invite':
        assert app.state.registration.invite_quota('u2') == 2
        e.service.deliver_awards(lid)
        assert app.state.registration.invite_quota('u2') == 2
        assert app.state.registration.spend_quota_for_invite('u2')['uses_left'] == 1
    elif kind == 'shop':
        inv = e.service.db.one('SELECT * FROM inventory WHERE id=?', (a['receipt']['card_id'],))
        assert json.loads(inv['spec_json'])['amount'] == item['amount']
        e.service.db.execute('UPDATE shop_items SET amount=99 WHERE id=?', (item['id'],))
        e.service.deliver_awards(lid)
        assert len(e.service.db.query('SELECT * FROM inventory')) == 1
        assert json.loads(inv['spec_json'])['amount'] != 99
    else:
        assert a['state'] == 'manual'
        assert 'PRIVATE-CLAIM-CODE' not in req(e, 'GET', '/' + lid, GUEST).text
        assert 'PRIVATE-CLAIM-CODE' in req(e, 'GET', '/' + lid, MEMBER).text
        assert req(e, 'POST', f'/{lid}/awards/{a["id"]}/confirm', MEMBER).status_code == 403
        assert req(e, 'POST', f'/{lid}/awards/{a["id"]}/confirm').status_code == 200
        assert req(e, 'POST', f'/{lid}/awards/{a["id"]}/confirm').status_code == 200
    assert app.state.points.balance('u2') == before
    public = req(e, 'GET', '/' + lid, GUEST).text
    assert 'private-account' not in public
    assert 'PRIVATE-CLAIM-CODE' not in str([p for m, p in e.calls if m == 'sendMessage'])


def test_registration_qualification_keeps_gifts_and_is_atomic(env):
    e = env
    old = app.state.registration.issue_gift(str(GUEST), 'test')
    prizes = [{'kind': 'registration', 'name': '本人专属注册资格', 'quantity': 3, 'group_id': 'standard', 'days': 17},
              {'kind': 'points', 'name': '积分', 'quantity': 3, 'amount': 50}]
    lid = create(e, config(prizes=prizes))
    join(e, lid, MEMBER)
    join(e, lid, GUEST)
    d = draw(e, lid)
    assert len(d['awards']) == 2
    mine = req(e, 'GET', '/' + lid, GUEST).json()['my_award']
    assert mine['prize']['kind'] == 'registration'
    code = mine['registration_url'].split('start=')[1]
    reg = app.state.registration
    assert reg.get_grant(str(GUEST)) == old  # no silent replacement
    assert reg.resolve(str(GUEST)).credential == old['gift_code']
    assert not reg.resolve(str(MEMBER), code).allowed
    admission = reg.resolve(str(GUEST), code)
    assert admission.allowed and admission.days == 17 and admission.group_id == 'standard'
    assert admission.via == 'admin' and admission.detail['lottery_award']
    with pytest.raises(RuntimeError), e.service.db.write() as conn:
        assert reg.consume(admission, 'new-account', conn=conn)
        raise RuntimeError('account transaction rolled back')
    assert reg.resolve(str(GUEST), code).allowed
    with e.service.db.write() as conn:
        assert reg.consume(admission, 'new-account', conn=conn)
    assert not reg.consume(admission, 'new-account')
    assert not reg.resolve(str(GUEST), code).allowed
    assert reg.get_grant(str(GUEST)) == old
    assert app.state.points.balance('u2') == 50
    assert 'private-account' not in str([p for m, p in e.calls if m == 'sendMessage'])


def test_registration_filter_at_draw_and_channel_gate(env):
    e = env
    p = {'kind': 'registration', 'name': '本人资格', 'quantity': 2, 'group_id': 'standard', 'days': 30}
    lid = create(e, config(prizes=[p]))
    join(e, lid, GUEST)
    app.state.members.upsert('u3', 'private-new-account', {'group_id': 'standard'})
    e.service.db.execute("UPDATE members SET tg_user_id=? WHERE emby_user_id='u3'", (str(GUEST),))
    assert draw(e, lid)['winners'] == []
    lid = create(e, config(prizes=[p]))
    join(e, lid, 777000444)
    e.client.post('/api/settings/telegram', json={'allow_admin_grant': False})
    assert draw(e, lid)['awards'][0]['state'] == 'failed'
    e.client.post('/api/settings/telegram', json={'allow_admin_grant': True})
    a = req(e, 'POST', f'/{lid}/claim', 777000444).json()['my_award']
    assert a['registration_url']
    code = a['registration_url'].split('start=')[1]
    e.client.post('/api/settings/telegram', json={'allow_admin_grant': False})
    assert not app.state.registration.resolve('777000444', code).allowed


@pytest.mark.parametrize('error,state', [
    ({'error_code': 500, 'description': 'server failure'}, 'unknown'),
    ({}, 'unknown'), ({'error_code': 403, 'description': 'forbidden'}, 'failed'),
    ({'error_code': 429, 'description': 'too many', 'parameters': {'retry_after': 120}}, 'pending'),
])
def test_notice_failure_no_blind_duplicate_and_recovery(env, error, state):
    e = env
    e.replies.append(error)
    lid = create(e)
    notice = req(e, 'GET', '/' + lid).json()['notices'][0]
    assert notice['state'] == state
    sent_before = len([m for m, p in e.calls if m == 'sendMessage'])
    asyncio.run(e.service.tick())
    assert len([m for m, p in e.calls if m == 'sendMessage']) == sent_before
    if state == 'unknown':
        assert req(e, 'POST', f'/{lid}/notices/{notice["id"]}/recover', json={}).status_code == 400
        r = req(e, 'POST', f'/{lid}/notices/{notice["id"]}/recover', json={'message_id': 777})
        assert r.json()['notices'][0]['state'] == 'sent'
        assert e.calls[-1][0] == 'editMessageText'
        assert e.calls[-1][1]['message_id'] == 777
    join(e, lid, MEMBER)
    e.replies.append({'error_code': 500})
    d = draw(e, lid)
    assert d['state'] == 'settled' and d['notices'][1]['state'] == 'unknown'
    again = draw(e, lid)
    assert again['winners'] == d['winners']


def test_emoji_definite_rejection_only_and_crash_lease(env):
    e = env
    e.replies.append({'error_code': 400, 'description': 'CUSTOM_EMOJI_ID_INVALID'})
    lid = create(e)
    sends = [p for m, p in e.calls if m == 'sendMessage']
    assert len(sends) == 2
    assert '<tg-emoji' in sends[0]['text'] and '<tg-emoji' not in sends[1]['text']
    assert 'icon_custom_emoji_id' not in sends[1]['reply_markup']['inline_keyboard'][0][0]
    e.service.db.execute("UPDATE lottery_notices SET state='sending',updated_at=? WHERE lottery_id=?", (int(time.time())-500, lid))
    asyncio.run(e.service.deliver_notices())
    assert req(e, 'GET', '/' + lid).json()['notices'][0]['state'] == 'unknown'
    assert len([p for m, p in e.calls if m == 'sendMessage']) == 2


def test_lottery_enters_existing_bot_password_flow_without_auto_account(env, monkeypatch):
    from tests.test_bot_password_choices import Emby
    e = env
    p = {'kind': 'registration', 'name': '本人资格', 'quantity': 1, 'group_id': 'standard', 'days': 19}
    lid = create(e, config(prizes=[p]))
    join(e, lid)
    draw(e, lid)
    code = req(e, 'GET', '/' + lid, GUEST).json()['my_award']['registration_url'].split('start=')[1]
    bot = e.service.bot
    emby = Emby()
    monkeypatch.setattr(bot, '_emby', emby)
    # Lottery registrations use the normal receipt queue/target, but never
    # include account names, passwords or admission credentials in the group.
    e.client.post('/api/settings/telegram', json={'registration_notify_chat_id': CHAT})

    async def run_flow():
        async def message(value):
            await bot._handle_message({'message_id': 999, 'chat': {'id': GUEST, 'type': 'private'},
                                       'from': {'id': GUEST, 'first_name': 'Participant'}, 'text': value})

        await message('/start ' + code)
        assert bot._pending[str(GUEST)][0] == 'username'
        await message('LotteryMember')
        assert bot._pending[str(GUEST)][0] == 'password_choice'
        assert not emby.created and not emby.passwords
        saved = bot._pending[str(GUEST)][2]
        await bot._password_flow_callback(f'pw:{saved["nonce"]}:custom', GUEST, saved['message_id'], str(GUEST))
        await message('Chosen-Test-Password-42')
        saved = bot._pending[str(GUEST)][2]
        assert bot._pending[str(GUEST)][0] == 'password_confirm'
        assert not emby.created and not emby.passwords
        await bot._password_flow_callback(f'pw:{saved["nonce"]}:confirm', GUEST, saved['message_id'], str(GUEST))
        await bot._password_flow_callback(f'pw:{saved["nonce"]}:confirm', GUEST, saved['message_id'], str(GUEST))

    asyncio.run(run_flow())
    assert emby.created == ['LotteryMember']
    assert emby.passwords == [('new-LotteryMember', 'Chosen-Test-Password-42')]
    member = e.service.members.find_by_telegram(str(GUEST))
    assert member['group_id'] == 'standard'
    assert abs(member['expires_at'] - time.time() - 19 * 86400) < 5
    assert not app.state.registration.resolve(str(GUEST), code).allowed
    assert e.service.db.one('SELECT used_by FROM lottery_admissions WHERE code=?', (code,))['used_by'] == 'new-LotteryMember'
    receipts = e.service.db.query('SELECT * FROM tg_gift_receipts')
    assert len(receipts) == 1 and receipts[0]['status'] == 'sent' and receipts[0]['attempts'] == 1
    posts = [p for method, p in e.calls if method == 'sendMessage' and str(p.get('chat_id')) == CHAT
             and '注册成功' in p.get('text', '')]
    assert len(posts) == 1 and f'tg://user?id={GUEST}' in posts[0]['text']
    assert code not in posts[0]['text'] and '账号：' not in posts[0]['text']
    for method, payload in e.calls:
        if payload and str(payload.get('chat_id', '')).startswith('-'):
            assert 'LotteryMember' not in str(payload) and 'Chosen-Test-Password' not in str(payload)


@pytest.mark.parametrize('kind', ['invite_card', 'bandwidth_card', 'streams_card', 'title_card', 'whitelist_card'])
def test_all_shop_awards_use_existing_inventory_effects(env, kind):
    e = env
    # Set finite base rights so optional bandwidth/streams can actually be used.
    app.state.members.set_overrides('u2', {'bandwidth_limit_kbps': 10000, 'max_streams': 2})
    item = next(i for i in e.service.shop.items() if i['kind'] == kind)
    lid = create(e, config(prizes=[{'kind': 'shop', 'name': item['name'], 'quantity': 1, 'item_id': item['id']}]))
    join(e, lid, MEMBER)
    award = draw(e, lid)['awards'][0]
    assert app.state.points.balance('u2') == 0
    card = award['receipt']['card_id']
    before = app.state.members.get('u2')
    result = app.state.inventory.use('u2', card, title='幸运之星' if kind == 'title_card' else '')
    after = app.state.members.get('u2')
    assert result['kind'] == kind and result['ok']
    assert app.state.inventory.use('u2', card, title='幸运之星' if kind == 'title_card' else '') == result
    if kind == 'invite_card':
        assert app.state.registration.invite_quota('u2') == 1
    elif kind == 'bandwidth_card':
        assert after['bandwidth_limit_kbps'] == before['bandwidth_limit_kbps'] + item['amount'] * 1000
    elif kind == 'streams_card':
        assert after['max_streams'] == before['max_streams'] + item['amount']
    elif kind == 'whitelist_card':
        assert after['group_id'] == 'whitelist'
    else:
        assert result['tag'] == '幸运之星'
    assert app.state.points.balance('u2') == 0


def test_private_custom_shop_snapshot_never_public(env):
    e = env
    item = e.service.shop.create({'kind': 'custom', 'name': '限定兑换', 'cost': 500, 'amount': 1,
                                 'purchase_notice': 'PRIVATE-ONLY-CONTENT', 'retention_days': 7})
    lid = create(e, config(prizes=[{'kind': 'shop', 'name': item['name'], 'quantity': 1, 'item_id': item['id']}]))
    join(e, lid, MEMBER)
    award = draw(e, lid)['awards'][0]
    assert 'PRIVATE-ONLY-CONTENT' not in req(e, 'GET', '/' + lid, GUEST).text
    assert 'PRIVATE-ONLY-CONTENT' not in str([p for m, p in e.calls if m == 'sendMessage'])
    notice = app.state.inventory.custom_notice('u2', award['receipt']['card_id'])
    assert notice['notice'] == 'PRIVATE-ONLY-CONTENT'
    assert app.state.points.balance('u2') == 0


@pytest.mark.parametrize('fallback_first', [False, True])
def test_any_timeout_is_unknown_even_after_emoji_fallback(env, monkeypatch, fallback_first):
    import httpx
    e = env
    original = e.service.bot._call
    attempts = []

    async def timeout(method, payload=None, **kw):
        if method != 'sendMessage':
            return await original(method, payload, **kw)
        attempts.append(payload)
        if fallback_first and len(attempts) == 1:
            CALL_DELIVERY.set(failure({'error_code': 400, 'description': 'custom emoji not allowed'}))
            _CALL_ERROR.set('custom emoji not allowed')
        else:
            CALL_DELIVERY.set(failure(exc=httpx.ConnectTimeout('synthetic')))
            _CALL_ERROR.set('ConnectTimeout: 请求失败')
        return None
    monkeypatch.setattr(e.service.bot, '_call', timeout)
    lid = create(e)
    assert e.service.view(lid, str(ADMIN), True)['notices'][0]['state'] == 'unknown'
    asyncio.run(e.service.tick())
    assert len(attempts) == (2 if fallback_first else 1)


def test_account_award_failure_rolls_back_ledger_then_recovers(env, monkeypatch):
    e = env
    lid = create(e)
    join(e, lid, MEMBER)
    original = e.service.points._apply

    def broken(*args):
        original(*args)
        raise ValueError('synthetic failure after ledger write')
    monkeypatch.setattr(e.service.points, '_apply', broken)
    d = draw(e, lid)
    assert d['state'] == 'settled' and d['awards'][0]['state'] == 'failed'
    assert app.state.points.balance('u2') == 0
    monkeypatch.setattr(e.service.points, '_apply', original)
    req(e, 'POST', f'/{lid}/claim', MEMBER)
    assert app.state.points.balance('u2') == 500
    assert e.service.view(lid, str(MEMBER))['winners'] == d['winners']


def test_award_claim_across_database_connections(env):
    e = env
    lid = create(e)
    join(e, lid, MEMBER)
    e.service.draw(lid)
    award = e.service.db.one('SELECT id FROM lottery_awards WHERE lottery_id=?', (lid,))
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda s: s.deliver_award(award['id']), [e.service, other] * 10))
        assert app.state.points.balance('u2') == 500
        assert len(e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.award'")) == 1
    finally:
        other.db.close()


def test_existing_lifecycle_executes_timer_and_delivery(env):
    e = env
    lid = create(e)
    join(e, lid, MEMBER)
    c = e.service.row(lid)['config']
    # Move only fixture time to due; do not bypass product deadline validation.
    c['deadline'] = int(time.time()) - 1
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))
    end = time.monotonic() + 22
    while time.monotonic() < end:
        d = e.service.view(lid, str(ADMIN), True)
        if d['state'] == 'settled' and len(d['notices']) == 2 and d['notices'][1]['state'] == 'sent':
            break
        time.sleep(0.1)
    assert d['state'] == 'settled'
    assert d['notices'][1]['state'] == 'sent'
    assert app.state.points.balance('u2') == 500
    assert app.state.lottery_task is not None


def test_bot_disabled_holds_notices_and_registration_but_not_settlement(env):
    e = env
    lid = create(e)
    join(e, lid, MEMBER)
    e.client.post('/api/settings/telegram', json={'enabled': False})
    count = len([p for m, p in e.calls if m == 'sendMessage'])
    d = draw(e, lid)
    assert d['state'] == 'settled' and d['notices'][1]['state'] == 'pending'
    assert len([p for m, p in e.calls if m == 'sendMessage']) == count
    assert req(e, 'POST', f'/{lid}/join', GUEST).status_code == 400
    e.client.post('/api/settings/telegram', json={'enabled': True})
    asyncio.run(e.service.deliver_notices(lid))
    assert e.service.view(lid, str(ADMIN), True)['notices'][1]['state'] == 'sent'
