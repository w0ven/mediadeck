"""Single-photo size-pool cards with lossless UTF-16-budgeted pagination."""
from __future__ import annotations

import json
import math
import time

from app.modules import dice_quicknet as quicknet
from app.modules.dice_banker import QUICK_BANKER_VERSION, banker, is_banker
from app.modules.dice_group import TERMINAL
from app.modules.dice_group_view import markets, result_heading
from app.modules.game_mentions import result_mention, text_units

PRESENTATION = 'caption-v1'


def is_caption(row):
    return json.loads(row['rules']).get('presentation') == PRESENTATION


def pages(row, snapshot, *, now=None):
    terminal = row['state'] in TERMINAL
    cap = json.loads(row['rules']).get('pool_cap') if is_banker(row) else None
    total = sum(t['amount'] for t in snapshot['totals'])
    if terminal:
        header = result_heading(row)
    else:
        remaining = max(0, math.ceil(row['deadline']-(time.time() if now is None else now)))
        accepting = row['state'] in ('opening', 'open') and remaining > 0
        state = '投注中' if accepting else '已满额封盘 · 等待开奖' if cap is not None and total >= cap else '已封盘 · 等待开奖'
        remaining = remaining if accepting else 0
        header = f'🎲 <b>大小对决</b>　{state}\n⏳ 剩余 {remaining//60:02d}:{remaining%60:02d} · 0抽成'
    if is_banker(row):
        bank = banker(row)
        header += '\n\n👑 庄家 '+result_mention(bank['tg_id'], bank['display_name'])+f'　庄本 <b>{bank["capital"]:,}</b>'
        if terminal:
            net = json.loads(row['result'])['size']['banker']['net']
            label = '持平' if net == 0 else f'{net:+,}'
            header += '\n庄家净盈亏 <b>'+label+'</b>'
        sides = {t['option']: t['amount'] for t in snapshot['totals']}
        header += f'\n\n🔴 大 <b>{sides.get("大", 0):,}</b>　🔵 小 <b>{sides.get("小", 0):,}</b>'
        if cap is not None:
            header += f'\n总池 <b>{total:,} / {cap:,}</b>'
        header += '\n赢返2倍含本金 · 豹子通吃 · 0抽成'
    else:
        header += '\n\n'+markets(snapshot['totals'], row=row)
    if not terminal and json.loads(row['rules']).get('version') == QUICK_BANKER_VERSION:
        header += '\n<blockquote expandable>🔐 公平开奖\n<code>'+row['commitment']+'</code>'
        header += '\n封盘时刻＋2秒后的首轮 quicknet（3秒/轮）'
        if row['round_number']:
            header += f' · #{row["round_number"]}'
        header += '</blockquote>'
    if terminal and json.loads(row['rules']).get('version') == QUICK_BANKER_VERSION and row['evidence'] != '{}':
        header += (f'\n\n<blockquote expandable>🔐 验真 · <a href="https://api.drand.sh/{quicknet.CHAIN}/public/{row["round_number"]}">quicknet #{row["round_number"]}</a>'
                   f'\n局 <code>{row["nonce"]}</code> · 封盘 {row["closed_at"]}'
                   f'\n承诺 <code>{row["commitment"]}</code>\n种子 <code>{row["secret"]}</code></blockquote>')
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


def card(row, snapshot, *, first=False, now=None):
    bodies = pages(row, snapshot, now=now)
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
