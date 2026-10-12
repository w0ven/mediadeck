"""Real handlers, real SQLite ledger, real signed drand sample; never send to Telegram."""
import asyncio
import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules import dice_beacon as beacon
from app.modules.bot_dice import DiceCleanup
from app.modules.dice import DiceService
from app.modules.dice_rules import MAX_INT, POOLS, hits, settle_pool, validate
from app.modules.dice_view import private_card, report_pages
from app.modules.economy_rules import economy_write, encode
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.play_money import PlayError
from app.modules.points import PointsService
from app.modules.report_delivery import CALL_DELIVERY

# Downloaded from the pinned public chain on 2026-10-12 and independently BLS verified.
SIGNED = {'round': 6543504, 'randomness': '58d987b634f331bfee0a5146e4cefa19c6c018025c07d9f47f1208f56f3a228e',
 'signature': '80f8bede68af1a40b473770db73f0f25b34ca244f18f3895485649212459db5590ecc2590c02171136c45cdc598217290504b4ef7dd4f91454add86a4441618c2319d99a6f90b22762b1bec34a0a22aa149122d1954207d636b6f254f19736a8',
 'previous_signature': 'b899676063407b7e8de34c0da1bd997d1451c2b2fb6375d5f689dc4b6cd082cd7d7399138be9854c6e0944167bf2b819104bc395f55bdc98ba7922aeca9ab94890a04ec9f7e77431526cc8a4f5ba70b5bf75533a7ca74f7e79a114ed961e12b7'}


@pytest.fixture
def env(request, monkeypatch):
    e = request.getfixturevalue('bw_env')
    assert not e.services.registry.enabled('dice')
    e.services.registry.save('dice', enabled=True)
    e.services.registry.get('dice').ctx.telegram = e.bot
    # Existing suite is the legacy seven-pool compatibility contract. Seed the
    # persisted pre-upgrade format, rather than changing new production rules.
    from dice_legacy import legacy_group_create
    original = legacy_group_create
    def legacy_create(service, message, *, now=None):
        row = original(service, message, now=now)
        if json.loads(row['rules'])['version'] != beacon.VERSION:
            deadline = row['created']+90
            service.db.execute('UPDATE dice_rounds SET rules=?,deadline=?,timeout=?,round_number=? WHERE nonce=?',
                (encode({'version': beacon.VERSION, 'chain': beacon.CHAIN, 'window': 90, 'wait': 600, 'fee': 0}), deadline, deadline+600, beacon.future_round(deadline), row['nonce']))
            for pool in POOLS:
                service.db.execute('INSERT OR IGNORE INTO dice_pools(nonce,pool) VALUES(?,?)', (row['nonce'], pool))
        return service.get(row['nonce'])
    monkeypatch.setattr(DiceService, 'create', legacy_create)
    return e


def svc(e):
    return e.bot._dice_service()


def message(e, text='/猜大小 1000', index=0, mid=7001, private=False, thread=21):
    chat = e.actors[index]['id'] if private else GROUP
    return {'chat': {'id': chat, 'type': 'private' if private else 'supergroup'}, 'from': e.actors[index],
            'message_id': mid, 'text': text, **({'is_topic_message': True, 'message_thread_id': thread} if not private and thread else {})}


async def create(e, **kw):
    await e.bot._dispatch_update({'message': message(e, **kw)})
    row = e.db.one('SELECT * FROM dice_rounds ORDER BY created DESC LIMIT 1')
    assert row, e.bot._last_error
    return row


async def enter(e, row, index=0):
    await e.bot._dispatch_update({'message': message(e, '/start dice_'+row['nonce'], index, private=True)})
    s = e.db.one('SELECT * FROM dice_sessions WHERE nonce=? AND tg_id=? ORDER BY rowid DESC LIMIT 1', (row['nonce'], str(e.actors[index]['id'])))
    assert s and s['mid'], e.bot._last_error
    return s


def card(e, s):
    m = copy.deepcopy(e.tg.message(int(s['tg_id']), s['mid']))
    m.update(chat={'id': int(s['tg_id']), 'type': 'private'}, message_id=s['mid'])
    m['from'] = {'id': 123, 'is_bot': True}
    return m


async def click(e, s, op, arg='0', index=0, original=None):
    s = svc(e).session(s['token'])
    data = f'dc:{s["token"]}:{s["revision"]}:{op}:{arg}'
    await e.bot._dispatch_update({'callback_query': {'id': data, 'from': e.actors[index], 'data': data, 'message': original or card(e, s)}})
    return svc(e).session(s['token'])


