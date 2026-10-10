"""One new result per newly finished room; old receipts are never backfilled."""
from __future__ import annotations

import json
import time

from app.modules.economy_rules import economy_write, encode
from app.modules.game_mentions import result_mention


def result_caption(row, players):
    banker = json.loads(row['config_json']).get('game') == 'niuniu-banker-v1'
    body = '🐂 <b>牛牛 · 本局揭晓</b>' if row['state'] == 'settled' else '🐂 <b>牛牛 · 本局结束</b>'
    if banker and row['state'] == 'settled':body += '\n各自对庄'
    for p in players:
        bank = banker and p['user_id'] == row['actor_user_id']
        role = ('庄 · ' if bank else '闲 · ') if banker else ''
        if row['state'] == 'cancelled':
            amount = f'退回 {p["result_amount"]} 积分'
        else:
            value = p['result_amount']-row['stake']*(4 if bank else 1)
            amount = f'赢 {value}积分' if value > 0 else f'输 {-value}积分' if value < 0 else '本局持平'
        body += '\n'+role+result_mention(p['tg_id'], p['display_name'], p.get('tg_username'))+' · '+amount
    return body


def queue_result(conn, service, nonce):
    # Called inside the same money transaction, after the immutable outcome.
    row = service.get(nonce)
    snapshot = {key: row[key] for key in ('state', 'stake', 'config_json', 'actor_user_id')}
    players = [{key: p[key] for key in ('user_id', 'tg_id', 'display_name', 'tg_username', 'cards_json', 'result_amount')}
               for p in service.players(nonce)]
    payload = encode({'row': snapshot, 'players': players, 'caption': result_caption(snapshot, players)})
    conn.execute("UPDATE niuniu_rounds SET result_state='pending',result_payload=? WHERE nonce=? AND result_state=''", (payload, nonce))


class NiuniuDelivery:
    def __init__(self, service):self.service, self.db = service, service.db

    def claim(self, nonce, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row = self.service.get(nonce)
            if not row or row['bot_id'] != self.service.bot_id:return None
            if row['result_state'] == 'sending' and row['result_lease'] <= clock:
                conn.execute("UPDATE niuniu_rounds SET result_state='unknown',result_error='发送确认中断；不自动重复' WHERE nonce=?", (nonce,))
                return None
            if (row['result_state'] not in ('pending', 'retry') or row['result_due'] > clock
                    or not row['card_message_id'] or row['rendered_revision'] < row['revision']):return None
            lease = clock+90
            conn.execute("UPDATE niuniu_rounds SET result_state='sending',result_lease=?,result_attempts=result_attempts+1 WHERE nonce=?", (lease, nonce))
            return dict(row, _lease=lease, _attempts=row['result_attempts']+1)

    def finish(self, row, response, failure, *, now=None):
        clock = time.time() if now is None else float(now)
        mid = response.get('message_id') if isinstance(response, dict) else None
        success = type(mid) is int and mid > 0
        state = ('sent' if success else 'retry' if failure.get('state') == 'retry' and row['_attempts'] < 3
                 else 'failed' if failure.get('state') in ('failed', 'retry') else 'unknown')
        self.db.execute("UPDATE niuniu_rounds SET result_state=?,result_message_id=?,result_due=?,result_error=? WHERE nonce=? AND result_state='sending' AND result_lease=?",
                        (state, mid if success else None, clock+max(5, int(failure.get('retry_after') or 10)),
                         '' if success else '结果未确认；未知不自动重发', row['nonce'], row['_lease']))
