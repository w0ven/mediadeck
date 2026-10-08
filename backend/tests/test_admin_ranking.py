"""Actual received-update ranking and discoverable gameplay, isolated existing ledger."""
import asyncio
import copy
import json
import os
import time
from pathlib import Path

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_market_bot import body, card, click, cmd, message, panel
from test_tg_interaction_context import VIEWER
from test_tg_interaction_context import click as menu_click
from test_tg_interaction_context import command as menu_command

from app.modules.economy_rules import economy_write
from app.modules.play_money import CashBook
from app.modules.points_ranking import PointsRankingService


@pytest.fixture
def env(request):
    e=request.getfixturevalue('bw_env')
    for name in ('points_ranking','poker','checkin_cleanup'):
        e.services.registry.save(name,enabled=True)
        e.services.registry.get(name).ctx.telegram=e.bot
    e.mid=1800
    return e


def ranking(e):return PointsRankingService(e.members,e.services.points).rows()


def test_actual_ranking_spendable_only_names_neutral_and_pagination_independent_market_switch(env):
    for i,uid in enumerate(env.uids):
        env.db.execute('UPDATE members SET tg_display_name=?,tg_username=? WHERE emby_user_id=?',
                       ('公开<名&>'+str(i) if i else '', 'never-public-'+str(i), uid))
    with economy_write(env.db) as conn:CashBook(env.services.points).reserve(conn,'poker','ranking-fixture',env.uids[1],950)
    before=copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
    assert not env.services.registry.enabled('stock_market')
    rows=ranking(env)
    assert next(r for r in rows if r['_uid']==env.uids[1])['points']==50
    assert next(r for r in rows if r['_uid']==env.uids[0])['name']=='成员'
    assert all(rows[i]['points']>=rows[i+1]['points'] for i in range(len(rows)-1))
    env.actors[0]['first_name']='公开管理名'
    env.services.registry.save('points_ranking',config={'page_size':5})
    async def run():
        row=await cmd(env,'/积分榜',group=True)
        assert '1/2页' in body(env,row) and '当前可用' in body(env,row)
        assert '&lt;名&amp;&gt;' in body(env,row)
        one=body(env,row)
        await click(env,row,'page',1)
        two=body(env,row)
        assert '2/2页' in two and '50</b>' in two
        for uid in env.uids:assert uid not in one+two
        assert 'private-login' not in one+two and 'never-public' not in one+two and '市值' not in one+two
        assert env.db.query('SELECT * FROM points_ledger ORDER BY id')==before
        target=os.getenv('GAMES_MARKET_ARTIFACTS')
        if target:Path(target,'ranking-effect.json').write_text(json.dumps({'page1':one,'page2':two},ensure_ascii=False,indent=2))
    asyncio.run(run())


@pytest.mark.parametrize('invalid',['unbound','disabled','expired','missing','malformed_tg'])
def test_ranking_filters_effective_bindings_not_username(env,invalid):
    uid=env.uids[1]
    if invalid=='unbound':env.members.unbind_telegram(uid)
    elif invalid=='disabled':env.members.set_status(uid,'suspended')
    elif invalid=='expired':env.members.set_overrides(uid,{'expires_at_override':time.time()-1})
    elif invalid=='missing':env.db.execute('UPDATE members SET emby_missing_since=1 WHERE emby_user_id=?',(uid,))
    else:env.db.execute("UPDATE members SET tg_user_id='not-an-id' WHERE emby_user_id=?",(uid,))
    assert uid not in [r['_uid'] for r in ranking(env)]


