"""New rules through real dispatch and SQLite; only Telegram transport is simulated."""
import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_dice import SIGNED, cash, create, message, svc
from test_tg_interaction_context import GROUP

from app.modules import dice_beacon as beacon
from app.modules.bot_dice import DiceCleanup
from app.modules.dice_group import GROUP_VERSION
from app.modules.dice_group_view import group_card
from app.modules.economy_rules import economy_write
from app.modules.play_money import PlayError


@pytest.fixture
def env(request, monkeypatch):
    e = request.getfixturevalue('bw_env')
    e.services.registry.save('dice', enabled=True)
    e.services.registry.get('dice').ctx.telegram = e.bot
    # Existing split photo/text rounds retain their persisted transport after upgrade.
    from app.modules.dice import DiceService
    original = DiceService.create
    def split_create(service, message, *, now=None):
        row = original(service, message, now=now)
        rules = json.loads(row['rules'])
        rules.pop('presentation', None)
        service.db.execute('UPDATE dice_rounds SET rules=? WHERE nonce=?', (json.dumps(rules), row['nonce']))
        return service.get(row['nonce'])
    monkeypatch.setattr(DiceService, 'create', split_create)
    return e


def reply(e, row):
    m = copy.deepcopy(e.tg.message(GROUP, row['card_mid'])) or {}
    m.update(chat={'id': GROUP, 'type': 'supergroup'}, message_id=row['card_mid'],
             is_topic_message=True, message_thread_id=row['thread_id'], **{'from': {'id': 123, 'is_bot': True}})
    return m


def bet(e, row, text='/大 100', index=0, mid=8100):
    return dict(message(e, text, index, mid), reply_to_message=reply(e, row))


async def send(e, m):
    await e.bot._dispatch_update({'message': m})


async def refresh(e, row):
    e.db.execute('UPDATE dice_rounds SET group_due=0 WHERE nonce=?', (row['nonce'],))
    await e.bot._dice_group_refresh(row['nonce'])


async def click(e, row, op, m=None):
    data = f'dg:{row["nonce"]}:{op}'
    await e.bot._dispatch_update({'callback_query': {'id': data, 'from': e.actors[0], 'data': data, 'message': m or reply(e, row)}})


def test_dispatch_full_ledger_append_reverse_and_shared_page(env):
    async def run():
        row = await create(env)
        assert row['deadline']-row['created'] == 180
        assert json.loads(row['rules'])['version'] == GROUP_VERSION
        assert env.db.one('SELECT COUNT(*) n FROM dice_pools')['n'] == 1
        start = cash(env)
        for i in range(19):
            m = bet(env, row, '/大 10' if i % 2 else '/小 20', index=i % 2, mid=8100+i)
            await send(env, m)
            await send(env, m)
        assert env.db.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 19
        assert env.db.one('SELECT COUNT(*) n FROM dice_group_receipts')['n'] == 19
        assert cash(env) == start
        await refresh(env, row)
        body = env.tg.text(GROUP, row['card_mid'])
        assert '1/3' in body and '共19笔' in body and '#19' in body
        import re
        def numbers(text):
            return [int(n) for n in re.findall(r'#(\d+)</code>', text.split('完整流水</b>')[1])]
        assert numbers(body) == list(range(19, 11, -1))
        assert '正在查看历史页' not in body
        assert body.index('#19') < body.index('#18') < body.index('总池')
        assert 'tg://user?id=' in body
        await click(env, row, 'page1')
        await refresh(env, row)
        body = env.tg.text(GROUP, row['card_mid'])
        assert '2/3' in body and numbers(body) == list(range(11, 3, -1))
        await click(env, row, 'page2')
        await refresh(env, row)
        assert numbers(env.tg.text(GROUP, row['card_mid'])) == [3, 2, 1]
        await click(env, row, 'page1')
        await send(env, bet(env, row, '/小 7', index=1, mid=8400))
        await refresh(env, row)
        body = env.tg.text(GROUP, row['card_mid'])
        assert '#20' in body and '2/3' in body
        assert numbers(body) == list(range(12, 4, -1))
        await click(env, row, 'latest')
        await refresh(env, row)
        body = env.tg.text(GROUP, row['card_mid'])
        assert '1/3' in body and numbers(body) == list(range(20, 12, -1))
        assert not env.db.query('SELECT * FROM dice_sessions')
        assert len([p for method, p in env.tg.calls if method == 'sendMessage' and p['chat_id'] == GROUP]) == 1
        await env.bot._drain_checkin_deletes(now=time.time()+70, service=DiceCleanup(env.db))
        deleted = {p['message_id'] for method, p in env.tg.calls if method == 'deleteMessage'}
        assert set(range(8100, 8119)) <= deleted
        assert row['card_mid'] not in deleted
    asyncio.run(run())


