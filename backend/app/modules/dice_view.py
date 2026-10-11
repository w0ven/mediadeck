"""Source templates used by both Telegram and offline previews."""
import json
from datetime import datetime, timedelta, timezone
from html import escape

from app.modules import dice_beacon as beacon
from app.modules.dice_rules import POOLS, hits
from app.modules.game_mentions import result_mention

RULES = ('七池独立 · 0抽成 · 最低1整数积分，仅限站内。\n'
         '命中先退本金，再按中奖权重分本池落败本金；无固定赔率，收益到封盘才确定。\n'
         '无人命中或无落败投注，本池全退。\n'
         '可多项、追加及反向投注，均是新增扣款。\n'
         '<b>确认后不可撤注、减额或移动旧注。</b>')
POOL_RULES = {
    'size': '小4–10，大11–17；豹子全退。按中奖金额分红。',
    'parity': '按三骰总和判单双；豹子全退。按中奖金额分红。',
    'single': '出现即命中；本金只退一次，分红权重=金额×出现次数。',
    'double': '指定点数出现至少两次即命中，含豹子；按中奖金额分红。',
    'combo': '两个不同点数同时出现即命中；多项可同中，按中奖金额分红。',
    'triple': '非豹子命中无豹子；豹子命中任意及对应指定项，共同按金额分红，指定无额外倍率。',
    'total': '三骰总和3–18精确命中；按中奖金额分红。',
}
HELP = ('🎲 <b>骰局 · 七池对赌</b>\n\n'+RULES+'\n\n'
        +'\n'.join(POOLS[p][0]+'：'+rule for p, rule in POOL_RULES.items())
        +'\n按用户＋池聚合后用最大余数法分整数余分，平局按固定种子、池和账户ID的哈希排序。'
        '\n90秒封盘，固定未来drand轮次并验签；最多等10分钟，无有效数据全局退款。已有结果只按原结果结算，绝不重摇。'
        '\n公开结果显示每人净盈亏，不公开余额或暗注。过程消息自动清理，最终群战报保留。'
        '\n\n授权群发送 <code>/骰局</code>，从群卡进入私聊下注。')


def clock(value):
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime('%H:%M:%S')


def group_card(row, participants, username):
    opened = row['state'] in ('opening', 'open')
    title = '等待封盘' if opened else '已封盘 · 开奖中' if row['state'] == 'locked' else '结果送达中'
    body = f'🎲 <b>骰局 · {title}</b>\n封盘 {clock(row["deadline"])} · 已下注 {participants} 人\n玩家对赌 · 确认后不可撤注'
    body += ('\n\n<blockquote expandable>开奖凭证\n公开锁定 · 不重摇\n展开查看完整承诺'
             f'\n局号 <code>{row["nonce"]}</code>\n固定信标 drand #{row["round_number"]}'
             f'\n承诺 <code>{row["commitment"]}</code>'
             '\n封盘后最多等待10分钟；无有效开奖数据则全局退款，已有结果按原结果结算。</blockquote>')
    keys = [[{'text': '进入私聊下注' if opened else '我的投注 · 验真', 'url': f'https://t.me/{username}?start=dice_{row["nonce"]}'},
             {'text': '查看本局', 'callback_data': f'dcg:{row["nonce"]}'}]]
    return body, keys


