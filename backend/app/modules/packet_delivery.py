"""Only envelope pin, exact-message unpin and one independent public receipt."""
from __future__ import annotations

import json
import time

from app.modules.economy_rules import economy_write, encode
from app.modules.game_mentions import result_mention, text_units
from app.modules.red_packets import RESULTS_PER_PAGE, PacketError, public_name

EFFECTS=('pin','unpin','receipt')


def receipt_mention(claim):
    # Captured Telegram nickname only; do not prefer handles or account logins.
    name = public_name({'first_name': claim.get('display_name')})
    name = name[:39]+'…' if len(name) > 40 else name
    return result_mention(claim.get('tg_user_id'), name)


def receipt_view(row,claims,page=0):
    pages=max(1,(len(claims)+RESULTS_PER_PAGE-1)//RESULTS_PER_PAGE)
    page=max(0,min(page,pages-1))
    body=f'🌸 <b>红包圆满收官</b>\n{row["total"]} 积分 · {row["parts"]} 份都找到主人啦'
    for c in claims[page*RESULTS_PER_PAGE:(page+1)*RESULTS_PER_PAGE]:
        body+='\n'+receipt_mention(c)+f'  ·  <b>{c["amount"]}</b> 积分'
    if row['mode']=='random' and claims:
        best=min(claims,key=lambda c:(-c['amount'],c['slot']))
        body+='\n\n✨ 手气最佳 '+receipt_mention(best)+f' · {best["amount"]} 积分'
    body+='\n<i>愿好手气与你相伴。</i>'
    keys=[]
    if pages>1:
        nav=[]
        if page:nav.append({'text':'‹ 上一页','callback_data':f'rpresult:{row["nonce"]}:{page-1}'})
        nav.append({'text':f'{page+1}/{pages}','callback_data':f'rpresult:{row["nonce"]}:{page}'})
        if page+1<pages:nav.append({'text':'下一页 ›','callback_data':f'rpresult:{row["nonce"]}:{page+1}'})
        keys=[nav]
    return {'text':body,'parse_mode':'HTML','reply_markup':{'inline_keyboard':keys}}


def complete_receipt(row, claims):
    """All recipients in new messages, not mentions hidden on edited pages.

    Freeze once in the final claim transaction. Oversized configured envelopes
    use as few messages as possible, with a durable cursor/ACK for every part.
    """
    header = f'🌸 <b>红包圆满收官</b>\n{row["total"]} 积分 · {row["parts"]} 份'
    messages, body = [], header
    lines = ['\n'+receipt_mention(c)
             +f' · {c["amount"]} 积分' for c in claims]
    if row['mode'] == 'random' and claims:
        best = min(claims, key=lambda c: (-c['amount'], c['slot']))
        lines.append('\n\n✨ 手气最佳 '+receipt_mention(best)+f' · {best["amount"]} 积分')
    count = 0
    for line in lines:
        if text_units(body+line) > 3800 or count >= 90:
            messages.append({'text': body, 'parse_mode': 'HTML', 'reply_markup': {'inline_keyboard': []}})
            body, count = header, 0
        body += line
        count += 1
    messages.append({'text': body, 'parse_mode': 'HTML', 'reply_markup': {'inline_keyboard': []}})
    return {'messages': messages, 'cursor': 0, 'message_ids': []}


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
                payload=complete_receipt(row,self.service.claims(nonce,row['claimed_count']))
                conn.execute('UPDATE red_packets SET receipt_payload=? WHERE nonce=?',(encode(payload),nonce))
                row=dict(row,receipt_payload=encode(payload))
            lease=clock+90
            conn.execute(f"UPDATE red_packets SET {kind}_state='sending',{kind}_lease=?,{kind}_attempts={kind}_attempts+1 WHERE nonce=?",(lease,nonce))
            job = dict(row,_effect=kind,_lease=lease,_attempts=row[kind+'_attempts']+1)
            if kind == 'receipt':
                payload = json.loads(row['receipt_payload'])
                job['_payload'] = payload['messages'][payload['cursor']] if 'messages' in payload else payload
            return job

    def finish(self,row,response,failure,*,now=None):
        kind=row['_effect'];clock=time.time() if now is None else float(now)
        mid=response.get('message_id') if isinstance(response,dict) else None
        success=(type(mid) is int and mid>0) if kind=='receipt' else response is True
        state='sent' if success else 'retry' if failure.get('state')=='retry' and row['_attempts']<3 else 'failed' if failure.get('state') in ('failed','retry') else 'unknown'
        error='' if success else ('置顶未确认，仍可正常领取' if kind=='pin' else '仅本红包解除置顶未确认' if kind=='unpin' else '结果消息未确认；未知不自动重发')
        extra = ''
        due = clock+max(5,int(failure.get('retry_after') or 10))
        receipt_id = row.get('receipt_message_id')
        payload = row.get('receipt_payload')
        if kind == 'receipt':
            if success:
                receipt_id = receipt_id or mid
                snapshot = json.loads(payload)
                if 'messages' in snapshot:
                    snapshot['message_ids'].append(mid)
                    snapshot['cursor'] += 1
                    payload = encode(snapshot)
                    if snapshot['cursor'] < len(snapshot['messages']):
                        state, due = 'pending', 0
            extra = ',receipt_message_id=?,receipt_payload=?'
            if success:
                extra += ',receipt_attempts=0'
        args=[state,error,due]
        if kind=='receipt':args.extend((receipt_id, payload))
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
