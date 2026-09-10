"""Real Bot handlers with a fully local Telegram transport; no Telegram/TMDB sends."""
from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.requests import ACCEPT_NOTICE, RequestService
from app.modules.telegram import TelegramBot


class FakeTmdb:
    async def lookup(self, media_type, tmdb_id):
        return {'title':'搏击俱乐部' if media_type=='movie' else '测试剧集', 'year':1999,
                'poster_path':'/poster.jpg', 'overview':'本地模拟简介', 'original_title':'Fight Club',
                'seasons':[{'season_number':1,'episode_count':12}] if media_type=='tv' else []}

    async def search(self, query, page=1, media_type='', year=None):
        return {'available':True,'page':page,'total_pages':2,
                'results':[dict(await self.lookup(media_type or 'movie',550+page), tmdb_id=550+page,
                                media_type=media_type or 'movie',title=f'{query} 候选 {page}',year=year or 1999)]}


class FakeLibrary:
    async def request_lookup(self, kind, tmdb_id, user_id=None):
        return [{'id':'local-film','name':'已有内容','url':'https://emby.invalid/web/#!/item?id=local-film',
                 'episodes':[{'season':1,'episode':1}]}]


class Transport:
    def __init__(self):
        self.calls=[]; self.messages={}; self.mid=100; self.fail=set()

    async def call(self, method, payload=None, timeout=None):
        p=dict(payload or {}); self.calls.append((method,copy.deepcopy(p)))
        if method in self.fail: return None
        if method in ('sendMessage','sendPhoto'):
            self.mid+=1; p['message_id']=self.mid
            self.messages[(str(p['chat_id']),self.mid)]=p
            return {'message_id':self.mid}
        if method.startswith('editMessage'):
            key=(str(p['chat_id']),int(p['message_id']))
            row=self.messages.setdefault(key,{'message_id':p['message_id'],'chat_id':p['chat_id']})
            if method=='editMessageMedia':
                row.update(photo=p['media']['media'],caption=p['media'].get('caption',''))
                row.pop('text',None)
            else:
                row.update(p)
                if method=='editMessageText': row.pop('caption',None)
            row['reply_markup']=p.get('reply_markup',{'inline_keyboard':[]})
            return {'message_id':p['message_id']}
        return True


@pytest.fixture()
def bot(tmp_path):
    db=Database(tmp_path/'bot.db');groups=GroupService(db);groups.seed_defaults();members=MemberService(db,groups)
    for uid,tg,roles in [('u1','900',[]),('u2','901',[]),('up1','801',['uploader']),('up2','802',['uploader']),('admin1','800',['admin'])]:
        members.upsert(uid,uid,{'group_id':'standard'},actor='test');members.bind_telegram(uid,tg,actor='test');members.set_roles(uid,roles,actor='test')
    tmdb=FakeTmdb();service=RequestService(db,members,groups,tmdb,FakeLibrary())
    transport=Transport()
    cfg={'enabled':True,'bot_token':'1234567'+':local-test-only','require_group':'','group_interaction_chats':'-10055'}
    def build():
        b=TelegramBot(lambda:cfg,members,db=db,requests=service,groups=groups,tmdb=tmdb)
        b._call=transport.call;b.transport=transport;b.service=service;b.members=members;b.db=db;b.reboot=build;b.cfg=cfg
        return b
    yield build()
    db.close()


def run(awaitable): return asyncio.run(awaitable)


def message(bot,text,chat='900',reply=None):
    p={'chat':{'id':chat,'type':'private'},'from':{'id':chat,'first_name':'测试用户'},'text':text,'message_id':999}
    if reply: p['reply_to_message']={'message_id':reply}
    run(bot._handle_message(p))


def cards(bot,chat='900'):
    return bot.db.query('SELECT * FROM request_cards WHERE chat_id=? ORDER BY message_id DESC',(str(chat),))


def card_with(bot,action,chat='900'):
    return next(c for c in cards(bot,chat) if action in json.loads(c['actions']))


def tap(bot,action,chat='900',card=None,actor=None):
    card=card or card_with(bot,action,chat)
    data=f"rq:{card['token']}:{action}"
    run(bot._handle_callback({'id':'cb-'+str(time.time_ns()),'data':data,'from':{'id':actor or chat,'first_name':'测试'},
         'message':{'chat':{'id':chat,'type':'private'},'message_id':card['message_id'], **({'photo':[{}]} if card['photo'] else {})}}))
    return card


def body(bot,chat='900'):
    card=cards(bot,chat)[0];m=bot.transport.messages[(chat,card['message_id'])]
    return m.get('text') or m.get('caption') or ''


