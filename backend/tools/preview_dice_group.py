"""Offline, synthetic previews from the exact production text/keyboard templates.

Run from backend: PYTHONPATH=. python tools/preview_dice_group.py OUTPUT_DIRECTORY
No database, Bot token, browser service or network access is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from html import escape
from pathlib import Path

from app.modules import dice_beacon as beacon
from app.modules.dice_group import GROUP_VERSION, LATEST_SIZE, PAGE_SIZE
from app.modules.dice_group_view import buttons, group_card, report_pages
from app.modules.dice_rules import settle_pool
from app.modules.game_mentions import text_units


def samples():
    row = {'nonce': '1234567890abcdef12345678', 'state': 'open', 'created': 1791741000,
           'deadline': 1791741180, 'timeout': 1791741780, 'round_number': beacon.future_round(1791741180),
           'rules': json.dumps({'version': GROUP_VERSION, 'draw_version': beacon.VERSION, 'chain': beacon.CHAIN, 'window': 180, 'wait': 600, 'fee': 0}),
           'secret': 'ab'*32, 'commitment': hashlib.sha256(bytes.fromhex('ab'*32)).hexdigest(),
           'seed': '', 'dice': '[]', 'evidence': '{}', 'result': '{}', 'group_page': -1, 'group_view': 'ledger'}
    tickets = []
    names = ['桃桃 🍑', '星河', '柚子汽水', '小鹿 ✨', '月亮邮差', '蓝莓派', '阿夏']
    for i in range(23):
        side = '大' if i % 3 else '小'
        tickets.append({'user_id': str(i % 7), 'tg_id': str(900001+i % 7), 'display_name': names[i % 7],
                        'amount': [2000, 500, 300, 120, 800, 150, 90][i % 7],
                        'items': json.dumps([{'pool': 'size', 'option': side}])})

    def snapshot(items, page=-1):
        pages = max(1, (len(items)+PAGE_SIZE-1)//PAGE_SIZE)
        page = 0 if page < 0 else page
        totals = []
        for side in ('大', '小'):
            selected = [t for t in items if json.loads(t['items'])[0]['option'] == side]
            totals.append({'option': side, 'amount': sum(t['amount'] for t in selected), 'people': len({t['user_id'] for t in selected})})
        return {'count': len(items), 'page': page, 'pages': pages, 'totals': totals,
                'latest': items[-LATEST_SIZE:][::-1], 'ledger': items[::-1][page*PAGE_SIZE:(page+1)*PAGE_SIZE]}

    views = []
    def add(title, r, s, report_body=None):
        body, keys = group_card(r, s, report_body=report_body)
        views.append({'title': title, 'text': body, 'parse_mode': 'HTML', 'reply_markup': {'inline_keyboard': keys}})
    add('01 · 开局 / 等待入场', row, snapshot([]))
    add('02 · 单边入场 / 等待对手', row, snapshot(tickets[:1]))
    add('03 · 热局 / 默认最新页', row, snapshot(tickets))
    add('04 · 历史第2页 / 顶部仍是最新投注', dict(row, group_page=1), snapshot(tickets, 1))
    add('05 · 封盘 / 盘口固定', dict(row, state='locked'), snapshot(tickets))
    bets = [dict(json.loads(t['items'])[0], amount=t['amount'], user_id=t['user_id']) for t in tickets]
    done = dict(row, state='settled', dice='[4,5,6]', seed='cd'*32,
                result=json.dumps({'size': settle_pool('size', bets, [4, 5, 6], 'cd'*32)}), group_view='summary')
    reports = report_pages(done, tickets)
    views.append({'title': '06 · 最终战报 / 每人净盈亏', 'text': reports[0], 'parse_mode': 'HTML',
                  'reply_markup': {'inline_keyboard': buttons(row['nonce'], pages=3, terminal=True)}})
    add('07 · 战报内查看完整流水', dict(done, group_view='ledger', group_page=1), snapshot(tickets, 1))
    add('08 · 规则详情', dict(row, group_view='rules'), snapshot(tickets))
    add('09 · 验真详情（合成演示，并非已验签开奖）', dict(done, group_view='proof'), snapshot(tickets))
    return views


def render(output):
    output.mkdir(parents=True, exist_ok=True)
    views = samples()
    panels = []
    for view in views:
        rows = ''.join('<div class="keys">'+''.join('<span>'+escape(b['text'])+'</span>' for b in row)+'</div>' for row in view['reply_markup']['inline_keyboard'])
        panels.append('<section><h2>'+escape(view['title'])+'</h2><div class="phone"><div class="chat-head">🎲　积分游乐场 <small>BOT · 合成演示</small></div><article>'+view['text'].replace('\n', '<br>')+'<footer>02:00　✓✓</footer></article>'+rows+'</div></section>')
        assert text_units(view['text']) <= 4096
        assert all(len(b['callback_data'].encode()) <= 64 for row in view['reply_markup']['inline_keyboard'] for b in row)
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>骰局 · 群内大小改版</title>
<style>
*{box-sizing:border-box}body{margin:0;color:#e9efff;background:radial-gradient(ellipse at top,#242951,#101322 70%);font:15px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}header{max-width:1100px;margin:56px auto 26px;padding:0 24px}h1{font-size:38px;margin:0;background:linear-gradient(100deg,#94b9ff,#ebaeff,#ffc585);background-clip:text;color:transparent}header p{color:#aebad2}.badge{border:1px solid #586393;border-radius:30px;padding:6px 14px;font-size:12px;color:#b9caff}main{max-width:1380px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:26px;padding:20px 24px 80px;align-items:start}h2{font-size:15px;color:#c3cee7;margin:0 0 12px;font-weight:500}.phone{border-radius:24px;background:linear-gradient(140deg,#202d40,#172538);border:1px solid #3b4c69;padding:14px;box-shadow:0 18px 38px #070b1670}.chat-head{padding:6px 5px 17px;border-bottom:1px solid #354965;margin-bottom:14px;font-weight:700}.chat-head small{display:block;padding-left:36px;color:#7e9cba;font-size:11px;font-weight:400}article{background:#263b50;border-radius:16px 16px 16px 5px;padding:18px 16px 8px;overflow-wrap:anywhere;color:#eff6fc;box-shadow:0 2px 10px #09152655}article b{color:#fff}article a{color:#91d1ff;text-decoration:none}article i{font-size:12px;color:#b2c6d9}code,pre{font-family:ui-monospace,monospace;color:#b6d9ff;background:#14283b;border-radius:5px;padding:2px 4px;font-size:12px}pre{white-space:pre-wrap}footer{font-size:10px;text-align:right;color:#8faabe;margin-top:10px}.keys{display:flex;gap:5px;margin-top:5px}.keys span{flex:1;text-align:center;background:#2f4661b8;border-radius:7px;padding:9px 1px;font-size:12px;color:#c4e1ff}.note{font-size:12px;color:#92a4c5} @media(max-width:450px){header{margin-top:30px}h1{font-size:28px}main{padding:10px 12px}}
</style><header><span class="badge">DICE CLUB · GROUP EDITION</span><h1>🎲 大小对决 · 热闹在群里</h1><p>180秒入场 · 回复即下注 · 完整流水 · 0抽成</p><div class="note">源模板离线预览 · 全部为合成数据 · 未向 Telegram 发送<br>正文与按钮直接来自生产模板；背景、手机外框与字体是浏览器展示壳，实际 Telegram 外观随客户端主题变化。点击昵称链接不会在此预览触发下注。</div></header><main>'''+''.join(panels)+'</main></html>'
    (output/'index.html').write_text(html, encoding='utf-8')
    (output/'telegram-payloads.json').write_text(json.dumps(views, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'bounds.json').write_text(json.dumps([{'title': v['title'], 'utf16_units': text_units(v['text']), 'max_callback_bytes': max(len(b['callback_data'].encode()) for row in v['reply_markup']['inline_keyboard'] for b in row)} for v in views], ensure_ascii=False, indent=2), encoding='utf-8')
    print(output/'index.html')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    render(parser.parse_args().output)
