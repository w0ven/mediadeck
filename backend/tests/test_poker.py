"""Actual update handlers/photos + real SQLite conservation, rivals and recovery."""
import asyncio
import copy
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from itertools import combinations
from pathlib import Path

import pytest
from PIL import Image
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.play_money import CashBook, PlayError
from app.modules.points import PointsService
from app.modules.poker import PokerService, strength
from app.modules.poker_image import face, render
from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def env(request):
    e = request.getfixturevalue('bw_env')
    # Legacy regression suite intentionally exercises original 500-point snapshots.
    e.services.registry.save('poker', enabled=True, config={'default_budget': 500})
    e.services.registry.get('poker').ctx.telegram = e.bot
    e.photo_calls = []
    async def multipart(method, fields, files, **kw):
        assert method == 'sendPhoto' and files['photo'][2] == 'image/png'
        image = files['photo'][1]
        with Image.open(BytesIO(image)) as parsed:
            assert parsed.format == 'PNG' and parsed.width == 780 and parsed.height >= 450
            parsed.verify()
        e.photo_calls.append({'fields': copy.deepcopy(fields), 'png': image})
        return await e.tg.call(method, fields)
    e.multipart = multipart
    e.bot._call_multipart = multipart
    return e


def service(env): return env.bot._poker_service()


def total(env):
    return sum(env.services.points.balances().values()) + (env.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0) + (env.db.one('SELECT SUM(amount) n FROM play_funds')['n'] or 0)


def command(env, text='/炸金花 10', index=0, mid=710, thread=22, private=False):
    actor = env.actors[index]
    return {'from': actor, 'chat': {'id': actor['id'] if private else GROUP, 'type': 'private' if private else 'supergroup'}, 'message_id': mid,
            'text': text, **({} if private else {'message_thread_id': thread}),
            **({'is_topic_message': True} if not private and thread else {})}


async def create(env, **kw):
    await env.bot._dispatch_update({'message': command(env, **kw)})
    return env.db.one("SELECT r.*,s.turn FROM play_rounds r JOIN poker_state s ON s.nonce=r.nonce ORDER BY r.created_at DESC LIMIT 1")


def card(env, row):
    body = copy.deepcopy(env.tg.message(GROUP, row['card_message_id']))
    body['chat'] = {'id': GROUP, 'type': 'supergroup'}
    body['from'] = {'id': 123, 'is_bot': True}
    body['message_id'] = row['card_message_id']
    body['reply_to_message'] = {'message_id': row['command_message_id']}
    if row['thread_id']:
        body['is_topic_message'] = True
    return body


def index_for(env, player): return next(i for i, a in enumerate(env.actors) if str(a['id']) == player['tg_id'])


def current(env, row=None):
    row = row or env.db.one("SELECT r.*,s.current_seat,s.turn FROM play_rounds r JOIN poker_state s ON r.nonce=s.nonce ORDER BY r.created_at DESC LIMIT 1")
    return next(p for p in service(env).players(row['nonce']) if p['seat'] == row['current_seat'])


async def click(env, row, op, index=None, target=None, message=None, turn=None):
    if index is None: index = index_for(env, current(env, row))
    if op in ('look', 'help'): data = ('pgl:' if op == 'look' else 'pgh:')+row['nonce']
    else:
        data = f'pg:{row["nonce"]}:{row["turn"] if turn is None else turn}:{op}' + (f':{target}' if target is not None else '')
    await env.bot._dispatch_update({'callback_query': {'id': f'poker-{op}', 'data': data, 'from': env.actors[index], 'message': message or card(env, row)}})
    return service(env).get(row['nonce'])


async def opened(env, n=3, **kw):
    row = await create(env, **kw)
    for i in range(1, n): row = await click(env, row, 'join', index=i)
    if n < 5: row = await click(env, row, 'start', index=0)
    assert row['state'] == 'running'
    return row


def c(rank, suit=0): return suit*13+rank-2