def submit(bot,chat='900',kind='movie',tmdb=550):
    message(bot,'/requests',chat);tap(bot,'new',chat);message(bot,f'https://www.themoviedb.org/{kind}/{tmdb}',chat)
    if any('submit' in json.loads(c['actions']) for c in cards(bot,chat)): tap(bot,'submit',chat)
    else: tap(bot,'follow',chat)
    return bot.service.list()[0]


def test_movie_search_select_confirm_has_no_requirements_step(bot):
    message(bot,'/requests');tap(bot,'new');message(bot,'Fight Club')
    assert any(m=='editMessageMedia' and p['media']['media'].endswith('/poster.jpg') for m,p in bot.transport.calls)
    original=cards(bot)[0]['message_id']
    tap(bot,'candidate:1');assert cards(bot)[0]['message_id']==original
    tap(bot,'searchtype:movie');tap(bot,'year');message(bot,'1999');tap(bot,'pick')
    assert cards(bot)[0]['message_id']==original
    assert bot.service.used('u1')==0
    text=body(bot)
    for expected in ('搏击俱乐部','1999','电影','扣 1 次','媒体库已有'): assert expected in text
    for removed in ('可选要求','清晰度','字幕','配音','备注','重选需求'): assert removed not in text
    assert set(json.loads(cards(bot)[0]['actions']))=={'submit','new','home'}
    old=tap(bot,'submit');tap(bot,'submit',card=old)
    row=bot.service.list()[0]
    assert row['status']=='open' and bot.service.used('u1')==1
    assert row['demand']['scope']=='movie' and row['demand']['version']==''
    assert all(card_with(bot,'accept',chat) for chat in ('801','802'))


def test_bare_id_requires_type_tv_defaults_to_all_seasons_then_submit(bot):
    message(bot,'/requests');tap(bot,'new');message(bot,'1396')
    assert '明确选择类型' in body(bot) and not bot.service.list()
    original=cards(bot)[0]['message_id'];tap(bot,'type:tv')
    assert '全部季' in body(bot)
    assert cards(bot)[0]['message_id']==original
    buttons=bot.transport.messages[('900',original)]['reply_markup']['inline_keyboard']
    assert any(b['text']=='☑ 全部季' for row in buttons for b in row)
    tap(bot,'submit')
    row=bot.service.list()[0]
    assert row['media_type']=='tv' and row['demand']['scope']=='series'
    assert row['demand']['seasons']==[] and row['demand']['episodes']==[]


def test_tv_season_text_updates_same_confirmation_card_with_retry_and_return(bot):
    message(bot,'/requests');tap(bot,'new');message(bot,'https://www.themoviedb.org/tv/1396')
    original=cards(bot)[0]['message_id'];old=card_with(bot,'submit')
    tap(bot,'selectseasons')
    assert 'submit' not in json.loads(cards(bot)[0]['actions'])
    tap(bot,'submit',card=old)
    assert not bot.service.list()
    # An invalid old button must not borrow the active card or its input.
    message(bot,'1,3,5-7',reply=original)
    assert cards(bot)[0]['message_id']==original and '第1、3、5-7季' in body(bot)
    tap(bot,'selectseasons');message(bot,'-2')
    assert cards(bot)[0]['message_id']==original and '⚠' in body(bot)
    assert bot.db.one("SELECT * FROM request_inputs WHERE chat_id='900'")
    message(bot,'1,3,7-5')
    assert '⚠' in body(bot) and not bot.service.list()
    message(bot,'7,5-7,3,1')
    assert '第1、3、5-7季' in body(bot) and 'submit' in json.loads(cards(bot)[0]['actions'])
    tap(bot,'selectseasons');tap(bot,'preview')
    assert '第1、3、5-7季' in body(bot)
    tap(bot,'allseasons');assert '全部季' in body(bot)
    tap(bot,'selectseasons');bot=bot.reboot();message(bot,'1,3,5-7')
    assert cards(bot)[0]['message_id']==original and bot.service.used('u1')==0
    bot=bot.reboot();tap(bot,'submit')
    row=bot.service.list()[0]
    assert row['demand']['scope']=='season' and row['demand']['seasons']==[1,3,5,6,7]
    assert row['demand']['episodes']==[] and bot.service.used('u1')==1
    caption=[p['caption'] for m,p in bot.transport.calls if m=='sendPhoto' and str(p['chat_id'])=='801'][-1]
    assert '第1、3、5-7季' in caption


