"""Multiple independent SQLite connections: money, identity and delivery states."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.points import PointsService
from app.modules.red_packets import DEFAULT_PACKET_CONFIG, PacketError, PacketService, allocations

GROUP, BOT = -10088, 555
NOW = 1791604800


def actor(index=1):
    return {'id': 1000 + index, 'is_bot': False}


def make_service(db, env):
    members = MemberService(db, GroupService(db))
    return PacketService(db, members, PointsService(db), lambda: env.cfg,
                         lambda: env.enabled, lambda c: c['id'] in (GROUP, GROUP - 1),
                         lambda m: 'admin' in m['roles'], BOT)


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr('time.time', lambda: clock[0])
    db = Database(tmp_path / 'packets.db')
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    for i in range(1, 18):
        members.upsert('u' + str(i), '隔离用户' + str(i), {'group_id': 'standard', 'roles': ['admin'] if i == 17 else []})
        members.bind_telegram('u' + str(i), str(1000 + i))
    points = PointsService(db)
    points.add('u1', 1000, 'isolated.test')
    points.add('u17', 50, 'isolated.test')
    e = SimpleNamespace(db=db, cfg=dict(DEFAULT_PACKET_CONFIG), enabled=True,
                        members=members, points=points, clock=clock)
    e.service = make_service(db, e)
    yield e
    db.close()


def command(sender=1, mid=20, thread=7):
    return {'chat': {'id': GROUP, 'type': 'supergroup'}, 'from': actor(sender),
            'message_id': mid, 'message_thread_id': thread}


def context(row, *, claim=False):
    return {'chat': {'id': int(row['chat_id']), 'type': 'supergroup'},
            'from': {'id': BOT, 'is_bot': True}, 'message_id': row['card_message_id'],
            'message_thread_id': row['thread_id'], 'reply_markup': {'inline_keyboard': [[
                {'callback_data': ('rpclaim:' if claim else 'rpok:') + row['nonce']}]]}}


def prepare(env, *, sender=1, total=100, parts=10, mode='random', audience='all', mid=20):
    row = env.service.prepare(command(sender, mid), total, parts, mode, audience)
    # Pre-upgrade snapshot fixture; new permanent behavior has its own actual-handler suite.
    env.db.execute('UPDATE red_packets SET permanent=0,thread_id=7 WHERE nonce=?',(row['nonce'],))
    assert env.service.bind_card(row['nonce'], mid + 100)
    return env.service.get(row['nonce'])


def confirm(env, **kwargs):
    row = prepare(env, **kwargs)
    return env.service.confirm(row['nonce'], actor(kwargs.get('sender', 1)), context(row))


def multi(env, count, callback):
    def work(index):
        db = Database(env.db.path)
        try:
            return callback(make_service(db, env), index)
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=min(count, 16)) as pool:
        return list(pool.map(work, range(count)))


@pytest.mark.parametrize('mode', ['random', 'equal'])
def test_confirm_and_claim_cross_connection_atomic_idempotency_conservation(env, mode):
    row = prepare(env, mode=mode)
    results = multi(env, 12, lambda s, _: s.confirm(row['nonce'], actor(), context(row)))
    assert {r['allocations_json'] for r in results} == {results[0]['allocations_json']}
    assert env.points.balance('u1') == 900
    assert len([r for r in env.points.ledger('u1') if r['reason'] == 'packet.reserve']) == 1
    values = json.loads(results[0]['allocations_json'])
    assert len(values) == 10 and sum(values) == 100 and min(values) >= 1
    if mode == 'equal':
        assert values == [10] * 10
    card = context(row, claim=True)
    with pytest.raises(PacketError, match='发起人不能'):
        env.service.claim(row['nonce'], actor(), card)
    first = multi(env, 8, lambda s, _: s.claim(row['nonce'], actor(2), card))
    assert len({r['amount'] for r in first}) == 1 and sum(not r['already'] for r in first) == 1
    def claim(s, i):
        try:
            return s.claim(row['nonce'], actor(i + 3), card)
        except PacketError:
            return {'ok': False}
    taken = multi(env, 14, claim)
    assert sum(r['ok'] for r in taken) == 9
    end = env.service.get(row['nonce'])
    assert end['status'] == 'exhausted' and end['remaining'] == 0 and end['claimed_count'] == 10
    assert sum(r['amount'] for r in env.db.query('SELECT * FROM red_packet_claims')) == 100
    assert sum(env.points.balance('u' + str(i)) for i in range(1, 18)) == 1050
    assert env.service.best(row['nonce'])['amount'] == max(values)
    env.clock[0] += 24 * 3600
    env.service.expire_due()
    assert env.service.get(row['nonce'])['status'] == 'exhausted'


def test_partial_ordinary_expiry_refunds_original_account_not_new_binding_and_is_idempotent(env):
    row = confirm(env, mode='equal')
    card = context(row, claim=True)
    for i in range(2, 6):
        env.service.claim(row['nonce'], actor(i), card)
    env.members.bind_telegram('u1', '50001')
    env.members.bind_telegram('u16', str(actor()['id']))
    env.clock[0] += 24 * 3600
    results = multi(env, 12, lambda s, _: s.expire(row['nonce']))
    assert all(r['status'] == 'expired' for r in results)
    assert results[0]['refunded'] == 60 and results[0]['voided'] == 0
    assert env.points.balance('u1') == 960 and env.points.balance('u16') == 0
    assert len([r for r in env.points.ledger('u1') if r['reason'] == 'packet.refund']) == 1
    assert sum(env.points.balance('u' + str(i)) for i in range(1, 18)) == 1050
    assert all(env.points.balance('u' + str(i)) == 10 for i in range(2, 6))


def test_admin_reward_only_credits_claimed_shares_never_debits_or_refunds_admin_and_audits(env):
    row = confirm(env, sender=17, mode='equal')
    assert row['funding'] == 'reward' and env.points.balance('u17') == 50
    for i in range(2, 6):
        env.service.claim(row['nonce'], actor(i), context(row, claim=True))
    env.clock[0] += 24 * 3600
    end = env.service.expire(row['nonce'])
    assert end['voided'] == 60 and end['refunded'] == 0
    assert env.points.balance('u17') == 50
    assert sum(env.points.balance('u' + str(i)) for i in range(1, 18)) == 1090
    assert all(env.points.ledger('u' + str(i))[0]['reason'] == 'packet.reward' for i in range(2, 6))
    audit = env.db.query("SELECT action,detail FROM audit_log WHERE action LIKE 'points.packet.reward.%'")
    assert len(audit) == 6
    assert {r['action'] for r in audit} == {'points.packet.reward.confirm', 'points.packet.reward.claim', 'points.packet.reward.expire'}
    assert all(row['nonce'] in r['detail'] for r in audit)


@pytest.mark.parametrize('changes', [
    {'total': 0}, {'total': -1}, {'total': True}, {'total': 1.5}, {'parts': '2.0'},
    {'total': 5001}, {'parts': 51}, {'total': 5, 'parts': 10}, {'parts': 0},
    {'total': 101, 'parts': 10, 'mode': 'equal'}, {'mode': 'fake'}, {'audience': 'admin'},
])
def test_invalid_requests_refused_without_intent_or_money(env, changes):
    args = {'total': 100, 'parts': 10, 'mode': 'random', 'audience': 'all'}
    args.update(changes)
    with pytest.raises(PacketError):
        env.service.prepare(command(), **args)
    assert not env.db.query('SELECT * FROM red_packets') and env.points.balance('u1') == 1000


def test_random_minimum_bounds_and_exact_sum_even_extreme_draws(monkeypatch):
    for endpoint in (lambda n: 0, lambda n: n - 1):
        monkeypatch.setattr('secrets.randbelow', endpoint)
        for total, count in ((1, 1), (50, 50), (5000, 50), (11, 10)):
            values = allocations(total, count, 'random')
            assert len(values) == count and min(values) >= 1 and sum(values) == total


@pytest.mark.parametrize('mutation', ['binding', 'role', 'status', 'config', 'off'])
def test_confirmation_revalidates_authority_and_rules_never_switches_funding(env, mutation):
    row = prepare(env)
    if mutation == 'binding':
        env.members.bind_telegram('u1', '59999')
    elif mutation == 'role':
        env.members.set_roles('u1', ['admin'])
    elif mutation == 'status':
        env.members.set_status('u1', 'suspended')
    elif mutation == 'config':
        env.cfg['max_total'] = 90
    else:
        env.enabled = False
    with pytest.raises(PacketError):
        env.service.confirm(row['nonce'], actor(), context(row))
    assert env.points.balance('u1') == 1000 and env.service.get(row['nonce'])['status'] == 'draft'


@pytest.mark.parametrize('mutation', ['chat', 'message', 'topic', 'forward', 'bot_sender', 'buttons', 'anonymous', 'bot', 'missing_is_bot', 'fake_id'])
def test_claim_context_and_reliable_actor_server_side_guards(env, mutation):
    row = confirm(env)
    msg, who = context(row, claim=True), actor(2)
    if mutation == 'chat':
        msg['chat']['id'] -= 1
    elif mutation == 'message':
        msg['message_id'] += 1
    elif mutation == 'topic':
        msg['message_thread_id'] += 1
    elif mutation == 'forward':
        msg['forward_origin'] = {'type': 'user', 'sender_user': {'id': BOT}}
    elif mutation == 'bot_sender':
        msg['from']['id'] += 1
    elif mutation == 'buttons':
        msg['reply_markup'] = {'inline_keyboard': []}
    elif mutation == 'anonymous':
        msg['sender_chat'] = {'id': GROUP}
    elif mutation == 'bot':
        who['is_bot'] = True
    elif mutation == 'missing_is_bot':
        who.pop('is_bot')
    else:
        who['id'] = str(who['id'])
    with pytest.raises(PacketError):
        env.service.claim(row['nonce'], who, msg)
    assert not env.db.query('SELECT * FROM red_packet_claims') and env.service.get(row['nonce'])['remaining'] == 100


def test_whitelist_only_and_valid_account_eligibility_and_rebinding_cannot_claim_again(env):
    row = confirm(env, audience='whitelist')
    card = context(row, claim=True)
    with pytest.raises(PacketError, match='白名单'):
        env.service.claim(row['nonce'], actor(2), card)
    env.members.upsert('u2', 'two', {'group_id': 'whitelist'})
    result = env.service.claim(row['nonce'], actor(2), card)
    env.members.bind_telegram('u2', '50002')
    env.members.upsert('u3', 'three', {'group_id': 'whitelist'})
    env.members.bind_telegram('u3', str(actor(2)['id']))
    again = env.service.claim(row['nonce'], actor(2), card)
    assert again['already'] and again['amount'] == result['amount'] and env.points.balance('u3') == 0
    env.members.upsert('u4', 'four', {'group_id': 'whitelist', 'status': 'suspended'})
    with pytest.raises(PacketError, match='有效'):
        env.service.claim(row['nonce'], actor(4), card)


def test_unknown_confirmation_ack_no_charge_and_publish_lost_ack_restart_keeps_single_escrow(env):
    row = env.service.prepare(command(), 100, 10)
    env.db.execute('UPDATE red_packets SET permanent=0 WHERE nonce=?',(row['nonce'],))
    row=env.service.get(row['nonce'])
    unknown = dict(row, card_message_id=120)
    with pytest.raises(PacketError, match='原Bot'):
        env.service.confirm(row['nonce'], actor(), context(unknown))
    assert env.points.balance('u1') == 1000 and not env.service.bind_card(row['nonce'], None)
    env.service.bind_card(row['nonce'], 120)
    row = env.service.get(row['nonce'])
    sent = env.service.confirm(row['nonce'], actor(), context(row))
    env.service.rendered(row['nonce'], sent['render_version'], False)
    retried = env.service.confirm(row['nonce'], actor(), context(row))
    assert retried['allocations_json'] == sent['allocations_json'] and env.points.balance('u1') == 900
    with pytest.raises(PacketError, match='领取按钮'):
        env.service.claim(row['nonce'], actor(2), context(row))
    # Successful unknown-ACK publish is proved by the real known card button.
    env.service.claim(row['nonce'], actor(2), context(row, claim=True))
    env.clock[0] += 10
    restart = Database(env.db.path)
    try:
        service = make_service(restart, env)
        pending = service.pending_renders()
        assert len(pending) == 1 and pending[0]['card_message_id'] == 120
        service.rendered(row['nonce'], sent['render_version'], True)  # stale rendering ACK cannot mark newer claim UI
        assert service.pending_renders()
        version = service.get(row['nonce'])['render_version']
        service.rendered(row['nonce'], version, True)
        assert not service.pending_renders()
        env.clock[0] = NOW + 24 * 3600
        service.expire_due()
        end = service.get(row['nonce'])
        assert end['refunded'] + sum(r['amount'] for r in restart.query('SELECT * FROM red_packet_claims')) == 100
    finally:
        restart.close()


def test_multi_connection_claim_vs_expiry_never_refunds_claimed_amounts(env):
    row = confirm(env)
    card = context(row, claim=True)
    def race(service, index):
        if index % 3 == 0:
            return service.expire(row['nonce'], now=row['expires_at'])
        try:
            return service.claim(row['nonce'], actor(index + 2), card, now=row['expires_at'] - 1)
        except PacketError:
            return {'ok': False}
    multi(env, 15, race)
    end = env.service.get(row['nonce'])
    claimed = sum(r['amount'] for r in env.db.query('SELECT * FROM red_packet_claims'))
    assert end['status'] == 'expired' and claimed + end['refunded'] == 100
    assert sum(env.points.balance('u' + str(i)) for i in range(1, 18)) == 1050


def test_expiry_failure_preserves_escrow_retry_and_resumes_after_restart(env, monkeypatch):
    row = confirm(env)
    env.clock[0] = row['expires_at']
    original = env.service._credit
    monkeypatch.setattr(env.service, '_credit', lambda *a: (_ for _ in ()).throw(PacketError('isolated unavailable')))
    assert env.service.expire_due() == 1
    failed = env.service.get(row['nonce'])
    assert failed['status'] == 'active' and failed['remaining'] == 100 and failed['settlement_error']
    assert env.points.balance('u1') == 900
    assert env.service.expire_due() == 0
    monkeypatch.setattr(env.service, '_credit', original)
    env.clock[0] += 60
    restart = Database(env.db.path)
    try:
        resumed = make_service(restart, env)
        resumed.expire_due()
        assert resumed.get(row['nonce'])['refunded'] == 100 and env.points.balance('u1') == 1000
        assert not resumed.get(row['nonce'])['settlement_error']
    finally:
        restart.close()


def test_draft_options_cancel_and_duplicate_command_no_financial_side_effect(env):
    row = prepare(env)
    edited = env.service.edit_draft(row['nonce'], actor(), context(row), 'mode', 'equal')
    assert edited['mode'] == 'equal'
    assert env.service.prepare(command(), 100, 10)['nonce'] == row['nonce']
    with pytest.raises(PacketError):
        env.service.edit_draft(row['nonce'], actor(2), context(row), 'total', 200)
    end = env.service.confirm(row['nonce'], actor(), context(row), cancel=True)
    assert end['status'] == 'cancelled' and env.points.balance('u1') == 1000
    assert env.service.confirm(row['nonce'], actor(), context(row))['status'] == 'cancelled'
    assert not env.db.query('SELECT * FROM red_packet_claims')


def test_telegram_group_admin_fields_never_confer_reward_and_reward_role_loss_refuses(env):
    msg = command()
    msg['from']['status'] = 'administrator'
    msg['from']['is_admin'] = True
    row = env.service.prepare(msg, 100, 10)
    assert row['funding'] == 'user'
    env.service.bind_card(row['nonce'], 120)
    row = env.service.get(row['nonce'])
    env.service.confirm(row['nonce'], msg['from'], context(row))
    assert env.points.balance('u1') == 900
    reward = prepare(env, sender=17, mid=21)
    env.members.set_roles('u17', [])
    with pytest.raises(PacketError, match='系统角色'):
        env.service.confirm(reward['nonce'], actor(17), context(reward))
    assert env.points.balance('u17') == 50 and env.service.get(reward['nonce'])['status'] == 'draft'


@pytest.mark.parametrize('mutation', ['bot', 'anonymous', 'channel', 'unbound', 'no_identity'])
def test_unreliable_sender_cannot_prepare_any_intent(env, mutation):
    msg = command()
    if mutation == 'bot':
        msg['from']['is_bot'] = True
    elif mutation == 'anonymous':
        msg['sender_chat'] = {'id': GROUP}
    elif mutation == 'channel':
        msg['chat']['type'] = 'channel'
    elif mutation == 'unbound':
        msg['from']['id'] = 599999
    else:
        msg['from'].pop('is_bot')
    with pytest.raises(PacketError):
        env.service.prepare(msg, 100, 10)
    assert not env.db.query('SELECT * FROM red_packets') and env.points.balance('u1') == 1000
