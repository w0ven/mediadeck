"""Single-photo size-pool cards with lossless UTF-16-budgeted pagination."""
from __future__ import annotations

import json

from app.modules.dice_group import TERMINAL
from app.modules.dice_group_view import clock, markets, result_heading
from app.modules.game_mentions import result_mention, text_units

PRESENTATION = 'caption-v1'


def is_caption(row):
    return json.loads(row['rules']).get('presentation') == PRESENTATION


def pages(row, snapshot):
    terminal = row['state'] in TERMINAL
    if terminal:
        header = result_heading(row)
    else:
        state = '投注中' if row['state'] in ('opening', 'open') else '已封盘 · 待揭晓'
        header = f'🎲 <b>大小对决</b>　{state}\n⏳ {clock(row["deadline"])} 封盘 · 0抽成'
    header += '\n\n'+markets(snapshot['totals'], row=row)
    if not terminal:
        header += '\n\n<code>/大 金额</code> · <code>/小 金额</code>\n发送即确认，不可撤注'
    tickets = snapshot['tickets']  # newest first, original ticket numbers retained
    entries = []
    ledger = f'📜 <b>完整流水</b> · 共{len(tickets)}笔'
    users = {}
    for i, ticket in enumerate(tickets):
        side = json.loads(ticket['items'])[0]['option']
        mention = result_mention(ticket['tg_id'], ticket['display_name'])
        entries.append((ledger, f'{mention}　{side} <b>{ticket["amount"]:,}</b>　<code>#{len(tickets)-i}</code>'))
        users.setdefault(ticket['user_id'], ticket)
    if not entries:
        entries.append((ledger, '暂无投注'))
    if terminal:
        allocation = json.loads(row['result'])['size']['users']
        for uid, ticket in users.items():
            net = allocation[uid]['net']
            label = '持平' if net == 0 else f'{net:+,}'
            entries.append(('🏅 <b>本局净盈亏</b>', result_mention(ticket['tg_id'], ticket['display_name'])+'　'+label))
    # Reserve the longest possible footer before packing; never slice an HTML line.
    reserve = '\n\n'+str(len(entries))+'/'+str(len(entries))
    chunks, body, section, count = [], header, None, 0
    for title, line in entries:
        extra = ('\n\n'+title if title != section else '')+'\n'+line
        if count and ((title == ledger and count >= 8) or text_units(body+extra+reserve) > 1024):
            chunks.append(body)
            body, section, count = header, None, 0
            extra = '\n\n'+title+'\n'+line
        if text_units(body+extra+reserve) > 1024:
            raise ValueError('dice caption entry exceeds budget')
        body += extra
        section, count = title, count+1
    chunks.append(body)
    return [body+(f'\n\n{i+1}/{len(chunks)}' if len(chunks) > 1 else '') for i, body in enumerate(chunks)]


def card(row, snapshot, *, first=False):
    bodies = pages(row, snapshot)
    page = 0 if first else min(max(0, row['group_page']), len(bodies)-1)
    keys = []
    if len(bodies) > 1:
        pager = []
        if page:
            pager.append({'text': '‹ 上页', 'callback_data': f'dg:{row["nonce"]}:page{page-1}'})
        pager.append({'text': f'{page+1}/{len(bodies)} · 最新', 'callback_data': f'dg:{row["nonce"]}:latest'})
        if page+1 < len(bodies):
            pager.append({'text': '下页 ›', 'callback_data': f'dg:{row["nonce"]}:page{page+1}'})
        keys = [pager]
    return bodies[page], keys