def test_uploader_is_independent_accept_final_all_cards_retired_followers_once(bot):
    row=submit(bot);submit(bot,'901')
    assert bot.service.used('u2')==0
    old=card_with(bot,'accept','802')
    message(bot,'/uploader','801');tap(bot,f"view:{row['id']}",'801');tap(bot,'accept','801')
    assert bot.service.get(row['id'])['status']=='accepted'
    tap(bot,'accept','802',card=old)
    assert bot.service.get(row['id'])['claimed_by']=='up1'
    for chat in ('900','901'):
        sends=[p for m,p in bot.transport.calls if m=='sendMessage' and str(p['chat_id'])==chat and ACCEPT_NOTICE in p.get('text','')]
        assert len(sends)==1
    assert not any('accept' in json.loads(c['actions']) or 'reject' in json.loads(c['actions']) for c in cards(bot,'802'))
    assert bot.service.used('u1')==1


@pytest.mark.parametrize('reason',['','暂无片源'])
def test_rejection_reason_optional_no_fabricated_reason(bot,reason):
    row=submit(bot);tap(bot,'reject','801')
    assert set(json.loads(cards(bot,'801')[0]['actions']))=={'rejectok','reason:custom','view:1'}
    if reason:
        tap(bot,'reason:custom','801');message(bot,reason,'801')
    else:
        tap(bot,'rejectok','801')
    assert bot.service.get(row['id'])['status']=='rejected'
    assert bot.service.get(row['id'])['result_note']==reason
    notice=[p['text'] for m,p in bot.transport.calls if m=='sendMessage' and str(p['chat_id'])=='900' and '已拒绝' in p.get('text','')][-1]
    assert ('理由：' in notice)==bool(reason)
    assert bot.service.used('u1')==1


def test_question_reply_internal_edit_cancel_and_message_ownership(bot):
    row=submit(bot);tap(bot,'ask','801');message(bot,'请说明版本','801')
    assert bot.service.get(row['id'])['status']=='open'
    assert any('请说明版本' in p.get('text','') and str(p.get('chat_id'))=='900' for m,p in bot.transport.calls if m=='sendMessage')
    message(bot,'/requests');tap(bot,'list:mine');tap(bot,'view:1');tap(bot,'reply')
    message(bot,'跨卡消息不该转达',reply=999999)
    assert not any(e['body']=='跨卡消息不该转达' for e in bot.service.events(1))
    waiting=bot.db.one("SELECT * FROM request_inputs WHERE chat_id='900'")
    assert waiting
    message(bot,'原版即可')
    assert any(e['body']=='原版即可' for e in bot.service.events(1))
    message(bot,'/uploader','801');tap(bot,'view:1','801');tap(bot,'internal','801');message(bot,'内部片源位置','801')
    assert '内部片源位置' not in json.dumps(bot.service.events(1),ensure_ascii=False)
    assert not any('内部片源位置' in p.get('text','') and str(p.get('chat_id'))=='900' for m,p in bot.transport.calls)
    message(bot,'/requests');tap(bot,'list:mine');tap(bot,'view:1');tap(bot,'reply');message(bot,'补充备注')
    assert any(e['body']=='补充备注' for e in bot.service.events(1))
    tap(bot,'cancel');tap(bot,'cancelok');assert bot.service.get(1)['status']=='cancelled'


def test_restart_recovers_lists_bound_input_and_draft_without_recharging(bot):
    row=submit(bot)
    bot=bot.reboot();message(bot,'/uploader','801');tap(bot,'view:1','801');tap(bot,'ask','801')
    bot=bot.reboot();message(bot,'重启后继续询问','801')
    assert bot.service.get(row['id'])['status']=='open'
    assert any(e['body']=='重启后继续询问' for e in bot.service.events(1))
    message(bot,'/requests');tap(bot,'new');message(bot,'552');tap(bot,'type:movie')
    bot=bot.reboot();tap(bot,'submit')
    assert bot.service.used('u1')==2 and len(bot.service.list())==2


def test_notification_failure_retry_only_sends_failed_jobs(bot):
    row=submit(bot);bot.transport.fail.add('sendMessage')
    tap(bot,'accept','801')
    assert bot.service.get(row['id'])['status']=='accepted'
    pending=bot.db.query("SELECT * FROM request_outbox WHERE kind='result' AND state='pending'")
    assert len(pending)==1
    bot=bot.reboot();bot.transport.fail.clear();bot.service.retry_notifications(1);run(bot.flush_request_notifications());run(bot.flush_request_notifications())
    sent=[p for m,p in bot.transport.calls if m=='sendMessage' and str(p.get('chat_id'))=='900' and ACCEPT_NOTICE in p.get('text','')]
    # One failed attempt and exactly one acknowledged retry.
    assert len(sent)==2 and bot.service.used('u1')==1
    assert len(bot.db.query("SELECT * FROM request_events WHERE kind='accepted'"))==1


