"""Colorful HTML group cards; full immutable ledger, no per-player cards."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from html import escape

from app.modules import dice_beacon as beacon
from app.modules.dice_group import PAGE_SIZE, TERMINAL
from app.modules.game_mentions import result_mention, text_units

HELP = ('🎲 <b>骰局 · 大小对决</b>\n\n'
        '在授权群发 /骰局，180秒封盘。\n'
        '<b>回复本局Bot卡</b>发送 <code>/大 2000</code> 或 <code>/小 500</code>，指令即确认扣款。\n'
        '小：4–10点；大：11–17点。三骰相同为豹子，全部退款。\n'
        '0抽成，最低1整数积分，不设人为金额上限，须余额及安全整数范围允许。'
        '命中者拿回本金，按金额分享对方本金；没有有效对手则全退。\n'
        '允许不同金额、追加及反向追加，每条新指令是新扣款。<b>确认后不可撤注、减额或移动旧注。</b>\n'
        '返还倍数含本金，随投注变化，以封盘为准；整数余分按用户聚合后最大余数法分配。\n'
        '最新投注固定在卡片上方；完整流水按确认顺序分页，共享同一张群卡。\n'
        '固定未来drand轮次并验签，不重摇。封盘后最多等10分钟，无有效数据全局退款；已有结果只按原结果结算。\n'
        '仅站内积分。旧七池局仍按原90秒、七池规则收尾。')


def clock(ts):
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime('%H:%M:%S')


def ratio(total, stake):
    if not stake:
        return '—'
    value = total*100//stake
    return f'{value//100}.{value%100:02d}×'


def result_heading(row):
    dice = json.loads(row['dice'])
    allocation = json.loads(row['result']).get('size', {})
    reason = allocation.get('reason', '')
    title = '本局无人下注' if row['state'] == 'empty' else '本局全额退回' if reason else '本局揭晓'
    body = f'🎲 <b>骰局 · {title}</b>'
    if dice:
        category = '豹子' if len(set(dice)) == 1 else '大' if sum(dice) >= 11 else '小'
        body += '\n\n'+'　'.join('⚀⚁⚂⚃⚄⚅'[d-1] for d in dice)+f'　<b>{sum(dice)}点 · {category}</b>'
    elif row['state'] == 'refunded':
        body += '\n固定信标超时 · 本金已退回'
    return body


def markets(totals, *, row=None):
    sides = {t['option']: t for t in totals}
    total = sum(t['amount'] for t in totals)
    active = row is None or row['state'] not in TERMINAL
    result = json.loads(row['result']).get('size', {}) if not active else {}
    dice = json.loads(row['dice']) if row else []
    winner = ('大' if sum(dice) >= 11 else '小') if dice else None
    lines = []
    for side, icon in (('大', '🔴'), ('小', '🔵')):
        amount, people = sides.get(side, {}).get('amount', 0), sides.get(side, {}).get('people', 0)
        if active:
            multiple = '≈'+ratio(total, amount) if all(sides.get(s, {}).get('amount', 0) for s in ('大', '小')) else '等待对手'
        elif not amount:
            multiple = '—'
        elif result.get('reason'):
            multiple = '退款 1.00×'
        else:
            multiple = ('≈'+ratio(total, amount)) if side == winner else '0.00×'
        lines.append(f'{icon} <b>{side}</b>　{amount:,} 分 · {people}人\n　└ 返还 <b>{multiple}</b>')
    return '\n'.join(lines)+f'\n\n💎 总池 <b>{total:,}</b> 分'


def bet_line(ticket, number):
    item = json.loads(ticket['items'])[0]
    side = item['option']
    mention = result_mention(ticket['tg_id'], ticket['display_name'][:18])
    return f'{"🔴" if side == "大" else "🔵"} {mention}　{side} <b>{ticket["amount"]:,}</b>　<code>#{number}</code>'


def buttons(nonce, *, page=0, pages=1, terminal=False):
    def key(label, op):
        return {'text': label, 'callback_data': f'dg:{nonce}:{op}'}
    pager = []
    if page > 0:
        pager.append(key('‹ 上页', f'page{page-1}'))
    pager.append(key(f'流水 {page+1}/{pages}', f'page{page}'))
    if page+1 < pages:
        pager.append(key('下页 ›', f'page{page+1}'))
    return [pager, [key('↻ 回最新', 'latest'), key('🏁 战报' if terminal else '🎲 盘口', 'summary' if terminal else 'latest')],
            [key('📖 规则', 'rules'), key('🔐 验真', 'proof')]]


def group_card(row, snapshot, *, report_body=None):
    terminal = row['state'] in TERMINAL
    if terminal:
        head = result_heading(row)
    else:
        state = '投注中' if row['state'] in ('opening', 'open') else '已封盘 · 待揭晓'
        head = f'🎲 <b>骰局 · 大小对决</b>　{state}\n⏳ {clock(row["deadline"])} 封盘 · 0抽成'
    body = head+'\n\n⚡ <b>最新入场</b>'
    body += '\n'+'\n'.join(bet_line(t, snapshot['count']-i) for i, t in enumerate(snapshot['latest'])) if snapshot['latest'] else '\n第一笔，等你落子 ✨'
    body += '\n\n'+markets(snapshot['totals'], row=row)
    if not terminal:
        body += '\n\n↳ 回复本卡 <code>/大 2000</code> 或 <code>/小 500</code>\n<b>发送即确认，不可撤注</b>\n<i>参考返还含本金，随投注变动，以封盘为准。</i>'
    else:
        body += '\n<i>最终返还含本金；整数余分按用户分配。</i>'
    view = row.get('group_view', 'ledger')
    if view == 'summary' and report_body and terminal:
        # The immutable report remains the source of truth when returning from history.
        body = report_body
    elif view == 'rules':
        body += '\n\n'+HELP
    elif view == 'proof':
        evidence = {'rules': json.loads(row['rules']), 'chain': beacon.CHAIN, 'public_key': beacon.PUBLIC_KEY,
                    'scheme': beacon.SCHEME, 'nonce': row['nonce'], 'round': row['round_number'],
                    'commitment': row['commitment'], 'deadline': row['deadline'], 'timeout': row['timeout']}
        if row['state'] not in ('opening', 'open'):
            evidence.update(secret=row['secret'], seed=row['seed'], dice=json.loads(row['dice']), beacon=json.loads(row['evidence']))
        body += '\n\n🔐 <b>开奖证据</b>\n<pre>'+escape(json.dumps(evidence, ensure_ascii=False, separators=(',', ':')))+'</pre>'
        body += '\n推导：SHA256(紧凑ASCII JSON[draw_version,chain,round,secret,nonce,randomness])；SHA256(seed字节＋8字节大端计数器)，从0开始，拒绝252–255，%6＋1取前三个。承诺=SHA256(secret字节)。BLS G2Basic验签消息=SHA256(previous_signature字节＋8字节大端轮次)。'
    else:
        body += f'\n\n📜 <b>完整流水</b>　{snapshot["page"]+1}/{snapshot["pages"]} · 共{snapshot["count"]}笔'
        body += '\n'+'\n'.join(bet_line(t, snapshot['page']*PAGE_SIZE+i+1) for i, t in enumerate(snapshot['ledger'])) if snapshot['ledger'] else '\n暂无投注'
        if row.get('group_page', -1) >= 0 and not terminal:
            body += '\n<i>正在查看历史页 · 上方最新区持续更新</i>'
    keys = buttons(row['nonce'], page=snapshot['page'], pages=snapshot['pages'], terminal=terminal)
    assert text_units(body) <= 4096
    assert all(len(b['callback_data'].encode()) <= 64 for line in keys for b in line)
    return body, keys


def report_pages(row, tickets):
    allocation = json.loads(row['result'])['size']
    totals = {}
    users = {}
    for ticket in tickets:
        item = json.loads(ticket['items'])[0]
        side = totals.setdefault(item['option'], {'option': item['option'], 'amount': 0, 'ids': set()})
        side['amount'] += ticket['amount']
        side['ids'].add(ticket['user_id'])
        users.setdefault(ticket['user_id'], ticket)
    header = result_heading(row)+'\n\n'+markets([dict(s, people=len(s['ids'])) for s in totals.values()], row=row)
    header += '\n<i>最终返还含本金 · 本局已结算</i>'
    lines = []
    for uid, ticket in users.items():
        net = allocation['users'][uid]['net']
        label = '持平' if net == 0 else f'{net:+,}'
        lines.append(result_mention(ticket['tg_id'], ticket['display_name'][:24])+'　'+label)
    chunks = [lines[i:i+20] for i in range(0, len(lines), 20)] or [[]]
    pages = []
    for i, chunk in enumerate(chunks):
        body = header+'\n\n🏅 <b>本局净盈亏</b>\n'+'\n'.join(chunk)
        if len(chunks) > 1:
            body += f'\n战报 {i+1}/{len(chunks)}'
        assert text_units(body) <= 4096
        pages.append(body)
    return pages