def private_card(session, row, balance, bets, result):
    p = json.loads(session['payload'])
    token, rev = session['token'], session['revision']
    def button(label, op, arg='0'):
        return {'text': label, 'callback_data': f'dc:{token}:{rev}:{op}:{arg}'}
    invested = sum(b['amount'] for b in bets)
    head = f'🎲 <b>骰局 · {row["nonce"][:8]}</b>\n{clock(row["deadline"])}封盘 · 已投 {invested} 分'
    view = p.get('view', 'home')
    keys = []
    if view == 'proof':
        evidence = {'version': beacon.VERSION, 'chain': beacon.CHAIN, 'public_key': beacon.PUBLIC_KEY,
                    'scheme': beacon.SCHEME, 'round': row['round_number'], 'commitment': row['commitment'],
                    'nonce': row['nonce'], 'deadline': row['deadline'], 'timeout': row['timeout']}
        if row['state'] not in ('opening', 'open'):
            evidence.update(secret=row['secret'], seed=row['seed'], dice=json.loads(row['dice']), beacon=json.loads(row['evidence']))
        body = head+'\n\n<b>开奖证据</b>\n<pre>'+escape(json.dumps(evidence, ensure_ascii=False, indent=2))+'</pre>'
        body += '\n算法：SHA256(JSON紧凑ASCII[版本,链,轮次,种子,局ID,randomness])；SHA256(摘要字节＋8字节大端计数器)，计数器从0开始；只取0–251，%6＋1，取前三个。承诺=SHA256(种子hex解码)。验签BLS G2Basic，消息=SHA256(前签名字节＋8字节大端轮次)。'
    elif view == 'mine':
        body = head+'\n\n<b>我的投注</b>'
        # Seven fixed pages keep every accepted choice readable even for a full portfolio.
        pool = p.get('pool', 'size')
        body += '\n'+POOLS[pool][0]
        merged = {}
        for b in bets:
            if b['pool'] == pool:
                merged[b['option']] = merged.get(b['option'], 0)+b['amount']
        dice = json.loads(row['dice'])
        winning = hits(dice)[pool] if dice else {}
        lines = []
        for o, a in merged.items():
            status = (' · 命中'+(f'×{winning[o]}' if pool=='single' else '') if o in winning else ' · 未中') if dice else ''
            lines.append(f'{escape(o)} · {a}分'+status)
        body += '\n'+('\n'.join(lines) or '本池未投注')
        outcome = result.get(pool, {}).get('users', {}).get(session['user_id'])
        if outcome:
            reason = result[pool]['reason']
            if reason:
                body += '\n'+{'no_hit':'无人命中，本池退款', 'no_loser':'无落败投注，本池退款', 'triple_refund':'豹子，本池退款', 'beacon_timeout':'信标超时，全局退款'}.get(reason, '')
            body += f'\n\n投入 {outcome["invested"]} · 命中本金 {outcome["winning"]}\n奖金 {outcome["bonus"]}（含余分 {outcome["extra"]}） · 退款 {outcome["refund"]}\n返还 {outcome["returned"]} · 净盈亏 {outcome["net"]:+d}'
        keys = [[button(name, 'mine', key) for key, (name, _) in list(POOLS.items())[i:i+3]] for i in range(0, 7, 3)]
    elif view == 'rules':
        body = HELP
    elif view == 'options':
        pool = p['pool']
        body = head+'\n\n<b>'+POOLS[pool][0]+'</b> · 选择投注项\n'+POOL_RULES[pool]
        options = POOLS[pool][1]
        keys = [[button(o, 'pick', str(i+j)) for j, o in enumerate(options[i:i+4])] for i in range(0, len(options), 4)]
    elif view == 'amount':
        pool, option = p['selected']
        body = head+f'\n\n{POOLS[pool][0]} · {escape(option)}\n选择金额，或<b>回复此卡</b>输入正整数。\n只加入草稿，尚未扣分。'
        body += '\n'+POOL_RULES[pool]+f'\n<code>输入凭据 {token[:6]}-{rev}</code>'
        keys = [[button(str(a), 'amount', str(a)) for a in (10, 50, 100)]]
    elif view in ('draft', 'success'):
        draft = p['draft']
        body = head+ ('\n\n✅ <b>下注已确认</b>\n追加是新扣款，旧注不变。' if view == 'success' else '\n\n<b>确认草稿</b>')
        # Each pool is collapsed to a single line, all options and amounts remain explicit.
        for pool, (name, _) in POOLS.items():
            lines = [f'{escape(i["option"])}={i["amount"]}' for i in draft if i['pool'] == pool]
            if lines:
                body += '\n'+name+' · '+' / '.join(lines)
        if view == 'draft':
            body += f'\n合计 <b>{sum(i["amount"] for i in draft)}</b> 分'
            body += '\n\n'+'\n'.join(POOLS[pool][0]+'：'+POOL_RULES[pool] for pool in POOLS if any(i['pool']==pool for i in draft))
            body += '\n\n'+RULES
            keys = [[button('确认全部', 'confirm'), button('继续加项', 'home')], [button('清空草稿（不动旧注）', 'clear')]]
        else:
            keys = [[button('继续追加', 'new'), button('我的投注', 'mine', 'size')]]
    else:
        body = head+f'\n可用积分 {balance}\n\n'+RULES
        keys = [[button(name, 'pool', key) for key, (name, _) in list(POOLS.items())[i:i+3]] for i in range(0, 7, 3)]
        keys += [[button('查看草稿', 'draft'), button('我的投注', 'mine', 'size')]]
    if p.get('notice'):
        body += '\n\n'+escape(p['notice'])
    if view not in ('home', 'success'):
        keys.append([button('返回', 'home')])
    keys.append([button('规则', 'rules'), button('开奖验真', 'proof')])
    return body, keys


def report_pages(row, tickets, username):
    results = json.loads(row['result'])
    dice = json.loads(row['dice'])
    title = '本局无人下注' if row['state'] == 'empty' else '本局全部退回' if all(r['reason'] for r in results.values()) else '已揭晓'
    head = '🎲 <b>骰局 · '+title+'</b>'
    if dice:
        head += '\n'+' '.join('⚀⚁⚂⚃⚄⚅'[d-1] for d in dice)+f'　{sum(dice)}点'
        head += ' · 豹子' if len(set(dice)) == 1 else (' · 大' if sum(dice) >= 11 else ' · 小')+(' · 单' if sum(dice) % 2 else ' · 双')
    if row['state'] == 'refunded':
        head += '\n固定信标等待超时，全部本金已退回'
    users = {}
    for t in tickets:
        users.setdefault(t['user_id'], t)
    lines = []
    for uid, t in users.items():
        net = sum(p['users'].get(uid, {}).get('net', 0) for p in results.values())
        lines.append(result_mention(t['tg_id'], t['display_name'])+'　'+('持平' if net == 0 else f'{net:+d}'))
    pages = [head+'\n\n'+'\n'.join(lines[i:i+25]) for i in range(0, len(lines), 25)] or [head]
    tail = f'\n\n<a href="https://t.me/{username}?start=dice_{row["nonce"]}">本人明细 · 开奖验真</a>'
    return [body+(f'\n{i+1}/{len(pages)}' if len(pages)>1 else '')+tail for i, body in enumerate(pages)]