def test_expired_cross_card_cross_actor_and_demoted_permissions(bot):
    submit(bot);card=card_with(bot,'accept','801')
    tap(bot,'accept','801',card=card,actor='802')
    assert bot.service.get(1)['status']=='open'
    bad=dict(card,message_id=card['message_id']+9999);tap(bot,'accept','801',card=bad)
    assert bot.service.get(1)['status']=='open'
    bot.members.set_roles('up1',[],actor='test');tap(bot,'accept','801',card=card)
    assert bot.service.get(1)['status']=='open'
    card=card_with(bot,'accept','802');bot.db.execute('UPDATE request_cards SET expires_at=0 WHERE token=?',(card['token'],))
    before=len([c for c in bot.transport.calls if c[0]=='answerCallbackQuery'])
    tap(bot,'accept','802',card=card)
    assert len([c for c in bot.transport.calls if c[0]=='answerCallbackQuery'])==before+1
    assert bot.service.get(1)['status']=='open'


def test_outbound_notices_do_not_cancel_other_draft_and_home_cancels_input(bot):
    message(bot,'/requests','801');tap(bot,'new','801')
    waiting=bot.db.one("SELECT * FROM request_inputs WHERE chat_id='801'")
    submit(bot)
    assert bot.db.one("SELECT * FROM request_inputs WHERE chat_id='801'")['token']==waiting['token']
    message(bot,'/cancel','801')
    assert not bot.db.one("SELECT * FROM request_inputs WHERE chat_id='801'")


def test_private_group_deep_link_and_no_request_content_to_group(bot):
    bot._bot_username='local_test_bot'
    run(bot._handle_message({'chat':{'id':-10055,'type':'supergroup'},'from':{'id':900},'text':'/requests'}))
    posts=[p for m,p in bot.transport.calls if m=='sendMessage' and str(p['chat_id'])=='-10055']
    assert posts and '私聊' in posts[-1]['text']
    assert 'start=requests' in posts[-1]['reply_markup']['inline_keyboard'][0][0]['url']
    assert not bot.service.list()


def test_uploader_notifications_are_recognizable_poster_cards_and_fallback(bot):
    row=submit(bot,kind='tv')
    photos=[p for method,p in bot.transport.calls if method=='sendPhoto' and str(p['chat_id'])=='801']
    assert len(photos)==1
    assert photos[0]['photo'].endswith('/poster.jpg')
    for text in ('测试剧集','1999','剧集','Fight Club','全部季','本地模拟简介'):
        assert text in photos[0]['caption']
    assert 'https://image.tmdb.org' not in photos[0]['caption']
    actions={b.get('callback_data','').rsplit(':',1)[-1] for r in photos[0]['reply_markup']['inline_keyboard'] for b in r}
    assert {'accept','reject','ask','full'} <= actions
    assert any(b.get('url','').startswith('https://www.themoviedb.org/tv/') for r in photos[0]['reply_markup']['inline_keyboard'] for b in r)
    message(bot,'/uploader','801')
    assert all(text in body(bot,'801') for text in ('测试剧集','1999','剧集','全部季'))
    # Media failure falls back to a usable text card, with all business actions.
    bot.transport.fail.update({'sendPhoto','editMessageMedia'})
    message(bot,'/uploader','802');tap(bot,'view:1','802')
    assert '测试剧集' in body(bot,'802') and card_with(bot,'accept','802')
    assert bot.service.get(row['id'])['original_title']=='Fight Club'


def test_cancelled_draft_capability_stays_invalid_after_restart(bot):
    message(bot,'/requests');tap(bot,'new');message(bot,'550');tap(bot,'type:movie')
    old=card_with(bot,'submit');message(bot,'/cancel');bot=bot.reboot();tap(bot,'submit',card=old)
    assert not bot.service.list() and bot.service.used('u1')==0


def test_metadata_failure_keeps_type_id_and_explicit_unknown_title(bot):
    bot._tmdb=None;bot.service._tmdb=None
    row=submit(bot,tmdb=999)
    assert row['tmdb_id']==999
    texts=[p.get('text','') for method,p in bot.transport.calls if method=='sendMessage' and str(p['chat_id'])=='801']
    assert texts and '暂未获取片名' in texts[-1] and '电影' in texts[-1] and '999' in texts[-1]