async def add(e, s, pool, option, amount=10, index=0):
    s = await click(e, s, 'pool', pool, index)
    s = await click(e, s, 'pick', str(POOLS[pool][1].index(option)), index)
    if amount in (10, 50, 100):
        return await click(e, s, 'amount', str(amount), index)
    m = message(e, str(amount), index, mid=7900+index, private=True)
    m['reply_to_message'] = card(e, s)
    await e.bot._dispatch_update({'message': m})
    return svc(e).session(s['token'])


def cash(e):
    return sum(e.services.points.balances().values()) + (e.db.one("SELECT SUM(amount) n FROM play_escrows")['n'] or 0)


def draft(e, row, index, items, now=None):
    s = svc(e).new_session(row['nonce'], e.actors[index], now=now)
    p = json.loads(s['payload'])
    p.update(draft=items, view='draft')
    with economy_write(e.db) as conn:
        svc(e).update_session(conn, s, p)
    return svc(e).session(s['token'])


def open_service(e, *, now=None, mid=9001, index=0, thread=21):
    row = svc(e).create(message(e, mid=mid, index=index, thread=thread), now=now)
    e.db.execute("UPDATE dice_rounds SET state='open',card_mid=301 WHERE nonce=?", (row['nonce'],))
    return svc(e).get(row['nonce'])


@pytest.mark.parametrize('dice', list(product(range(1, 7), repeat=3)))
def test_all_216_seven_pool_outcomes_and_conservation(dice):
    expected = hits(dice)
    assert expected['single'] == {str(n): dice.count(n) for n in set(dice)}
    for pool, (_, options) in POOLS.items():
        independent = {}
        for o in options:
            if pool == 'size': ok = len(set(dice)) > 1 and ((o == '大') == (sum(dice) >= 11))
            elif pool == 'parity': ok = len(set(dice)) > 1 and ((o == '单') == (sum(dice) % 2 == 1))
            elif pool == 'single': ok = int(o) in dice
            elif pool == 'double': ok = dice.count(int(o[0])) >= 2
            elif pool == 'combo': ok = all(int(n) in dice for n in o.split('+'))
            elif pool == 'triple': ok = (len(set(dice)) > 1 if o == '无豹子' else len(set(dice)) == 1 if o == '任意豹子' else tuple(map(int, o)) == dice)
            else: ok = int(o) == sum(dice)
            if ok: independent[o] = dice.count(int(o)) if pool == 'single' else 1
        assert expected[pool] == independent
        bets = [{'user_id': str(i % 3), 'option': o, 'amount': i+1} for i, o in enumerate(options)]
        result = settle_pool(pool, bets, dice, 'seed')
        assert sum(u['returned'] for u in result['users'].values()) == sum(b['amount'] for b in bets)
        assert sum(u['net'] for u in result['users'].values()) == 0
        split = [{**b, 'amount': 1} for b in bets for _ in range(b['amount'])]
        assert settle_pool(pool, split, dice, 'seed') == result
        assert settle_pool(pool, [], dice, 'seed')['reason'] == 'empty'


def test_document_examples_and_edge_rules():
    def bet(uid, option, amount): return {'user_id': uid, 'option': option, 'amount': amount}
    out = settle_pool('size', [bet('a','大',100), bet('b','大',200), bet('c','小',200)], [3,4,5], 'x')['users']
    assert [out[u]['returned'] for u in 'abc'] == [167,333,0]
    out = settle_pool('single', [bet('a','2',100), bet('b','5',100), bet('c','6',90)], [2,2,5], 'x')['users']
    assert [out[u]['returned'] for u in 'abc'] == [160,130,0]
    out = settle_pool('triple', [bet('a','任意豹子',10), bet('b','222',20), bet('c','无豹子',30)], [2,2,2], 'x')['users']
    assert [out[u]['returned'] for u in 'abc'] == [20,40,0]
    for pool, options in POOLS.values():
        assert options
    for p in POOLS:
        options = POOLS[p][1]
        one = [bet('a', options[0], 19)]
        assert settle_pool(p, one, [2,2,2], 'x')['users']['a']['returned'] == 19
    assert validate([{'pool':'combo','option':'5+2','amount':1}])[0]['option'] == '2+5'
    for a in (0, -1, True, 1.5, MAX_INT+1):
        with pytest.raises(PlayError): validate([{'pool':'size','option':'大','amount':a}])