@pytest.mark.parametrize('cards,expected', [
    ([c(14), c(14, 1), c(14, 2)], (5, 14)),
    ([c(12), c(13), c(14)], (4, 14)),
    ([c(14), c(2), c(3)], (4, 3)),
    ([c(14), c(8), c(4)], (3, 14, 8, 4)),
    ([c(14), c(2, 1), c(3, 2)], (2, 3)),
    ([c(2), c(3, 1), c(4, 2)], (2, 4)),
    ([c(12), c(13, 1), c(14, 2)], (2, 14)),
    ([c(14), c(2, 1), c(13, 2)], (0, 14, 13, 2)),
    ([c(9), c(9, 1), c(14, 2)], (1, 9, 14)),
    ([c(2), c(3, 1), c(5, 2)], (0, 5, 3, 2)),
])
def test_confirmed_point_only_order(cards, expected): assert strength(cards) == expected


def test_all_22100_hands_classify_correctly_and_52_faces_are_unique():
    counts = [0]*6
    for hand in combinations(range(52), 3): counts[strength(hand)[0]] += 1
    assert counts == [16440, 3744, 720, 1096, 48, 52]
    assert len({hashlib.sha256(face(card).tobytes()).hexdigest() for card in range(52)}) == 52
    assert strength([c(2), c(3, 1), c(5, 2)]) < strength([c(2), c(2, 1), c(2, 2)])
    assert strength([c(14), c(14, 1), c(9, 2)]) == strength([c(14, 2), c(14, 3), c(9, 1)])
    with pytest.raises(ValueError): strength([0, 0, 1])


def test_real_handlers_all_operators_secret_png_and_only_surviving_hand_public(env):
    initial = total(env)
    async def run():
        row = await opened(env)
        mid = row['card_message_id']
        players = service(env).players(row['nonce'])
        assert len({card for p in players for card in json.loads(p['cards_json'])}) == 9
        assert env.db.one("SELECT amount FROM play_funds WHERE kind='poker'")['amount'] == 30
        assert all(env.services.points.balance(p['user_id']) == 500 for p in players)
        # A real administrator's full budget is frozen, not exempt.
        assert env.services.points.balance(env.uids[0]) == 500
        first = current(env, row)
        row = await click(env, row, 'look')
        private = env.photo_calls[-1]
        assert private['fields']['chat_id'] == first['tg_id'] and 'message_thread_id' not in private['fields']
        assert private['png'] == render([{'name': first['display_name'], 'cards': json.loads(first['cards_json'])}], private=True)
        assert service(env).get(row['nonce'])['expires_at'] == row['expires_at']
        # Secret callbacks do not carry cards or ranking. Only nonce/turn/action/seat.
        data = [b['callback_data'] for line in card(env, row)['reply_markup']['inline_keyboard'] for b in line]
        assert all(len(x.encode()) <= 64 for x in data)
        assert not any(k in env.tg.text(GROUP, mid) for k in ('cards_json', '豹子', '· 金花', '当前余额', 'private-login'))
        row = await click(env, row, 'follow')  # seen 2x10
        second = current(env, row)
        row = await click(env, row, 'raise')   # blind 20
        third = current(env, row)
        row = await click(env, row, 'double')  # blind 40
        assert current(env, row)['id'] == first['id']
        row = await click(env, row, 'fold')
        assert current(env, row)['id'] == second['id']
        row = await click(env, row, 'compare', target=third['seat'])  # 2x40
        assert row['state'] == 'settled' and row['card_message_id'] == mid
        assert json.loads(row['result_json'])['pot'] == 190
        survivors = [p for p in service(env).players(row['nonce']) if not p['folded']]
        assert len(survivors) == 1 and survivors[0]['result_amount'] == 190
        group = env.photo_calls[-1]
        assert group['fields']['chat_id'] == str(GROUP) and group['fields']['message_thread_id'] == 22
        job = env.db.one("SELECT * FROM play_photos WHERE mode='result'")
        content = json.loads(job['content_json'])
        assert len(content) == 1 and content[0]['cards'] == json.loads(survivors[0]['cards_json'])
        assert group['png'] == render(content)
        assert sum(p['invested'] for p in service(env).players(row['nonce'])) == 190
        assert env.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0
        assert env.db.one("SELECT amount FROM play_funds WHERE kind='poker'")['amount'] == 0
        assert len([1 for method, _ in env.tg.calls if method == 'sendMessage']) == 1
        if os.environ.get('GAMES_MARKET_ARTIFACTS'):
            d = Path(os.environ['GAMES_MARKET_ARTIFACTS'])
            (d/'poker-private.png').write_bytes(private['png'])
            (d/'poker-result.png').write_bytes(group['png'])
            (d/'poker-effect.json').write_text(json.dumps({'result': env.tg.text(GROUP, mid), 'private_png_actual_multipart': True, 'folded_hands_excluded': True}, ensure_ascii=False, indent=2)+'\n')
    asyncio.run(run())
    assert total(env) == initial