@pytest.mark.parametrize('wrong',['message','topic','chat','bot','actor_bot','forward'])
def test_ranking_callback_original_context_guard(env,wrong):
    env.services.registry.save('points_ranking',config={'page_size':5})
    async def run():
        row=await cmd(env,'/积分榜',group=True)
        old=card(env,row)
        if wrong=='message':old['message_id']+=1
        elif wrong=='topic':old['message_thread_id']=77
        elif wrong=='chat':old['chat']['id']-=1
        elif wrong=='bot':old['from']['id']=345
        elif wrong=='actor_bot':env.actors[0]['is_bot']=True
        else:old['forward_origin']={'type':'user'}
        before=env.bot._market_panel(row['nonce'])['payload_json']
        await click(env,row,'page',1,original=old)
        assert env.bot._market_panel(row['nonce'])['payload_json']==before
    asyncio.run(run())


def test_hub_buttons_launch_both_games_idempotently_and_help_is_separate(env):
    async def run():
        row=await cmd(env,'/games',group=True)
        hub=body(env,row)
        assert len(hub)<300 and '不比花色' not in hub and '管理员' not in hub
        before=copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
        await click(env,row,'launch','bw')
        await click(env,row,'launch','bw')
        assert env.db.one("SELECT COUNT(*) n FROM play_rounds WHERE kind='blackwhite'")['n']==1
        await click(env,row,'launch','poker')
        await click(env,row,'launch','poker')
        assert env.db.one("SELECT COUNT(*) n FROM play_rounds WHERE kind='poker'")['n']==1
        assert env.db.query('SELECT * FROM points_ledger ORDER BY id')==before  # neither lobby freezes on creation
        await click(env,row,'gamehelp','poker')
        help_body=body(env,row)
        for rule in ('A23','QKA','235','花色','60','500'):assert rule in help_body
        await click(env,row,'view','playhub')
        await click(env,row,'gamehelp','bw')
        assert '同面全退' in body(env,row) and '秘密' in body(env,row)
        assert len(body(env,row))<4096
        private=await cmd(env,'/游戏')
        keys=[b['callback_data'] for line in card(env,private)['reply_markup']['inline_keyboard'] for b in line if 'callback_data' in b]
        assert not any(':launch:' in key for key in keys)
        target=os.getenv('GAMES_MARKET_ARTIFACTS')
        if target:Path(target,'hub-effect.json').write_text(json.dumps({'hub':hub,'poker_help':help_body},ensure_ascii=False,indent=2))
    asyncio.run(run())


@pytest.mark.parametrize('verb',['/积分榜','/游戏'])
def test_discovery_forwarded_private_wrong_bot_suffix_and_invalid_id_ignored(env,verb):
    async def run():
        for mutate in ('forward','id','chat','suffix'):
            msg=message(env,verb)
            if mutate=='forward':msg['forward_origin']={'type':'user'}
            elif mutate=='id':msg['message_id']=True
            elif mutate=='chat':msg['chat']['id']+=10
            else:msg['text']=verb+'@anotherbot'
            await env.bot._dispatch_update({'message':msg})
            assert panel(env) is None
    asyncio.run(run())


def test_menu_new_buttons_real_callback_and_plugin_switches(env):
    async def run():
        mid=await menu_command(env,'/me',chat=VIEWER,user=VIEWER)
        for action,view in (('play_hub','playhub'),('points_board','leaderboard')):
            await menu_click(env,action,mid,chat=VIEWER,user=VIEWER)
            last=env.db.one('SELECT * FROM market_panels ORDER BY created_at DESC LIMIT 1')
            assert json.loads(last['payload_json'])['view']==view and last['message_id']
        keys=[b['callback_data'] for line in env.bot.member_menu() for b in line]
        assert 'points_board' in keys and 'play_hub' in keys and 'market_home' not in keys
        for pid in ('blackwhite','poker','points_ranking'):env.services.registry.save(pid,enabled=False)
        keys=[b['callback_data'] for line in env.bot.member_menu() for b in line]
        assert 'points_board' not in keys and 'play_hub' not in keys
        for group in (False,True):
            commands={r['command'] for r in env.bot._command_list(group=group)}
            assert {'games','stock','pointsrank'}<=commands
            if group:assert {'blackwhite','poker'}<=commands
    asyncio.run(run())