@pytest.mark.parametrize('case', ['unrelated', 'wrong_bot', 'forged', 'group', 'topic', 'forward', 'reply_forward', 'anonymous', 'robot', 'wrong_target', 'zero', 'negative', 'float', 'overflow', 'closed', 'private'])
def test_invalid_dispatch_never_debits_or_deletes_unrelated(env, case):
    async def run():
        row = await create(env)
        m = bet(env, row)
        if case == 'unrelated': m['reply_to_message']['message_id'] += 123
        if case == 'wrong_bot': m['reply_to_message']['from']['id'] = 999
        if case == 'forged': m['reply_to_message']['from'] = env.actors[1]
        if case == 'group': m['reply_to_message']['chat']['id'] -= 1
        if case == 'topic': m['message_thread_id'] = 22
        if case == 'forward': m['forward_origin'] = {'type': 'user'}
        if case == 'reply_forward': m['reply_to_message']['forward_origin'] = {'type': 'user'}
        if case == 'anonymous': m['sender_chat'] = {'id': GROUP}
        if case == 'robot': m['from'] = {'id': 123, 'is_bot': True}
        if case == 'wrong_target': m['text'] = '/大@OtherBot 100'
        if case in ('zero', 'negative', 'float', 'overflow'):
            m['text'] = '/大 '+{'zero': '0', 'negative': '-1', 'float': '1.5', 'overflow': '999999999999999999999'}[case]
        if case == 'closed': env.db.execute("UPDATE dice_rounds SET state='locked'")
        if case == 'private': m['chat'] = {'id': env.actors[0]['id'], 'type': 'private'}
        before = cash(env)
        await send(env, m)
        assert not env.db.query('SELECT * FROM dice_tickets')
        assert cash(env) == before
        assert env.services.points.balance(env.uids[0]) == 1000
        await env.bot._drain_checkin_deletes(now=time.time()+70, service=DiceCleanup(env.db))
        deleted = {p['message_id'] for method, p in env.tg.calls if method == 'deleteMessage'}
        assert row['card_mid'] not in deleted
        assert row['card_mid']+123 not in deleted
        if case in ('forward', 'anonymous', 'robot', 'wrong_target', 'private'):
            assert m['message_id'] not in deleted
        else:
            assert m['message_id'] in deleted
    asyncio.run(run())


def test_concurrent_balance_idempotency_and_close_boundary(env):
    row = asyncio.run(create(env))
    m = bet(env, row, '/大 700')
    def accept(mid):
        try:
            svc(env).group_bet(dict(m, message_id=mid), '大', 700, now=row['deadline']-0.001)
            return True
        except PlayError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(accept, [8100]*8))
        assert not any(pool.map(accept, range(8101, 8109)))
    assert env.services.points.balance(env.uids[0]) == 300
    with pytest.raises(PlayError):
        svc(env).group_bet(bet(env, row, mid=8200), '大', 1, now=row['deadline'])
    svc(env).group_bet(m, '大', 700, now=row['timeout']+10)
    with pytest.raises(PlayError):
        svc(env).group_bet(m, '小', 700)
    assert env.db.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 1


