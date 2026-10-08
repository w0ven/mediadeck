"""Input ownership regression: the new market must not consume existing admin/request digits."""
import asyncio
import copy

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_market_bot import click, cmd, message
from test_market_bot import env as market_env  # noqa: F401
from test_tg_interaction_context import click as menu_click
from test_tg_interaction_context import command as menu_command


@pytest.fixture
def env(request):return request.getfixturevalue('market_env')


@pytest.mark.parametrize('text',['550','903','30','取消'])
@pytest.mark.parametrize('group',[False,True])
def test_unowned_numeric_or_cancel_does_not_intercept_or_validate_legacy_flow(env,text,group):
    inbound=message(env,text,index=1,group=group)
    inbound['from'].pop('is_bot')  # legacy handler fixture is deliberately partial, not a market actor
    before=copy.deepcopy(env.tg.calls)
    assert asyncio.run(env.bot._market_command(inbound)) is False
    assert env.tg.calls==before
    assert not env.db.query('SELECT * FROM market_inputs')


@pytest.mark.parametrize('leaving',['market_button','command','callback','new_market','other_bot','forward','other_actor'])
def test_explicit_navigation_abandons_only_own_market_input_without_financial_effect(env,leaving):
    async def run():
        tg=env.actors[1]['id']
        menu=await menu_command(env,'/me',chat=tg,user=tg)
        row=await cmd(env,'/股票 MD001',index=1)
        row=await click(env,row,'input','buy_MD001',index=1)
        assert env.db.one('SELECT * FROM market_inputs WHERE tg_id=?',(str(tg),))
        before=env.db.query('SELECT * FROM points_ledger ORDER BY id')
        if leaving=='market_button':await click(env,row,'view','search',index=1)
        elif leaving=='callback':await menu_click(env,'home',menu,chat=tg,user=tg)
        elif leaving=='new_market':await cmd(env,'/股票 MD002',index=1)
        else:
            inbound=message(env,'/me@AnotherBot' if leaving=='other_bot' else '/me',index=2 if leaving=='other_actor' else 1)
            if leaving=='forward':inbound['forward_origin']={'type':'user'}
            await env.bot._dispatch_update({'message':inbound})
        pending=env.db.one('SELECT * FROM market_inputs WHERE tg_id=?',(str(tg),))
        if leaving in ('other_bot','forward','other_actor'):assert pending
        else:
            assert pending is None
            assert await env.bot._market_command(message(env,'30',index=1)) is False
        assert env.db.query('SELECT * FROM points_ledger ORDER BY id')==before
    asyncio.run(run())