def test_lobby_budget_checks_no_cards_no_debit_idempotence_and_full_auto_start(env):
    initial = total(env)
    async def run():
        before = copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
        row = await create(env)
        same = await create(env)
        assert same['nonce'] == row['nonce'] and len(service(env).players(row['nonce'])) == 1
        for i in range(1, 5):
            row = await click(env, row, 'join', index=i)
            if i < 4:
                assert row['state'] == 'lobby'
                assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
                assert all(p['cards_json'] == '[]' for p in service(env).players(row['nonce']))
        assert row['state'] == 'running'
        assert len({card for p in service(env).players(row['nonce']) for card in json.loads(p['cards_json'])}) == 15
        for p in service(env).players(row['nonce']): assert env.services.points.balance(p['user_id']) == 500
        await click(env, row, 'join', index=5, turn=0)
        assert len(service(env).players(row['nonce'])) == 5
    asyncio.run(run())
    assert total(env) == initial


def test_member_drops_budget_before_start_no_partial_freeze_and_then_retry(env):
    async def run():
        row = await create(env)
        row = await click(env, row, 'join', index=1)
        env.services.points.add(env.uids[1], -600, 'isolated.fixture')
        before = copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
        row = await click(env, row, 'start', index=0)
        assert row['state'] == 'lobby' and row['start_error']
        assert not env.db.query('SELECT * FROM play_escrows')
        assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
        assert all(p['cards_json'] == '[]' for p in service(env).players(row['nonce']))
        env.services.points.add(env.uids[1], 100, 'isolated.fixture')
        row = await click(env, row, 'start', index=0)
        assert row['state'] == 'running'
    asyncio.run(run())


def test_private_failure_marked_seen_cost_doubles_then_retry_same_cards_no_debit(env, monkeypatch):
    async def run():
        row = await opened(env, n=2)
        first = current(env, row)
        saved_cards = first['cards_json']
        before = env.db.query('SELECT * FROM points_ledger ORDER BY id')
        failed_bytes = []
        async def unreachable(method, fields, files, **kw):
            CALL_DELIVERY.set({'state': 'failed'})
            failed_bytes.append(files['photo'][1])
        monkeypatch.setattr(env.bot, '_call_multipart', unreachable)
        deadline = row['expires_at']
        row = await click(env, row, 'look')
        assert row['expires_at'] == deadline
        p = next(p for p in service(env).players(row['nonce']) if p['id'] == first['id'])
        assert p['seen'] == 1 and p['cards_json'] == saved_cards
        assert env.db.one("SELECT state FROM play_photos WHERE mode='private'")['state'] == 'blocked'
        report=env.services.registry.card('poker')['live_status']
        assert report['牌图待处理']==1
        assert report['最近牌图异常']==env.db.one("SELECT last_error FROM play_photos WHERE mode='private'")['last_error']
        assert saved_cards not in json.dumps(report)
        assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
        row = await click(env, row, 'follow')
        assert env.db.one('SELECT amount FROM poker_actions')['amount'] == 20
        monkeypatch.setattr(env.bot, '_call_multipart', env.multipart)
        await env.bot._dispatch_update({'message': command(env, '/看牌 '+row['nonce'], index=index_for(env, first), private=True)})
        assert env.photo_calls[-1]['png'] == failed_bytes[0]
        assert env.db.one("SELECT state FROM play_photos WHERE mode='private'")['state'] == 'sent'
        assert env.services.registry.card('poker')['live_status']['牌图待处理']==0
        assert next(p for p in service(env).players(row['nonce']) if p['id'] == first['id'])['cards_json'] == saved_cards
        assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
        n = len(env.photo_calls)
        await click(env, row, 'look', index=index_for(env, first))
        assert len(env.photo_calls) == n  # ACKed private image is not duplicated
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['outsider', 'rebound', 'before_start', 'folded_unseen', 'foreign_private'])
def test_private_cards_and_callback_secrecy_bound_to_original_holder(env, invalid):
    async def run():
        row = await create(env) if invalid == 'before_start' else await opened(env)
        if invalid == 'folded_unseen':
            p = current(env, row)
            row = await click(env, row, 'fold')
            idx = index_for(env, p)
        else: idx = 0
        if invalid == 'outsider': idx = 6
        if invalid == 'rebound':
            env.members.upsert('replacement', 'private-new-login', {'group_id': 'standard'})
            env.members.bind_telegram('replacement', str(env.actors[0]['id']))
        before = len(env.photo_calls)
        if invalid == 'foreign_private':
            msg = command(env, '/看牌 '+row['nonce'], private=True)
            msg['chat']['id'] = env.actors[1]['id']
            await env.bot._dispatch_update({'message': msg})
        else:
            await env.bot._dispatch_update({'message': command(env, '/看牌 '+row['nonce'], index=idx, private=True)})
        assert len(env.photo_calls) == before
    asyncio.run(run())