def test_verified_settlement_report_paging_and_cleanup(env, monkeypatch):
    async def run():
        row = await create(env)
        original_cash = cash(env)
        await send(env, bet(env, row, '/大 101'))
        await send(env, bet(env, row, '/小 211', 1, 8101))
        for i in range(10):
            await send(env, bet(env, row, '/大 1', 0, 8200+i))
        env.db.execute("UPDATE dice_rounds SET deadline=?,timeout=?,round_number=?,state='locked'", (time.time()-1, time.time()+500, SIGNED['round']))
        async def fetch(number):
            assert number == SIGNED['round']
            return SIGNED
        monkeypatch.setattr(beacon, 'fetch', fetch)
        await env.bot._dice_tick()
        done = svc(env).get(row['nonce'])
        assert done['state'] == 'settled', done['error']
        assert len(json.loads(done['dice'])) == 3
        assert cash(env) == original_cash
        assert sum(o['net'] for o in json.loads(done['result'])['size']['users'].values()) == 0
        report = env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")
        assert report['state'] == 'sent'
        body = env.tg.text(GROUP, report['mid'])
        assert '净盈亏' in body and 'tg://user?id=' in body
        report_message = reply(env, row)
        report_message.update(copy.deepcopy(env.tg.message(GROUP, report['mid'])), message_id=report['mid'])
        await click(env, row, 'latest', report_message)
        await refresh(env, row)
        assert '#1' in env.tg.text(GROUP, report['mid']) and '共12笔' in env.tg.text(GROUP, report['mid'])
        assert not env.db.query("SELECT * FROM dice_messages WHERE purpose LIKE 'detail:%'")
        deleted = {p['message_id'] for method, p in env.tg.calls if method == 'deleteMessage'}
        assert row['card_mid'] in deleted and report['mid'] not in deleted
        prior = done['result']
        await env.bot._dice_tick()
        assert svc(env).get(row['nonce'])['result'] == prior
        assert cash(env) == original_cash
    asyncio.run(run())


def test_preview_bounds_rules_proof_and_no_private_entry(env):
    async def run():
        row = await create(env)
        with pytest.raises(PlayError): svc(env).new_session(row['nonce'], env.actors[0])
        for view in ('ledger', 'rules', 'proof'):
            env.db.execute('UPDATE dice_rounds SET group_view=?', (view,))
            with economy_write(env.db):
                row, snapshot = svc(env).group_snapshot(row['nonce'])
                body, keys = group_card(row, snapshot)
            assert keys
            if view == 'ledger':
                assert '等待对手' in body
            if view == 'ledger':
                assert row['nonce'] not in body and row['commitment'] not in body
    asyncio.run(run())


def test_coalesced_refresh_and_rate_limit_is_durable(env):
    from app.modules.report_delivery import CALL_DELIVERY
    async def run():
        row = await create(env)
        for i in range(3):
            await send(env, bet(env, row, '/小 10', mid=8300+i))
        assert not [p for method, p in env.tg.calls if method == 'editMessageText' and p.get('message_id') == row['card_mid']]
        await asyncio.sleep(2.2)
        edits = [p for method, p in env.tg.calls if method == 'editMessageText' and p.get('message_id') == row['card_mid']]
        assert len(edits) == 1 and '共3笔' in edits[0]['text']
        real = env.bot._call
        attempts = []
        async def limited(method, payload=None, **kw):
            if method == 'editMessageText':
                attempts.append(payload)
                CALL_DELIVERY.set({'state': 'retry', 'retry_after': 30})
                return None
            return await real(method, payload, **kw)
        env.bot._call = limited
        await send(env, bet(env, row, '/大 70', 1, 8400))
        await refresh(env, row)
        dirty = svc(env).get(row['nonce'])
        assert dirty['group_rendered'] < dirty['group_revision']
        assert dirty['group_retry'] > time.time()+20
        await click(env, row, 'latest')
        assert len(attempts) == 1  # Navigation cannot bypass Telegram retry_after.
        env.bot._call = real
        env.db.execute('UPDATE dice_rounds SET group_retry=0,group_due=0')
        await env.bot._dice_tick()
        done = svc(env).get(row['nonce'])
        assert done['group_rendered'] == done['group_revision']
        assert '共4笔' in env.tg.text(GROUP, row['card_mid'])
        assert env.services.points.balance(env.uids[1]) == 930
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['triple', 'single_side', 'self_opponent', 'timeout', 'fixed_after_timeout'])
def test_refunds_and_fixed_result_never_reroll(env, mode):
    async def run():
        row = await create(env)
        before = cash(env)
        await send(env, bet(env, row, '/大 100'))
        if mode != 'single_side':
            await send(env, bet(env, row, '/小 50', 0 if mode == 'self_opponent' else 1, 8101))
        if mode == 'timeout':
            env.db.execute('UPDATE dice_rounds SET deadline=0,timeout=0')
            await env.bot._dice_tick()
        else:
            dice = '[3,3,3]' if mode == 'triple' else '[4,5,6]'
            env.db.execute("UPDATE dice_rounds SET state='fixed',dice=?,seed=?,timeout=0", (dice, 'ab'*32))
            await env.bot._dice_tick()
        done = svc(env).get(row['nonce'])
        assert done['state'] in ('settled', 'refunded')
        assert cash(env) == before
        result = json.loads(done['result'])['size']
        if mode == 'fixed_after_timeout':
            assert not result['reason'] and env.services.points.balance(env.uids[0]) == 1050
        else:
            assert result['reason'] and all(v['net'] == 0 for v in result['users'].values())
        if mode == 'self_opponent': assert result['reason'] == 'no_opponent'
        await env.bot._dice_tick()
        assert svc(env).get(row['nonce'])['result'] == done['result']
        assert cash(env) == before
    asyncio.run(run())