def test_real_signature_tampering_and_future_round():
    assert beacon.verify(SIGNED, SIGNED['round']) == SIGNED
    for field, value in [('round', SIGNED['round']+1), ('signature', '00'*96), ('previous_signature', '00'*96), ('randomness', '00'*32)]:
        with pytest.raises(ValueError): beacon.verify({**SIGNED, field:value}, SIGNED['round'])
    now = beacon.round_time(SIGNED['round'])
    for offset in (0, 0.1, 29.9, 30):
        assert beacon.round_time(beacon.future_round(now+offset)) > now+offset
    seed, dice = beacon.derive('00'*32, '00'*12, SIGNED)
    assert (seed, dice) == ('f9d4db2a375f128df3cbf209d137b0727ee72c500da53e8f64bf1b4ac105e1ea', [1,1,2])
    assert beacon.derive('00'*32, '00'*12, SIGNED) == (seed, dice)


def test_actual_bot_seven_pools_multi_draft_append_opposite_cleanup_and_privacy(env):
    async def run():
        initial = cash(env)
        row = await create(env)
        assert row['state'] == 'open' and row['deadline']-row['created'] == 90
        assert row['timeout']-row['deadline'] == 600
        assert beacon.round_time(row['round_number']) > row['deadline']
        origin = env.tg.text(GROUP, row['card_mid'])
        assert row['commitment'] in origin and '余额' not in origin
        s = await enter(env, row)
        mid = s['mid']
        for pool in POOLS:
            s = await add(env, s, pool, POOLS[pool][1][0])
            assert s['mid'] == mid
            s = await click(env, s, 'home')
        assert cash(env) == initial
        s = await click(env, s, 'draft')
        assert '确认后不可撤注' in env.tg.text(int(s['tg_id']), mid)
        original = card(env, s)
        s = await click(env, s, 'confirm')
        assert env.db.one('SELECT COUNT(*) n FROM dice_bets')['n'] == 7, env.bot._last_error
        ledger = env.services.points.ledger(env.uids[0])
        await env.bot._dispatch_update({'callback_query': {'id':'repeat', 'data':next(b['callback_data'] for line in original['reply_markup']['inline_keyboard'] for b in line if b['text']=='确认全部'), 'from':env.actors[0], 'message':original}})
        assert env.services.points.ledger(env.uids[0]) == ledger
        s = await click(env, s, 'new')
        s = await add(env, s, 'size', '小', 17)
        s = await click(env, s, 'confirm')
        assert env.db.one('SELECT SUM(amount) n FROM dice_bets')['n'] == 87
        assert cash(env) == initial
        assert env.services.points.balance(env.uids[0]) == 913
        # Unrelated private numeric text (no exact card reply) is never a dice cleanup target.
        await env.bot._dispatch_update({'message':message(env, '123', private=True, mid=9999)})
        assert not env.db.one("SELECT 1 FROM play_jobs WHERE kind='dice.delete' AND payload_json LIKE '%9999%'")
        env.db.execute('UPDATE dice_rounds SET deadline=?,timeout=? WHERE nonce=?', (time.time()-601,time.time()-1,row['nonce']))
        await env.bot._dice_tick()
        row = svc(env).get(row['nonce'])
        assert row['state'] == 'refunded', row['error']
        assert cash(env) == initial
        assert env.services.points.balance(env.uids[0]) == 1000
        reports = env.db.query("SELECT * FROM dice_messages WHERE purpose LIKE 'report:%'")
        assert len(reports) == 1 and reports[0]['state'] == 'sent'
        report = reports[0]['body']
        assert 'tg://user?id=' in report and '本局全部退回' in report
        assert '余额' not in report and '913' not in report and 'private-login' not in report
        assert row['cleanup_queued'] == 1
        jobs = env.db.query("SELECT * FROM play_jobs WHERE kind='dice.delete'")
        targets = [json.loads(j['payload_json']) for j in jobs]
        assert any(t['message_id'] == row['card_mid'] and t['chat_id'] == GROUP for t in targets)
        assert not any(t['message_id'] == reports[0]['mid'] and t['chat_id'] == GROUP for t in targets)
        assert any(t['message_id'] == mid and t['chat_id'] == int(s['tg_id']) for t in targets)
        assert env.db.one("SELECT COUNT(*) n FROM dice_messages WHERE purpose LIKE 'detail:%' AND state='sent'")['n'] == 1
        before = len(env.services.points.ledger(env.uids[0]))
        await env.bot._dice_tick()
        assert len(env.services.points.ledger(env.uids[0])) == before
    asyncio.run(run())


