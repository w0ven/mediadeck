"""A same-value explicit Bot administrator operation owns the group intention."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from test_economy_bot import env as bot_env  # noqa: F401
from test_tg_interaction_context import ADMIN, VIEWER, command
from test_tg_interaction_context import env as interaction_env  # noqa: F401

from app.modules.economy_rules import DEFAULT_WHITELIST_CARD


@pytest.fixture
def env(request):
    return request.getfixturevalue('bot_env')


def test_actual_prouser_same_group_takes_over_timed_card_without_later_restore(env, monkeypatch):
    now = int(time.time())
    item = env.services.shop.create(dict(DEFAULT_WHITELIST_CARD, duration_days=1))
    env.services.points.add('u1', 5000, 'isolated.test')
    cid = env.services.shop.redeem('u1', item['id'])['card_id']
    env.services.bag.use('u1', cid)
    version = env.members.get('u1')['group_revision']
    asyncio.run(command(env, '/prouser ' + str(VIEWER), chat=ADMIN, user=ADMIN))
    assert env.members.get('u1')['group_revision'] == version + 1
    monkeypatch.setattr('time.time', lambda: now + 86402)
    sync = AsyncMock(return_value={'ok': True})
    asyncio.run(env.services.bag.reconcile_expired(sync))
    assert env.members.get('u1')['group_id'] == 'whitelist'
    assert env.db.one('SELECT status FROM whitelist_card_grants')['status'] == 'protected'
    sync.assert_not_called()