def test_new_group_cross_connection_reopen_and_idempotency(env):
    from app.core.db import Database
    from app.modules.dice import DiceService
    from app.modules.groups import GroupService
    from app.modules.members import MemberService
    from app.modules.points import PointsService
    row = asyncio.run(create(env))
    db2 = Database(env.db.one('PRAGMA database_list')['file'])
    second = DiceService(db2, MemberService(db2, GroupService(db2)), PointsService(db2), lambda: True, lambda c: True, '123')
    m = bet(env, row, '/大 800')
    try:
        def place(pair):
            service, mid = pair
            try:
                service.group_bet(dict(m, message_id=mid), '大', 800)
                return True
            except PlayError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(place, [(svc(env), 8100), (second, 8101)])) == 1
        assert second.points.balance(env.uids[0]) == 200
        receipt = db2.one('SELECT * FROM dice_group_receipts')
        second.group_bet(dict(m, message_id=receipt['message_id']), '大', 800)
        second.close(row['nonce'], now=row['timeout'])
        assert second.settle(row['nonce'], now=row['timeout'])['state'] == 'refunded'
        assert second.points.balance(env.uids[0]) == 1000
        assert db2.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 1
    finally:
        db2.close()


def test_unknown_open_ack_and_report_ack_do_not_guess_or_resend(env):
    from app.modules.report_delivery import CALL_DELIVERY
    async def run():
        real = env.bot._call
        async def lose(method, payload=None, **kw):
            result = await real(method, payload, **kw)
            if method == 'sendMessage' and payload['chat_id'] == GROUP and '骰局' in payload.get('text', ''):
                CALL_DELIVERY.set({'state': 'unknown', 'reason': 'lost ACK'})
                return None
            return result
        env.bot._call = lose
        row = await create(env)
        assert row['card_mid'] is None
        key, _ = next((k, v) for k, v in env.tg.messages.items() if str(k[0]) == str(GROUP) and '大小对决' in v.get('text', ''))
        real_row = dict(row, card_mid=key[1])
        m = bet(env, real_row)
        await send(env, m)
        assert not env.db.query('SELECT * FROM dice_tickets')
        env.bot._call = real
        await click(env, row, 'latest', reply(env, real_row))
        row = svc(env).get(row['nonce'])
        assert row['card_mid'] == key[1]
        await send(env, bet(env, row, mid=8101))
        assert env.services.points.balance(env.uids[0]) == 900
        env.bot._call = lose
        env.db.execute('UPDATE dice_rounds SET deadline=0,timeout=0')
        await env.bot._dice_tick()
        report = env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")
        assert report['state'] == 'unknown'
        assert not env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:{row["card_mid"]}',))
        env.bot._call = real
        await env.bot._dice_tick()
        assert env.db.one("SELECT state FROM dice_messages WHERE purpose='report:0'")['state'] == 'unknown'
        key, _ = next((k, v) for k, v in env.tg.messages.items() if str(k[0]) == str(GROUP) and '净盈亏' in v.get('text', ''))
        await click(env, row, 'latest', reply(env, dict(row, card_mid=key[1])))
        assert env.db.one("SELECT state FROM dice_messages WHERE purpose='report:0'")['state'] == 'sent'
        assert env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:{row["card_mid"]}',))
    asyncio.run(run())