def test_existing_season_request_can_follow_and_accepted_history_stays_visible(bot):
    row=run(bot.service.create('u1','tv',1396,demand={'scope':'season','seasons':[1,3,5,6,7]}))
    message(bot,'/requests','901');tap(bot,'new','901');message(bot,'https://www.themoviedb.org/tv/1396','901')
    tap(bot,'selectseasons','901');message(bot,'1,3,5-7','901')
    assert 'follow' in json.loads(cards(bot,'901')[0]['actions'])
    assert 'submit' not in json.loads(cards(bot,'901')[0]['actions'])
    tap(bot,'follow','901');assert bot.service.used('u2')==0
    assert bot.service.list(user_id='u2',followed=True)[0]['id']==row['id']
    bot.service.finish(row['id'],'up1','accepted')
    message(bot,'/requests','901');tap(bot,'new','901');message(bot,'https://www.themoviedb.org/tv/1396','901')
    assert '已有接受历史' in body(bot,'901') and '等待下载完成并入库' in body(bot,'901')
    tap(bot,'selectseasons','901');message(bot,'1,3,5-7','901')
    assert '已接受' in body(bot,'901')
    assert 'submit' not in json.loads(cards(bot,'901')[0]['actions'])


def test_historical_requirements_survive_details_and_old_edit_buttons(bot):
    demand={'scope':'episodes','seasons':[2],'episodes':[1,3],'version':'旧版本说明',
            'quality':'4K','subtitle':'简中','audio':'国语'}
    row=run(bot.service.create('u1','tv',1396,note='旧备注不能丢',demand=demand))
    before=bot.service.get(row['id'])
    message(bot,'/requests');tap(bot,'list:mine');tap(bot,'view:1');tap(bot,'full')
    for value in ('季：2','集：1,3','旧版本说明','4K','简中','国语','旧备注不能丢'):
        assert value in body(bot)
    member=bot.members.get('u1');mid=cards(bot)[0]['message_id']
    # Simulate a persisted pre-upgrade editor, including a hidden attempted edit.
    p={'draft':True,'editing':row['id'],'revision':row['revision'],'media_type':'tv',
       'tmdb_id':1396,'demand':dict(demand,quality='1080p'),'note':'未确认修改'}
    run(bot._rq_render('900',mid,member,'旧编辑器',[[('确认修改','save')]],p,rid=row['id'],revision=1))
    bot=bot.reboot();tap(bot,'save')
    assert bot.service.get(row['id'])==before
    assert '原工单要求未更改' in body(bot) and '旧备注不能丢' in body(bot)
    assert 'modify' not in json.loads(cards(bot)[0]['actions'])
    assert 'save' not in json.loads(cards(bot)[0]['actions'])


def test_old_unsubmitted_draft_requires_new_confirmation_not_silent_submit(bot):
    message(bot,'/requests');tap(bot,'new');message(bot,'https://www.themoviedb.org/movie/550')
    card=cards(bot)[0];p=json.loads(card['payload']);p.pop('simple')
    p['demand'].update(scope='version',version='旧草稿版本',quality='4K')
    bot.db.execute('UPDATE request_cards SET payload=? WHERE token=?',(json.dumps(p),card['token']))
    bot=bot.reboot();tap(bot,'submit',card=card)
    assert not bot.service.list() and bot.service.used('u1')==0
    assert '本次尚未提交' in body(bot)
    assert 'scope:movie' not in json.loads(cards(bot)[0]['actions'])
    tap(bot,'submit');assert bot.service.used('u1')==1
    assert bot.service.get(1)['demand']['scope']=='movie'


def test_rejection_input_retains_role_revision_and_single_execution_guards(bot):
    submit(bot);tap(bot,'reject','801');tap(bot,'reason:custom','801')
    assert '发送后立即拒绝' in body(bot,'801')
    old=cards(bot,'801')[0]
    bot.members.set_roles('up1',[],actor='test');message(bot,'不应拒绝','801')
    assert bot.service.get(1)['status']=='open'
    bot.members.set_roles('up1',['uploader'],actor='test')
    tap(bot,'accept','802');message(bot,'迟到的拒绝理由','801')
    assert bot.service.get(1)['status']=='accepted'
    assert bot.service.get(1)['result_note']==''
    assert len([e for e in bot.service.events(1) if e['kind'] in ('accepted','rejected')])==1
    tap(bot,'rejectok','801',card=old)
    assert bot.service.get(1)['status']=='accepted'