@pytest.mark.parametrize('changed', ['chat', 'thread', 'mid', 'sender', 'buttons', 'forward'])
def test_poker_actions_original_group_topic_bot_card_no_spoof(env, changed):
    async def run():
        row = await opened(env)
        message = card(env, row)
        if changed == 'chat': message['chat']['id'] -= 1
        elif changed == 'thread': message['message_thread_id'] += 1
        elif changed == 'mid': message['message_id'] += 1
        elif changed == 'sender': message['from']['id'] += 1
        elif changed == 'buttons': message['reply_markup'] = {}
        else: message['forward_origin'] = {'type': 'user'}
        before = row['revision']
        row = await click(env, row, 'follow', message=message)
        assert row['revision'] == before and not env.db.query('SELECT * FROM poker_actions')
    asyncio.run(run())


def force_hands(env, row, values):
    for p, cards in zip(service(env).players(row['nonce']), values, strict=True):
        env.db.execute('UPDATE poker_hands SET cards_json=? WHERE player_id=?', (json.dumps(cards), p['id']))


def test_compare_equal_initiator_out_and_final_tie_fair_integer_split(env):
    async def run():
        env.services.registry.save('poker', config={'budget': 51, 'default_budget': 51})
        row = await opened(env)
        # Same pair/tiebreaker, different suits, distinct cards in the injected test deal.
        force_hands(env, row, [[c(8), c(8, 1), c(14)], [c(8, 2), c(8, 3), c(14, 1)], [c(2), c(7, 1), c(9, 2)]])
        players = service(env).players(row['nonce'])
        row = await click(env, row, 'compare', target=players[1]['seat'])
        assert row['state'] == 'running'
        assert next(p for p in service(env).players(row['nonce']) if p['id'] == players[0]['id'])['folded'] == 1
        # Another independent game after finish, highest equal hands share odd 55.
        row = await click(env, row, 'fold')
        assert row['state'] == 'settled'
        row = await create(env, text='/炸金花 11', mid=711)
        for i in range(1, 5): row = await click(env, row, 'join', index=i)
        force_hands(env, row, [[c(14), c(8), c(4)], [c(14, 1), c(8, 1), c(4, 1)], [c(7), c(7, 1), c(2)], [c(9), c(9, 1), c(3)], [c(6), c(6, 1), c(5)]])
        # Doubling blind to 22 costs 22, next to44 cannot fit remaining18 -> no further spend, cap.
        row = await click(env, row, 'double')
        for _ in range(4): row = await click(env, row, 'follow')
        row = await click(env, row, 'double')
        assert row['state'] == 'settled'
        players = service(env).players(row['nonce'])
        assert sorted(p['result_amount'] for p in players if p['result_amount']) == [82, 83]
        assert json.loads(row['result_json'])['pot'] == 165
        assert sum(p['invested'] for p in players) == 165
        assert all(p['invested'] <= 51 for p in players)
    asyncio.run(run())


