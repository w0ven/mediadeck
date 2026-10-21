"""Paid viewing-date labels, not pending credit or arrival-date estimates."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from test_activity_bot import (  # noqa: F401
    bot_env,
    economy_env,
    env,
    interaction_env,
    private_command,
)
from test_economy import proof
from test_tg_interaction_context import VIEWER

from app.modules.economy_rules import day_bounds


def cards(e):
    member = e.members.get('u1')
    return [e.bot._account_card(member), e.bot._account_card(member, public=True),
            e.bot._account_card(member, managing=True)]


@pytest.mark.parametrize('seconds,points,growth', [(299, 0, 0), (300, 8, 5), (600, 16, 10)])
def test_reward_label_only_uses_committed_reward_for_beijing_viewing_date(env, seconds, points, growth):  # noqa: F811 - pytest fixture injection
    e = env
    now = int(time.time())
    service = e.levels.activity
    service.activate(now-600)
    proof(e.db, 'u1', now=now, seconds=seconds, key='reward-display')
    balance = e.services.points.balance('u1')
    for body in cards(e):
        assert '观影已到账' not in body and '今日奖励' not in body and '今日观影奖励' not in body
    assert e.services.points.balance('u1') == balance
    assert not e.db.query('SELECT * FROM activity_watch_days')  # Viewing never settles.
    service.settle(now)
    assert e.services.points.balance('u1') == balance+points
    for body in cards(e):
        if points:
            assert f'🎁 今日观影奖励 +{points} 积分 · +{growth} 成长' in body
        else:
            assert '观影奖励' not in body
    e.bot._edit = AsyncMock()
    member = e.members.get('u1')
    asyncio.run(e.bot._activity_action(VIEWER, 1, member, 'status', actor=str(VIEWER), callback=True))
    text = e.bot._edit.await_args.args[2]
    assert ('观影奖励' in text) == bool(points)
    assert '删除候选' not in text and '保留至' not in text


def test_late_previous_day_credit_increases_balance_but_is_not_todays_reward(env, monkeypatch):  # noqa: F811 - pytest fixture injection
    e = env
    now = int(time.time())
    _, midnight, _ = day_bounds(now)
    yesterday = day_bounds(midnight-1)[0]
    service = e.levels.activity
    service.activate(midnight-600)
    balance = e.services.points.balance('u1')
    proof(e.db, 'u1', now=midnight-1, seconds=300, key='late-previous-day')
    service.settle(now)
    row = e.db.one("SELECT delta,ref,created_at FROM points_ledger WHERE reason='watch.reward'")
    assert row['delta'] == 8 and row['created_at'] == now and yesterday in row['ref']
    assert e.services.points.balance('u1') == balance+8
    assert e.levels.status('u1')['today']['points'] == 0
    assert all('观影奖励' not in body for body in cards(e))
    proof(e.db, 'u1', now=now, seconds=300, key='current-day')
    service.settle(now)
    assert e.services.points.balance('u1') == balance+16
    for body in cards(e):
        assert '🎁 今日观影奖励 +8 积分 · +5 成长' in body
    # Next midnight changes the display bucket, never removes yesterday's money.
    monkeypatch.setattr(time, 'time', lambda: midnight+86400)
    assert all('观影奖励' not in body for body in cards(e))
    assert e.services.points.balance('u1') == balance+16


@pytest.mark.parametrize('offset', [-86400, 0, 86400])
def test_reward_date_label_never_calls_historical_or_stale_data_today(monkeypatch, offset):
    from app.modules.bot_activity import ActivityBotMixin
    now = 1791309600
    monkeypatch.setattr(time, 'time', lambda: now)
    day = day_bounds(now+offset)[0]
    lines = ActivityBotMixin._settled_reward_lines({'day':day, 'today':{'points':8, 'growth':5}})
    label = '今日观影奖励' if offset == 0 else f'{day[5:]} 观影奖励'
    assert lines == [f'🎁 {label} +8 积分 · +5 成长']
    assert ('今日' in lines[0]) == (offset == 0)


@pytest.mark.parametrize('group', ['standard', 'whitelist'])
def test_all_account_surfaces_hide_deletion_candidates_without_changing_deadline(env, group):  # noqa: F811 - pytest fixture injection
    e = env
    e.members.upsert('u1', '', {'group_id':group})
    before = e.levels.status('u1')
    for body in cards(e) + [e.bot._user_card(e.members.get('u1')), e.bot._home(str(VIEWER), 'ViewerA')[0]]:
        assert '删除候选' not in body and '保留至' not in body
    async def run():
        for entry in ('/me', '/myinfo', '/start account', '/activity'):
            mid = await private_command(e, entry)
            body = e.tg.text(VIEWER, mid)
            assert '删除候选' not in body and '保留至' not in body
    asyncio.run(run())
    after = e.levels.status('u1')
    assert before['delete_at'] == after['delete_at']
    assert before['expires_at'] == after['expires_at']