def test_identity_wrong_cards_and_disabled_drafts(env):
    async def run():
        row = await create(env)
        s = await enter(env, row)
        await click(env, s, 'pool', 'size', index=1)
        assert svc(env).session(s['token'])['revision'] == 0
        wrong = card(env, s); wrong['message_id'] += 1
        await click(env, s, 'pool', 'size', original=wrong)
        assert svc(env).session(s['token'])['revision'] == 0
        s = await add(env, s, 'size', '大', 10)
        env.services.registry.save('dice', enabled=False)
        s = await click(env, s, 'confirm')
        assert env.db.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 0
        assert env.services.points.balance(env.uids[0]) == 1000
        env.db.execute('UPDATE dice_rounds SET deadline=0 WHERE nonce=?',(row['nonce'],))
        await env.bot._dice_tick()
        assert svc(env).get(row['nonce'])['state'] == 'empty'
    asyncio.run(run())


def test_atomic_amount_limits_duplicates_boundary_and_shared_balance(env):
    row = open_service(env)
    s = draft(env, row, 0, [{'pool':'size','option':'大','amount':501}, {'pool':'total','option':'12','amount':500}])
    before = cash(env)
    with pytest.raises(PlayError): svc(env).confirm(s, env.actors[0])
    assert cash(env) == before and not env.db.query('SELECT * FROM dice_tickets')
    s = draft(env, row, 0, [{'pool':'size','option':'大','amount':700}])
    def confirm(_):
        try: return svc(env).confirm(s, env.actors[0])
        except PlayError: return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(confirm, range(8)))
    assert env.db.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 1
    assert env.services.points.balance(env.uids[0]) == 300
    s2 = draft(env, row, 1, [{'pool':'size','option':'小','amount':10}])
    with pytest.raises(PlayError): svc(env).confirm(s2, env.actors[1], now=row['deadline'])
    assert env.services.points.balance(env.uids[1]) == 1000
    assert cash(env) == before


def test_persist_real_fixed_result_never_refund_or_reroll_after_timeout(env):
    scheduled = beacon.round_time(SIGNED['round'])
    row = open_service(env, now=scheduled-100)
    assert row['round_number'] == SIGNED['round']
    s = draft(env, row, 0, [{'pool':p,'option':o,'amount':1} for p, (_,opts) in POOLS.items() for o in opts], now=scheduled-95)
    svc(env).confirm(s, env.actors[0], now=scheduled-94)
    svc(env).close(row['nonce'], now=scheduled)
    fixed = svc(env).fix(row['nonce'], SIGNED, now=scheduled+1)
    assert fixed['state'] == 'fixed'
    stored = fixed['dice'], fixed['seed'], fixed['evidence']
    out = svc(env).settle(row['nonce'], now=scheduled+1000)
    assert out['state'] == 'settled' and (out['dice'],out['seed'],out['evidence']) == stored
    ledger = env.services.points.ledger(env.uids[0])
    svc(env).settle(row['nonce'], now=scheduled+2000)
    assert env.services.points.ledger(env.uids[0]) == ledger
    assert env.db.one('SELECT SUM(held) n FROM dice_pools')['n'] == 0
    assert env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='dice'")['n'] == 0
    proof = private_card(svc(env).session(s['token']) | {'payload':encode({'view':'proof'})}, out, 0, [], json.loads(out['result']))[0]
    assert SIGNED['signature'] in proof and row['secret'] in proof


def test_restart_and_cross_connection_balance_serialization(env):
    row = open_service(env)
    other = open_service(env, mid=9002, index=1, thread=22)
    a = draft(env, row, 0, [{'pool':'size','option':'大','amount':800}])
    b = draft(env, other, 0, [{'pool':'size','option':'小','amount':800}])
    # Independent connection models another worker/process, not only a Python mutex.
    dbfile = env.db.one('PRAGMA database_list')['file']
    db2 = Database(dbfile)
    groups = GroupService(db2)
    service2 = DiceService(db2, MemberService(db2, groups), PointsService(db2), lambda:True, lambda c:True, '123')
    try:
        def place(pair):
            service, s = pair
            try: service.confirm(s, env.actors[0]); return True
            except PlayError: return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted = list(executor.map(place, [(svc(env), a), (service2, b)]))
        assert sum(accepted) == 1
        assert env.services.points.balance(env.uids[0]) == 200
        for r in (row, other):
            service2.close(r['nonce'], now=r['timeout'])
            service2.settle(r['nonce'], now=r['timeout'])
        assert env.services.points.balance(env.uids[0]) == 1000
    finally:
        db2.close()