def test_exact_budget_limit_and_unchanged_configuration_snapshots(env):
    async def run():
        env.services.registry.save('poker', config={'max_ante': 10, 'budget': 20, 'default_budget': 20})
        row = await opened(env, n=2)
        env.services.registry.save('poker', config={'budget': 500, 'step_seconds': 120})
        row = await click(env, row, 'follow')
        assert row['state'] == 'settled'
        assert json.loads(row['config_json'])['budget'] == 20
        assert json.loads(row['result_json'])['pot'] == 30
        assert env.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0
    asyncio.run(run())


def test_operation_timeout_and_plugin_closed_recovery_does_not_trap_money(env, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    initial = total(env)
    async def run():
        row = await opened(env)
        first = current(env, row)
        env.services.registry.save('poker', enabled=False)
        clock[0] = row['expires_at']
        await env.bot._poker_tick()
        row = service(env).get(row['nonce'])
        assert row['turn'] == 2
        assert next(p for p in service(env).players(row['nonce']) if p['id'] == first['id'])['folded']
        await env.bot._poker_tick()
        assert service(env).get(row['nonce'])['turn'] == 2
        row = await click(env, row, 'fold')  # closed plugin still exits/settles
        assert row['state'] == 'settled'
        assert total(env) == initial
        assert env.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0
    asyncio.run(run())


def test_lobby_timeout_cancel_leave_and_invalid_start_authority(env, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    async def run():
        row = await create(env)
        row = await click(env, row, 'start', index=0)
        assert row['state'] == 'lobby'
        row = await click(env, row, 'join', index=1)
        row = await click(env, row, 'start', index=1)
        assert row['state'] == 'lobby'
        row = await click(env, row, 'leave', index=1)
        assert len(service(env).players(row['nonce'])) == 1
        clock[0] = row['expires_at']
        await env.bot._poker_tick()
        assert service(env).get(row['nonce'])['state'] == 'expired'
        assert not env.db.query('SELECT * FROM play_escrows') and not env.photo_calls
        row = await create(env, mid=713)
        row = await click(env, row, 'leave', index=0)
        assert row['state'] == 'cancelled'
    asyncio.run(run())


def local_service(env, db):
    return PokerService(db, MemberService(db, GroupService(db)), PointsService(db), lambda: env.services.registry.config('poker'), lambda: env.services.registry.enabled('poker'), lambda chat: str(chat.get('id')) == str(GROUP), '123')


def test_two_connections_start_and_same_turn_compete_and_duplicate_is_idempotent(env):
    async def prepare():
        row = await create(env)
        return await click(env, row, 'join', index=1)
    row = asyncio.run(prepare())
    original = total(env)
    dbs = [Database(env.db.path) for _ in range(2)]
    services = [local_service(env, d) for d in dbs]
    message = card(env, row)
    def start(i):
        try: return services[i].lobby(row['nonce'], env.actors[0], message, 'start')['state'] == 'running'
        except PlayError: return False
    with ThreadPoolExecutor(max_workers=2) as pool: assert sum(pool.map(start, range(2))) == 1
    row = service(env).get(row['nonce'])
    asyncio.run(env.bot._poker_publish(row['nonce']))
    message = card(env, row)
    actor = env.actors[index_for(env, current(env, row))]
    def follow(i): return services[i].action(row['nonce'], row['turn'], actor, message, 'follow')
    with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(follow, range(2)))
    assert env.db.one('SELECT COUNT(*) n FROM poker_actions')['n'] == 1
    assert env.db.one("SELECT amount FROM play_funds WHERE kind='poker'")['amount'] == 30
    assert env.db.one('SELECT SUM(reserved) n FROM play_escrows')['n'] == 1000
    assert total(env) == original
    for db in dbs: db.close()


def test_budget_transaction_fault_rolls_back_deal_and_all_accounts(env, monkeypatch):
    async def prepare():
        row = await create(env)
        return await click(env, row, 'join', index=1)
    row = asyncio.run(prepare())
    before = env.db.query('SELECT * FROM points_ledger ORDER BY id')
    original = CashBook.reserve
    calls = []
    def crash(self, *args, **kw):
        original(self, *args, **kw)
        calls.append(1)
        if len(calls) == 2: raise RuntimeError('injected after second budget ledger write')
    monkeypatch.setattr(CashBook, 'reserve', crash)
    with pytest.raises(RuntimeError): service(env).lobby(row['nonce'], env.actors[0], card(env, row), 'start')
    assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
    assert not env.db.query('SELECT * FROM play_escrows') and not env.db.query('SELECT * FROM play_funds')
    assert service(env).get(row['nonce'])['state'] == 'lobby'
    assert all(p['cards_json'] == '[]' for p in service(env).players(row['nonce']))


def test_settlement_fault_restores_fold_and_all_money_then_retry(env, monkeypatch):
    row = asyncio.run(opened(env, n=2))
    before = env.db.query('SELECT * FROM points_ledger ORDER BY id')
    original = CashBook.release
    def crash(self, *args, **kw):
        original(self, *args, **kw)
        raise RuntimeError('injected after return ledger write')
    monkeypatch.setattr(CashBook, 'release', crash)
    p = current(env, row)
    with pytest.raises(RuntimeError): service(env).action(row['nonce'], row['turn'], env.actors[index_for(env, p)], card(env, row), 'fold')
    assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
    assert service(env).get(row['nonce'])['state'] == 'running'
    assert not any(p['folded'] for p in service(env).players(row['nonce']))
    assert not env.db.query('SELECT * FROM poker_actions') and not env.db.query("SELECT * FROM play_photos WHERE mode='result'")
    monkeypatch.setattr(CashBook, 'release', original)
    row = asyncio.run(click(env, row, 'fold'))
    assert row['state'] == 'settled'


def test_real_db_reopen_preserves_cards_round_and_photo_outbox_lease(env):
    row = asyncio.run(opened(env, n=2))
    p = current(env, row)
    actor = env.actors[index_for(env, p)]
    service(env).look(row['nonce'], actor)
    job = service(env).claim_photo(now=row['expires_at']-10)
    assert job['mode'] == 'private'
    db = Database(env.db.path)
    restored = local_service(env, db)
    assert restored.players(row['nonce']) == service(env).players(row['nonce'])
    assert restored.claim_photo(now=row['expires_at']-9) is None
    duplicate = restored.claim_photo(now=row['expires_at']+90)
    assert duplicate['content_json'] == job['content_json'] and duplicate['attempts'] == 2
    restored.photo_finished(job, 999)  # stale worker ACK cannot mark current attempt sent
    assert db.one('SELECT state FROM play_photos')['state'] == 'sending'
    restored.photo_finished(duplicate, 1000)
    assert db.one('SELECT state FROM play_photos')['state'] == 'sent'
    restored.expire_due(now=row['expires_at'])
    assert restored.get(row['nonce'])['state'] == 'settled'
    assert db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0
    db.close()


def test_photo_rebind_does_not_send_original_account_hand_to_new_holder(env):
    row = asyncio.run(opened(env, n=2))
    p = current(env, row)
    service(env).look(row['nonce'], env.actors[index_for(env, p)])
    env.members.upsert('new-photo-owner', 'private-name', {'group_id': 'standard'})
    env.members.bind_telegram('new-photo-owner', p['tg_id'])
    asyncio.run(env.bot._poker_photos())
    assert not env.photo_calls
    assert env.db.one('SELECT state FROM play_photos')['state'] == 'blocked'


def test_rival_follow_fold_is_serial_and_timeout_race_not_double_settle(env):
    row = asyncio.run(opened(env, n=2))
    before = total(env)
    dbs = [Database(env.db.path) for _ in range(2)]
    services = [local_service(env, d) for d in dbs]
    actor = env.actors[index_for(env, current(env, row))]
    message = card(env, row)
    def act(i):
        try:
            services[i].action(row['nonce'], row['turn'], actor, message, 'follow' if i == 0 else 'fold')
            return True
        except PlayError: return False
    with ThreadPoolExecutor(max_workers=2) as pool: assert sum(pool.map(act, range(2))) == 1
    assert env.db.one('SELECT COUNT(*) n FROM poker_actions')['n'] == 1
    row = service(env).get(row['nonce'])
    if row['state'] == 'running':
        asyncio.run(env.bot._poker_publish(row['nonce']))
        row = asyncio.run(click(env, row, 'fold'))
    row = asyncio.run(opened(env, n=2, mid=714))
    actor = env.actors[index_for(env, current(env, row))]
    message = card(env, row)
    def timeout(): services[0].expire(row['nonce'], now=row['expires_at'])
    def cutoff_action():
        with pytest.raises(PlayError): services[1].action(row['nonce'], row['turn'], actor, message, 'fold', now=row['expires_at'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(timeout), pool.submit(cutoff_action)
        a.result(); b.result()
    assert service(env).get(row['nonce'])['state'] == 'settled'
    assert env.db.one("SELECT COUNT(*) n FROM play_photos WHERE mode='result' AND nonce=?", (row['nonce'],))['n'] == 1
    assert total(env) == before
    for db in dbs: db.close()


def test_fifth_lobby_seat_race_freezes_only_winning_five_once(env):
    async def prepare():
        row = await create(env)
        for i in range(1, 4): row = await click(env, row, 'join', index=i)
        return row
    row = asyncio.run(prepare())
    before = total(env)
    dbs = [Database(env.db.path) for _ in range(2)]
    services = [local_service(env, d) for d in dbs]
    message = card(env, row)
    def join(i):
        try:
            services[i].lobby(row['nonce'], env.actors[4+i], message, 'join')
            return True
        except PlayError: return False
    with ThreadPoolExecutor(max_workers=2) as pool: assert sum(pool.map(join, range(2))) == 1
    assert service(env).get(row['nonce'])['state'] == 'running'
    assert len(service(env).players(row['nonce'])) == 5
    assert env.db.one('SELECT SUM(reserved) n FROM play_escrows')['n'] == 2500
    assert env.db.one("SELECT amount FROM play_funds WHERE kind='poker'")['amount'] == 50
    assert total(env) == before
    for db in dbs: db.close()


def test_network_lost_photo_ack_and_explicit_retry_same_png_without_new_deal(env, monkeypatch):
    async def run():
        row = await opened(env, n=2)
        owner = current(env, row)
        bytes_sent = []
        async def lost_ack(method, fields, files, **kw):
            bytes_sent.append(files['photo'][1])
            CALL_DELIVERY.set({'state': 'unknown'})
        monkeypatch.setattr(env.bot, '_call_multipart', lost_ack)
        row = await click(env, row, 'look')
        job = env.db.one("SELECT * FROM play_photos WHERE mode='private'")
        assert job['state'] == 'queued' and job['last_error']
        monkeypatch.setattr(env.bot, '_call_multipart', env.multipart)
        row = await click(env, row, 'look', index=index_for(env, owner))
        assert env.photo_calls[-1]['png'] == bytes_sent[0]
        assert env.db.one("SELECT attempts FROM play_photos WHERE mode='private'")['attempts'] == 2
        assert sum(p['invested'] for p in service(env).players(row['nonce'])) == 20
    asyncio.run(run())


def test_help_is_separate_actual_private_page_not_main_rules(env):
    async def run():
        row = await create(env)
        body = env.tg.text(GROUP, row['card_message_id'])
        for word in ('豹子', '管理员', 'A23', '看牌后', '同牌力'): assert word not in body
        await click(env, row, 'help', index=1)
        pages = [p for method, p in env.tg.calls if method == 'sendMessage' and str(p['chat_id']) == str(env.actors[1]['id'])]
        assert 'A23' in pages[-1]['text'] and '235' in pages[-1]['text'] and '同牌力' in pages[-1]['text']
        with pytest.raises(ValueError): env.services.registry.save('poker', config={'budget': 20})
    asyncio.run(run())
