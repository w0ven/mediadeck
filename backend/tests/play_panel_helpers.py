"""Original game/help and points-board panel helpers, isolated Telegram only."""
import copy

from test_tg_interaction_context import GROUP


def message(e,value,index=0,group=False,mid=None,reply=None):
    if mid is None:e.mid+=1;mid=e.mid
    actor=e.actors[index]
    result={'from':actor,'chat':{'id':GROUP if group else actor['id'],'type':'supergroup' if group else 'private'},'message_id':mid,'text':value}
    if group:result.update(message_thread_id=27,is_topic_message=True)
    if reply:result['reply_to_message']={'message_id':reply,'from':{'id':123,'is_bot':True}}
    return result


def panel(e,index=0,group=False):
    return e.db.one('SELECT * FROM play_panels WHERE tg_id=? AND chat_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(str(e.actors[index]['id']),str(GROUP if group else e.actors[index]['id'])))


async def cmd(e,value,index=0,group=False,**kw):
    await e.bot._dispatch_update({'message':message(e,value,index,group,**kw)})
    return panel(e,index,group)


def card(e,row):
    m=copy.deepcopy(e.tg.message(row['chat_id'],row['message_id']))
    m.update(chat={'id':int(row['chat_id']),'type':'private' if row['chat_id']==row['tg_id'] else 'supergroup'},message_id=row['message_id'],**{'from':{'id':123,'is_bot':True}})
    if row['thread_id']:m['is_topic_message']=True
    return m


def body(e,row):return e.tg.text(row['chat_id'],row['message_id'])


async def click(e,row,op,arg=None,index=None,original=None):
    if index is None:index=next(i for i,a in enumerate(e.actors) if str(a['id'])==row['tg_id'])
    data=f'pp:{row["nonce"]}:{op}:'+str(arg if arg is not None else '')
    await e.bot._dispatch_update({'callback_query':{'id':'play-panel-test','data':data,'from':e.actors[index],'message':original or card(e,row)}})
    return e.bot._play_panel(row['nonce'])
