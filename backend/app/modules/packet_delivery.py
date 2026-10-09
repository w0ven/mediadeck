"""Only envelope pin, exact-message unpin and one independent public receipt."""
from __future__ import annotations

import time
from html import escape

from app.modules.economy_rules import economy_write, encode
from app.modules.red_packets import RESULTS_PER_PAGE, PacketError

EFFECTS=('pin','unpin','receipt')


def receipt_view(row,claims,page=0):
    pages=max(1,(len(claims)+RESULTS_PER_PAGE-1)//RESULTS_PER_PAGE)
    page=max(0,min(page,pages-1))
    body=f'🌸 <b>红包圆满收官</b>\n{row["total"]} 积分 · {row["parts"]} 份都找到主人啦'
    for c in claims[page*RESULTS_PER_PAGE:(page+1)*RESULTS_PER_PAGE]:
        body+='\n'+escape(str(c['display_name'] or '成员')[:40])+f'  ·  <b>{c["amount"]}</b> 积分'
    if row['mode']=='random' and claims:
        best=min(claims,key=lambda c:(-c['amount'],c['slot']))
        body+='\n\n✨ 手气最佳 '+escape(best['display_name'][:40])+f' · {best["amount"]} 积分'
    body+='\n<i>愿好手气与你相伴。</i>'
    keys=[]
    if pages>1:
        nav=[]
        if page:nav.append({'text':'‹ 上一页','callback_data':f'rpresult:{row["nonce"]}:{page-1}'})
        nav.append({'text':f'{page+1}/{pages}','callback_data':f'rpresult:{row["nonce"]}:{page}'})
        if page+1<pages:nav.append({'text':'下一页 ›','callback_data':f'rpresult:{row["nonce"]}:{page+1}'})
        keys=[nav]
    return {'text':body,'parse_mode':'HTML','reply_markup':{'inline_keyboard':keys}}


class PacketDelivery:
    def __init__(self,service):self.service,self.db=service,service.db

    def claim(self,nonce,kind,*,now=None):
        if kind not in EFFECTS:raise ValueError('unknown envelope effect')
        clock=time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row=self.service.get(nonce)
            if not row or not row['permanent'] or not row['card_message_id']:return None
            if row[kind+'_state']=='sending' and row[kind+'_lease']<=clock:
                conn.execute(f"UPDATE red_packets SET {kind}_state='unknown',{kind}_error='发送确认中断；不自动重复' WHERE nonce=?",(nonce,))
                return None
            if row[kind+'_state'] not in ('pending','retry') or row[kind+'_due']>clock:return None
            required='active' if kind=='pin' else 'exhausted'
            if row['status']!=required or row['rendered_version']<row['render_version']:return None
            if kind=='unpin' and row['pin_state']=='sending' and row['pin_lease']>clock:return None
            if kind=='receipt' and not row['receipt_payload']:
                payload=receipt_view(row,self.service.claims(nonce,row['claimed_count']))
                conn.execute('UPDATE red_packets SET receipt_payload=? WHERE nonce=?',(encode(payload),nonce))
                row=dict(row,receipt_payload=encode(payload))
            lease=clock+90
            conn.execute(f"UPDATE red_packets SET {kind}_state='sending',{kind}_lease=?,{kind}_attempts={kind}_attempts+1 WHERE nonce=?",(lease,nonce))
            return dict(row,_effect=kind,_lease=lease,_attempts=row[kind+'_attempts']+1)

    def finish(self,row,response,failure,*,now=None):
        kind=row['_effect'];clock=time.time() if now is None else float(now)
        mid=response.get('message_id') if isinstance(response,dict) else None
        success=(type(mid) is int and mid>0) if kind=='receipt' else response is True
        state='sent' if success else 'retry' if failure.get('state')=='retry' and row['_attempts']<3 else 'failed' if failure.get('state') in ('failed','retry') else 'unknown'
        error='' if success else ('置顶未确认，仍可正常领取' if kind=='pin' else '仅本红包解除置顶未确认' if kind=='unpin' else '结果消息未确认；未知不自动重发')
        extra=',receipt_message_id=?' if kind=='receipt' else ''
        args=[state,error,clock+max(5,int(failure.get('retry_after') or 10))]
        if kind=='receipt':args.append(mid if success else None)
        args.extend((row['nonce'],row['_lease']))
        self.db.execute(f"UPDATE red_packets SET {kind}_state=?,{kind}_error=?,{kind}_due=?{extra} WHERE nonce=? AND {kind}_state='sending' AND {kind}_lease=?",tuple(args))
        if kind=='pin' and success:
            # A pin ACK arriving after a last claim must still trigger exact-message unpin.
            self.db.execute("UPDATE red_packets SET unpin_state='pending',unpin_due=0,unpin_attempts=0 WHERE nonce=? AND status='exhausted' AND unpin_state<>'sending'",(row['nonce'],))

    def page(self,nonce,actor,message,page):
        with economy_write(self.db) as conn:
            row=self.service.get(nonce)
            if not row or row['receipt_state']!='sent' or not row['receipt_message_id']:raise PacketError('请使用原红包结果卡')
            self.service._actor(actor)
            # Reuse all original Bot/group/topic checks against the independent receipt ID.
            self.service._context(dict(row,card_message_id=row['receipt_message_id']),message)
            buttons=[b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard',[]) for b in line]
            if f'rpresult:{nonce}:{page}' not in buttons:raise PacketError('请使用结果卡上的按钮')
            claims=self.service.claims(nonce,row['claimed_count'])
            if type(page) is not int or not 0<=page<max(1,(len(claims)+RESULTS_PER_PAGE-1)//RESULTS_PER_PAGE):raise PacketError('结果页码无效')
            conn.execute('UPDATE red_packets SET receipt_page=? WHERE nonce=?',(page,nonce))
            return row,receipt_view(row,claims,page)
