"""One new result per newly finished room; old receipts are never backfilled."""
from __future__ import annotations

import json
import time

from app.modules.economy_rules import economy_write, encode
from app.modules.game_mentions import result_mention
from app.modules.red_packets import public_name


def result_name(player):
    # Only the captured public name; never an account login or a network lookup.
    name = public_name({'first_name': player.get('display_name')})
    return name[:39]+'…' if len(name) > 40 else name


def result_amount(row, player):
    if row['state'] == 'cancelled':return f'退回 {player["result_amount"]} 积分'
    banker = json.loads(row['config_json']).get('game') == 'niuniu-banker-v1'
    bank = banker and player['user_id'] == row['actor_user_id']
    value = player['result_amount']-row['stake']*(4 if bank else 1)
    return f'{value:+d} 积分' if value else '0 积分 · 持平'


def result_caption(row, players):
    banker = json.loads(row['config_json']).get('game') == 'niuniu-banker-v1'
    body = '🐂 <b>牛牛 · 本局战报</b>'
    groups = [('庄家', [p for p in players if p['user_id'] == row['actor_user_id']]),
              ('闲家', [p for p in players if p['user_id'] != row['actor_user_id']])] if banker else [('玩家', players)]
    for title, entries in groups:
        if not entries:continue
        body += '\n\n<b>'+title+'</b>'
        for p in entries:
            body += '\n'+result_mention(p['tg_id'], result_name(p))+'  ·  <b>'+result_amount(row, p)+'</b>'
    return body


def queue_result(conn, service, nonce):
    # Called inside the same money transaction, after the immutable outcome.
    row = service.get(nonce)
    snapshot = {key: row[key] for key in ('state', 'stake', 'config_json', 'actor_user_id')}
    snapshot['result_layout'] = 'war-report-v1'
    players = [{key: p[key] for key in ('user_id', 'tg_id', 'display_name', 'tg_username', 'cards_json', 'result_amount')}
               for p in service.players(nonce)]
    payload = encode({'row': snapshot, 'players': players, 'caption': result_caption(snapshot, players)})
    conn.execute("UPDATE niuniu_rounds SET result_state='pending',result_payload=?,card_delete_state='waiting' WHERE nonce=? AND result_state=''", (payload, nonce))


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
        self.db.execute("UPDATE niuniu_rounds SET result_state=?,result_message_id=?,result_due=?,result_error=?,card_delete_state=CASE WHEN ? AND card_delete_state='waiting' THEN 'pending' ELSE card_delete_state END WHERE nonce=? AND result_state='sending' AND result_lease=?",
                        (state, mid if success else None, clock+max(5, int(failure.get('retry_after') or 10)),
                         '' if success else '结果未确认；未知不自动重发', success, row['nonce'], row['_lease']))

    def claim_delete(self, nonce, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row = self.service.get(nonce)
            if (not row or row['bot_id'] != self.service.bot_id or row['result_state'] != 'sent'
                    or not row['result_message_id'] or not row['card_message_id']
                    or row['card_message_id'] == row['result_message_id']):return None
            state = row['card_delete_state']
            if state == 'deleting' and row['card_delete_lease'] > clock:return None
            if state not in ('pending', 'retry', 'deleting') or row['card_delete_due'] > clock:return None
            if row['card_delete_attempts'] >= 8:
                conn.execute("UPDATE niuniu_rounds SET card_delete_state='failed' WHERE nonce=?", (nonce,))
                return None
            lease = clock+45
            conn.execute("UPDATE niuniu_rounds SET card_delete_state='deleting',card_delete_lease=?,card_delete_attempts=card_delete_attempts+1 WHERE nonce=?", (lease, nonce))
            return dict(row, _delete_lease=lease, _delete_attempts=row['card_delete_attempts']+1)

    def finish_delete(self, row, success, failure, *, now=None):
        clock = time.time() if now is None else float(now)
        state = 'deleted' if success else 'failed' if failure.get('state') == 'failed' or row['_delete_attempts'] >= 8 else 'retry'
        due = clock+max(int(failure.get('retry_after') or 0), min(300, 5*2**min(row['_delete_attempts'], 6)))
        self.db.execute("UPDATE niuniu_rounds SET card_delete_state=?,card_delete_due=? WHERE nonce=? AND card_delete_state='deleting' AND card_delete_lease=?",
                        (state, due, row['nonce'], row['_delete_lease']))