def test_permissions_old_round_reply_and_feedback_cleanup(env, monkeypatch):
    async def run():
        row = await create(env)
        m = bet(env, row)
        m['from'] = {'id': 99999999, 'is_bot': False, 'first_name': 'unbound'}
        await send(env, m)
        assert not env.db.query('SELECT * FROM dice_tickets')
        assert env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:8100',))
        feedback = [p for method, p in env.tg.calls if method == 'sendMessage' and '有效绑定' in p.get('text', '')]
        assert len(feedback) == 1
        env.db.execute('UPDATE dice_rounds SET deadline=0,timeout=0')
        await env.bot._dice_tick()
        await send(env, bet(env, row, mid=8200))
        assert not env.db.query('SELECT * FROM dice_tickets')
        monkeypatch.setattr(env.bot, '_group_chat_allowed', lambda chat: False)
        await send(env, bet(env, row, mid=8201))
        assert not env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:8201',))
    asyncio.run(run())


def test_large_source_template_preview_bounds_and_ledger_coverage():
    from app.modules.dice_group_view import report_pages
    from app.modules.game_mentions import text_units
    from tools.preview_dice_group import samples
    views = samples()
    assert len(views) == 9 and all(text_units(v['text']) <= 4096 for v in views)
    tickets = [{'user_id': str(i), 'tg_id': str(1000+i), 'display_name': '😀<&>'*30,
                'amount': 9999999, 'items': json.dumps([{'pool': 'size', 'option': '大' if i % 2 else '小'}])} for i in range(150)]
    from app.modules.dice_rules import settle_pool
    bets = [dict(json.loads(t['items'])[0], user_id=t['user_id'], amount=t['amount']) for t in tickets]
    row = {'nonce': 'a'*24, 'state': 'settled', 'dice': '[4,5,6]',
           'result': json.dumps({'size': settle_pool('size', bets, [4, 5, 6], 'ab'*32)})}
    pages = report_pages(row, tickets)
    assert len(pages) == 8
    assert sum(p.count('tg://user?id=') for p in pages) == 150
    assert all(text_units(p) <= 4096 for p in pages)


@pytest.mark.parametrize('case', ['wrong_mid', 'wrong_bot', 'topic', 'group', 'forward', 'anonymous', 'unbound'])
def test_shared_history_callbacks_require_authentic_context(env, case):
    async def run():
        row = await create(env)
        m = reply(env, row)
        actor = env.actors[0]
        if case == 'wrong_mid': m['message_id'] += 10
        if case == 'wrong_bot': m['from']['id'] = 999
        if case == 'topic': m['message_thread_id'] = 22
        if case == 'group': m['chat']['id'] -= 10
        if case == 'forward': m['forward_origin'] = {'type': 'user'}
        if case == 'anonymous': m['sender_chat'] = {'id': GROUP}
        if case == 'unbound': actor = {'id': 9999999, 'is_bot': False}
        data = f'dg:{row["nonce"]}:details'
        await env.bot._dispatch_update({'callback_query': {'id': data, 'from': actor, 'data': data, 'message': m}})
        assert svc(env).get(row['nonce'])['group_view'] == 'ledger'
        assert not env.db.query('SELECT * FROM dice_tickets')
    asyncio.run(run())


def test_group_bet_and_account_deletion_share_guard(env):
    from app.core.errors import ConfigError
    from app.modules.account_deletion import self_deletion_guard

    row = asyncio.run(create(env))
    service = svc(env)
    msg = bet(env, row)
    before = cash(env)
    with self_deletion_guard(env.db, env.uids[0]), pytest.raises(PlayError, match='注销'):
        service.group_bet(msg, '大', 100)
    assert not env.db.query('SELECT * FROM dice_tickets')
    assert cash(env) == before
    service.group_bet(msg, '大', 100)
    with pytest.raises(ConfigError, match='未结算'), self_deletion_guard(env.db, env.uids[0]):
        pytest.fail('accepted group wager must block deletion')
    env.db.execute("UPDATE dice_rounds SET state='locked' WHERE nonce=?", (row['nonce'],))
    service.settle(row['nonce'], now=row['timeout'])
    with self_deletion_guard(env.db, env.uids[0]):
        pass
    assert cash(env) == before
