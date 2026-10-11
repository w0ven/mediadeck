"""Self-deletion admission shares the dice write boundary, not its settlement rules."""
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_dice import draft, open_service, svc
from test_dice import env as dice_env  # noqa: F401

from app.core.db import Database
from app.core.errors import ConfigError
from app.modules.account_deletion import deletion_in_progress, self_deletion_guard
from app.modules.dice import DiceService
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.play_money import PlayError
from app.modules.points import PointsService


@pytest.fixture
def env(request):
    return request.getfixturevalue('dice_env')


def test_unsettled_bet_blocks_delete_then_settlement_allows_it(env):
    row = open_service(env)
    s = draft(env, row, 0, [{'pool': 'size', 'option': '大', 'amount': 10}])
    svc(env).confirm(s, env.actors[0])
    for state in ('open', 'locked', 'fixed'):
        env.db.execute('UPDATE dice_rounds SET state=? WHERE nonce=?', (state, row['nonce']))
        with pytest.raises(ConfigError, match='未结算'), self_deletion_guard(env.db, env.uids[0]):
            pytest.fail('must not delete')
        assert not deletion_in_progress(env.db._conn, env.uids[0])
    env.db.execute("UPDATE dice_rounds SET state='locked' WHERE nonce=?", (row['nonce'],))
    svc(env).settle(row['nonce'], now=row['timeout'])
    assert env.services.points.balance(env.uids[0]) == 1000
    with self_deletion_guard(env.db, env.uids[0]):
        assert deletion_in_progress(env.db._conn, env.uids[0])
    assert not deletion_in_progress(env.db._conn, env.uids[0])


def test_delete_guard_blocks_bet_across_connection_and_releases_on_failure(env):
    row = open_service(env)
    s = draft(env, row, 0, [{'pool': 'size', 'option': '大', 'amount': 10}])
    db2 = Database(env.db.one('PRAGMA database_list')['file'])
    service = DiceService(db2, MemberService(db2, GroupService(db2)), PointsService(db2),
                          lambda: True, lambda c: True, '123')
    try:
        with pytest.raises(RuntimeError, match='remote failed'), self_deletion_guard(env.db, env.uids[0]):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(service.confirm, s, env.actors[0])
                with pytest.raises(PlayError, match='注销'):
                    future.result(timeout=5)
            assert not env.db.query('SELECT * FROM dice_bets')
            raise RuntimeError('remote failed')
        service.confirm(s, env.actors[0])
        assert env.db.query('SELECT * FROM dice_bets')
    finally:
        db2.close()


def test_persisted_guard_and_broken_check_fail_closed(env):
    env.db.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                   ('account.self_delete:' + env.uids[0], 'interrupted-operation'))
    with pytest.raises(ConfigError, match='核查'), self_deletion_guard(env.db, env.uids[0]):
        pytest.fail('must not delete')
    env.db.execute('DELETE FROM meta WHERE key=?', ('account.self_delete:' + env.uids[0],))
    env.db.execute('DROP TABLE dice_rounds')
    with pytest.raises(sqlite3.OperationalError), self_deletion_guard(env.db, env.uids[0]):
        pytest.fail('must not delete')
    assert not deletion_in_progress(env.db._conn, env.uids[0])


def test_bot_rejects_unsettled_delete_without_remote_call(env):
    row = open_service(env)
    s = draft(env, row, 0, [{'pool': 'size', 'option': '大', 'amount': 10}])
    svc(env).confirm(s, env.actors[0])
    chat = str(env.actors[0]['id'])
    # The test actor is an operator in the shared fixture; use a normal member role.
    env.services.members.set_roles(env.uids[0], [])
    calls, bodies = [], []

    async def remote(*a, **kw):
        calls.append(True)
        return True

    async def edit(chat, mid, body, keyboard=None):
        bodies.append(body)
        return True

    env.bot._emby.delete_selfservice_user = remote
    env.bot._edit = edit

    async def run():
        await env.bot._account_delete_callback('delete_account', chat, 731, chat)
        p = env.bot._pending[chat][2]
        await env.bot._account_delete_callback(f"delself:{p['nonce']}:confirm", chat, p['message_id'], chat)

    asyncio.run(run())
    assert not calls and '未结算' in bodies[-1]
    assert env.services.members.get(env.uids[0])
    assert env.db.query('SELECT * FROM dice_bets')