def test_report_unknown_does_not_resend_or_delete_original(env):
    async def run():
        row = await create(env)
        s = await enter(env,row)
        s = await add(env,s,'size','大')
        await click(env,s,'confirm')
        real = env.bot._call
        async def lost(method, payload=None, **kwargs):
            if method == 'sendMessage' and payload['chat_id'] == GROUP and '本局全部退回' in payload.get('text',''):
                CALL_DELIVERY.set({'state':'unknown','reason':'simulated lost ACK'})
                return None
            return await real(method,payload,**kwargs)
        env.bot._call = lost
        env.db.execute('UPDATE dice_rounds SET deadline=0,timeout=0 WHERE nonce=?',(row['nonce'],))
        await env.bot._dice_tick()
        report = env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")
        assert report['state'] == 'unknown'
        assert svc(env).get(row['nonce'])['cleanup_queued'] == 0
        env.bot._call = real
        await env.bot._dice_tick()
        assert env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")['state'] == 'unknown'
        assert not env.db.one("SELECT * FROM play_jobs WHERE job_key=?", (f'dice:{GROUP}:{row["card_mid"]}',))
    asyncio.run(run())


def test_deletion_kind_isolation_and_paginated_mentions(env):
    cleanup = DiceCleanup(env.db)
    cleanup.enqueue('123', 910, 0, 12345, 'command', '910', now=100)
    assert CleanupServiceKind(env) == {'dice.delete'}
    assert cleanup.claim('123', now=159) is None
    assert cleanup.claim('123', now=160)['kind'] == 'dice.delete'
    row = {'nonce':'a'*24,'state':'settled','dice':'[3,4,5]','result':encode({p:{'reason':'','users':{str(i):{'net':i-15} for i in range(60)}} for p in POOLS})}
    tickets = [{'user_id':str(i),'tg_id':str(100+i),'display_name':f'<昵称{i}>'} for i in range(60)]
    pages = report_pages(row,tickets,'ExampleBot')
    assert len(pages) == 3 and sum(p.count('tg://user?id=') for p in pages) == 60
    assert all(len(p)<4096 and '余额' not in p for p in pages)


def CleanupServiceKind(env):
    return {r['kind'] for r in env.db.query('SELECT kind FROM play_jobs')}


@pytest.mark.skipif(os.environ.get('DICE_LIVE_BEACON') != '1', reason='opt-in public beacon network; never Telegram')
def test_live_future_beacon_real_worker_end_to_end(env):
    async def run():
        initial = cash(env)
        row = await create(env)
        assert beacon.round_time(row['round_number']) > time.time()+89
        captures = {'open': {'body':env.tg.text(GROUP,row['card_mid']), 'keyboard':env.tg.actions(GROUP,row['card_mid'])}}
        for index in (0,1):
            s = await enter(env,row,index)
            if index == 0:
                captures['private'] = {'body':env.tg.text(int(s['tg_id']),s['mid']), 'keyboard':env.tg.actions(int(s['tg_id']),s['mid'])}
            for pool in POOLS:
                s = await add(env,s,pool,POOLS[pool][1][0 if index == 0 else -1],10,index)
                s = await click(env,s,'home',index=index)
            s = await click(env,s,'draft',index=index)
            if index == 0:
                captures['draft'] = {'body':env.tg.text(int(s['tg_id']),s['mid']), 'keyboard':env.tg.actions(int(s['tg_id']),s['mid'])}
            await click(env,s,'confirm',index=index)
        assert env.db.one('SELECT COUNT(*) n FROM dice_bets')['n'] == 14
        await asyncio.sleep(max(0,beacon.round_time(row['round_number'])-time.time()+2))
        for _ in range(12):
            await env.bot._dice_tick()
            out = svc(env).get(row['nonce'])
            if out['state'] == 'settled':
                break
            await asyncio.sleep(5)
        assert out['state'] == 'settled', out
        evidence = json.loads(out['evidence'])
        assert beacon.verify(evidence,row['round_number']) == evidence
        assert beacon.derive(out['secret'],out['nonce'],evidence) == (out['seed'],json.loads(out['dice']))
        assert cash(env) == initial
        assert env.db.one('SELECT SUM(held) n FROM dice_pools')['n'] == 0
        reports = env.db.query("SELECT * FROM dice_messages WHERE purpose LIKE 'report:%'")
        assert reports and all(r['state']=='sent' for r in reports)
        assert env.db.one("SELECT COUNT(*) n FROM dice_messages WHERE purpose LIKE 'detail:%' AND state='sent'")['n'] == 2
        before = env.services.points.balances()
        await env.bot._dice_tick()
        assert env.services.points.balances() == before
        output = Path('../artifacts/dice-game/开发预览')
        output.mkdir(parents=True,exist_ok=True)
        captures['result'] = {'body':reports[0]['body'],'keyboard':[]}
        captures['personal_result'] = {'body':env.db.one("SELECT body FROM dice_messages WHERE purpose LIKE 'detail:%' ORDER BY id LIMIT 1")['body'],'keyboard':[]}
        (output/'live-render.json').write_text(json.dumps(captures,ensure_ascii=False,indent=2))
        record = {k:out[k] for k in ('nonce','created','deadline','timeout','round_number','commitment','secret','seed','dice','evidence','state','result')}
        record.update(chain=beacon.CHAIN,public_key=beacon.PUBLIC_KEY,scheme=beacon.SCHEME,version=beacon.VERSION, verified_at=time.time(), total_conserved=initial)
        (output/'live-beacon.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
    asyncio.run(run())


def test_financial_failure_rolls_back_and_fixed_result_recovers(env, monkeypatch):
    scheduled = beacon.round_time(SIGNED['round'])
    row = open_service(env, now=scheduled-100)
    for index, option in ((0,'大'),(1,'小')):
        s = draft(env,row,index,[{'pool':'size','option':option,'amount':100}],now=scheduled-95)
        svc(env).confirm(s,env.actors[index],now=scheduled-94)
    svc(env).close(row['nonce'],now=scheduled)
    fixed = svc(env).fix(row['nonce'],SIGNED,now=scheduled+1)
    balances = env.services.points.balances()
    original = env.bot._points._apply
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('power loss after ledger write')
    monkeypatch.setattr(env.bot._points,'_apply',crash)
    with pytest.raises(RuntimeError): svc(env).settle(row['nonce'],now=scheduled+2)
    assert env.services.points.balances() == balances
    assert env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='dice'")['n'] == 200
    assert svc(env).get(row['nonce'])['state'] == 'fixed'
    monkeypatch.setattr(env.bot._points,'_apply',original)
    result = svc(env).settle(row['nonce'],now=scheduled+10000)
    assert result['state'] == 'settled' and result['dice'] == fixed['dice']
    assert sum(env.services.points.balances().values()) == sum(balances.values())+200


def test_bad_beacon_never_changes_fixed_round_and_timeout_refunds(env):
    scheduled = beacon.round_time(SIGNED['round'])
    row = open_service(env,now=scheduled-100)
    s = draft(env,row,0,[{'pool':'single','option':'2','amount':100}],now=scheduled-95)
    svc(env).confirm(s,env.actors[0],now=scheduled-94)
    svc(env).close(row['nonce'],now=scheduled)
    for bad in ({**SIGNED,'round':SIGNED['round']+1},{**SIGNED,'randomness':'00'*32}):
        with pytest.raises(ValueError): svc(env).fix(row['nonce'],bad,now=scheduled+1)
    current = svc(env).get(row['nonce'])
    assert current['round_number'] == row['round_number'] and current['dice'] == '[]'
    assert svc(env).settle(row['nonce'],now=row['timeout']-0.1)['state'] == 'locked'
    assert svc(env).settle(row['nonce'],now=row['timeout'])['state'] == 'refunded'
    with pytest.raises(PlayError): svc(env).fix(row['nonce'],SIGNED,now=row['timeout']+1)
    assert env.services.points.balance(env.uids[0]) == 1000


def test_open_and_private_lost_ack_recover_only_authentic_exact_callback(env):
    async def run():
        real = env.bot._call
        async def lose(method,payload=None,**kw):
            result = await real(method,payload,**kw)
            if method == 'sendMessage' and '骰局' in payload.get('text',''):
                CALL_DELIVERY.set({'state':'unknown','reason':'lost ack after accepted'})
                return None
            return result
        env.bot._call = lose
        row = await create(env)
        assert row['state'] == 'opening' and row['card_mid'] is None
        job = env.db.one("SELECT * FROM dice_messages WHERE purpose='open'")
        assert job['state'] == 'unknown'
        # FakeTelegram's exact successful-send storage, not a guessed previous message ID.
        candidates = [(k,v) for k,v in env.tg.messages.items() if str(k[0])==str(GROUP) and '等待封盘' in v.get('text','')]
        key,m = candidates[-1]
        m = copy.deepcopy(m)
        m.update(chat={'id':GROUP,'type':'supergroup'},message_id=key[1],message_thread_id=21,is_topic_message=True)
        m['from'] = {'id':123,'is_bot':True}
        env.bot._call = real
        invalid = copy.deepcopy(m); invalid['message_thread_id'] = 99
        await env.bot._dispatch_update({'callback_query':{'id':'bad','data':'dcg:'+row['nonce'],'from':env.actors[0],'message':invalid}})
        assert svc(env).get(row['nonce'])['card_mid'] is None
        await env.bot._dispatch_update({'callback_query':{'id':'good','data':'dcg:'+row['nonce'],'from':env.actors[0],'message':m}})
        assert svc(env).get(row['nonce'])['card_mid'] == key[1]
        env.bot._call = lose
        await env.bot._dispatch_update({'message':message(env,'/start dice_'+row['nonce'],private=True)})
        s = env.db.one('SELECT * FROM dice_sessions')
        assert s['mid'] is None
        candidates = [(k,v) for k,v in env.tg.messages.items() if str(k[0])==s['tg_id'] and '可用积分' in v.get('text','')]
        key,m = candidates[-1]
        m = copy.deepcopy(m); m.update(chat={'id':int(s['tg_id']),'type':'private'},message_id=key[1]); m['from']={'id':123,'is_bot':True}
        env.bot._call = real
        await click(env,s,'pool','size',original=m)
        assert svc(env).session(s['token'])['mid'] == key[1]
        assert env.db.one('SELECT COUNT(*) n FROM dice_delivery_audit')['n'] == 2
    asyncio.run(run())


def test_amount_context_replay_and_stale_reply_cannot_move_draft(env):
    async def run():
        row = await create(env)
        s = await enter(env,row)
        s = await click(env,s,'pool','single')
        s = await click(env,s,'pick','0')
        old = card(env,s)
        s = await click(env,s,'home')
        s = await click(env,s,'pool','single')
        s = await click(env,s,'pick','1')
        m = message(env,'37',private=True,mid=8101); m['reply_to_message']=old
        await env.bot._dispatch_update({'message':m})
        assert json.loads(svc(env).session(s['token'])['payload'])['draft'] == []
        s = svc(env).session(s['token']); m['message_id']=8102; m['reply_to_message']=card(env,s)
        await env.bot._dispatch_update({'message':m})
        await env.bot._dispatch_update({'message':m})
        draft = json.loads(svc(env).session(s['token'])['payload'])['draft']
        assert draft == [{'pool':'single','option':'2','amount':37}]
        assert env.db.one('SELECT COUNT(*) n FROM dice_inputs')['n'] == 1
    asyncio.run(run())


def test_public_report_pages_retry_only_unsent_and_controlled_ack(env):
    async def run():
        row = await create(env)
        env.db.execute('UPDATE dice_rounds SET deadline=0 WHERE nonce=?',(row['nonce'],))
        svc(env).close(row['nonce']); row=svc(env).settle(row['nonce'])
        with economy_write(env.db) as conn:
            svc(env).queue_message(conn,row['nonce'],'report:0',GROUP,'page zero',thread=21)
            svc(env).queue_message(conn,row['nonce'],'report:1',GROUP,'page one',thread=21)
        real = env.bot._call
        async def uncertain(method,payload=None,**kw):
            if method=='sendMessage' and payload.get('text')=='page one':
                CALL_DELIVERY.set({'state':'unknown','reason':'lost'})
                return None
            return await real(method,payload,**kw)
        env.bot._call=uncertain
        await env.bot._dice_deliver(row['nonce'])
        zero=env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")
        one=env.db.one("SELECT * FROM dice_messages WHERE purpose='report:1'")
        assert zero['state']=='sent' and one['state']=='unknown'
        assert svc(env).get(row['nonce'])['cleanup_queued']==0
        env.bot._call=real
        svc(env).recover_delivery(one['id'],confirmed_mid=7777)
        await env.bot._dice_deliver(row['nonce'])
        assert svc(env).get(row['nonce'])['cleanup_queued']==1
        assert env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")['mid']==zero['mid']
        assert env.db.one('SELECT COUNT(*) n FROM dice_delivery_audit')['n']==1
    asyncio.run(run())


def test_opening_restart_ack_gate_and_edit_failure_never_rededuct(env):
    async def run():
        service=svc(env)
        row=service.create(message(env))
        s=draft(env,row,0,[{'pool':'size','option':'大','amount':10}])
        with pytest.raises(PlayError): service.confirm(s,env.actors[0])
        assert env.services.points.balance(env.uids[0])==1000
        await env.bot._dice_tick()
        row=service.get(row['nonce'])
        assert row['state']=='open' and row['card_mid']
        s=await enter(env,row)
        env.tg.edit_error='simulated edit outage'
        s=await click(env,s,'confirm')
        assert env.services.points.balance(env.uids[0])==990
        assert s['rendered_revision']<s['revision']
        mid=s['mid']
        env.tg.edit_error=''
        await env.bot._dice_tick()
        s=service.session(s['token'])
        assert s['mid']==mid and s['rendered_revision']==s['revision']
        assert '下注已确认' in env.tg.text(int(s['tg_id']),mid)
        assert env.services.points.balance(env.uids[0])==990
    asyncio.run(run())


def test_safe_integer_capacity_is_not_an_artificial_bet_cap(env):
    row=open_service(env)
    env.services.points.add(env.uids[0],MAX_INT-1000,'isolated.boundary')
    s=draft(env,row,0,[{'pool':'size','option':'大','amount':MAX_INT}])
    svc(env).confirm(s,env.actors[0])
    assert env.services.points.balance(env.uids[0])==0
    s=draft(env,row,1,[{'pool':'total','option':'12','amount':1}])
    with pytest.raises(PlayError): svc(env).confirm(s,env.actors[1])
    assert env.services.points.balance(env.uids[1])==1000
    svc(env).close(row['nonce'],now=row['timeout'])
    assert svc(env).settle(row['nonce'],now=row['timeout'])['state']=='refunded'
    assert env.services.points.balance(env.uids[0])==MAX_INT


def test_dice_only_menu_and_full_help_are_reachable(env):
    async def run():
        env.services.registry.save('blackwhite',enabled=False)
        env.services.registry.save('niuniu',enabled=False)
        assert any(b['callback_data']=='play_hub' for line in env.bot.member_menu() for b in line)
        await env.bot._dispatch_update({'message':message(env,'/游戏',private=True)})
        assert any('骰局玩法' in p.get('text','') or any(b.get('text')=='骰局玩法' for line in p.get('reply_markup',{}).get('inline_keyboard',[]) for b in line) for method,p in env.tg.calls if method=='sendMessage')
        await env.bot._dispatch_update({'message':message(env,'/猜大小帮助',private=True,mid=8998)})
        text=next(p['text'] for method,p in reversed(env.tg.calls) if method=='sendMessage')
        assert '/猜大小 1000' in text and '发送即扣款' in text and '180秒' in text
        assert '赢返2倍' in text and '庄家通吃' in text
        assert env.db.one("SELECT 1 FROM play_jobs WHERE job_key=?", (f'dice:{env.actors[0]["id"]}:8998',))
    asyncio.run(run())


@pytest.mark.parametrize('raw',['0','-1','1.5','9'*20,str(MAX_INT+1)])
def test_trusted_invalid_amount_feedback_is_cleaned_without_debit(env,raw):
    async def run():
        row=await create(env)
        s=await enter(env,row)
        s=await click(env,s,'pool','single')
        s=await click(env,s,'pick','0')
        m=message(env,raw,private=True,mid=8500); m['reply_to_message']=card(env,s)
        await env.bot._dispatch_update({'message':m})
        latest=svc(env).session(s['token'])
        assert json.loads(latest['payload'])['draft']==[]
        assert env.services.points.balance(env.uids[0])==1000
        assert env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?',(f'dice:{s["tg_id"]}:8500',))
        assert '整数' in env.tg.text(int(s['tg_id']),s['mid'])
    asyncio.run(run())


def test_upgrade_retains_seven_pool_escrow_rules_and_history(env):
    """An already funded v0.42 record is reopened/migrated, never reinterpreted."""
    from app.modules.dice import migrate
    original_total = cash(env)
    row = open_service(env)
    for index in range(2):
        s = draft(env, row, index, [{'pool': pool, 'option': options[index], 'amount': 11+index}
                                   for pool, (_, options) in POOLS.items()])
        svc(env).confirm(s, env.actors[index])
    before = {name: env.db.query('SELECT * FROM '+name) for name in ('dice_tickets', 'dice_bets', 'dice_pools', 'play_escrows')}
    rules = svc(env).get(row['nonce'])['rules']
    migrate(env.db)
    assert all(env.db.query('SELECT * FROM '+name) == records for name, records in before.items())
    assert svc(env).get(row['nonce'])['rules'] == rules
    assert json.loads(rules)['window'] == 90
    db2 = Database(env.db.one('PRAGMA database_list')['file'])
    try:
        service = DiceService(db2, MemberService(db2, GroupService(db2)), PointsService(db2), lambda: True, lambda c: True, '123')
        service.close(row['nonce'], now=row['deadline'])
        db2.execute("UPDATE dice_rounds SET state='fixed',seed=?,dice=? WHERE nonce=?", ('ab'*32, '[4,5,6]', row['nonce']))
        done = service.settle(row['nonce'], now=row['deadline'])
        assert set(json.loads(done['result'])) == set(POOLS)
        assert service.get(row['nonce'])['rules'] == rules
        assert db2.query('SELECT * FROM dice_tickets') == before['dice_tickets']
        assert db2.query('SELECT * FROM dice_bets') == before['dice_bets']
        assert sum(PointsService(db2).balances().values()) == original_total
        session = service.new_session(row['nonce'], env.actors[0])
        assert json.loads(session['payload'])['view'] == 'mine'
    finally:
        db2.close()
