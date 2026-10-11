"""Render the real source templates locally; no Telegram requests."""
import json
from html import escape
from pathlib import Path

from app.modules.dice_view import group_card, private_card
from app.modules.economy_rules import encode

ROOT = Path(__file__).resolve().parent
record = json.loads((ROOT/'live-beacon.json').read_text())
live = json.loads((ROOT/'live-render.json').read_text())
row = {**record, 'state':'open'}
uid = next(iter(json.loads(record['result'])['size']['users']))
session = {'token':'preview0123456789abcd', 'revision':3, 'user_id':uid,
           'tg_id':'901', 'payload':encode({'view':'home','draft':[]})}
private, keys = private_card(session,row,1000,[],{})
sections = [('群内开局', *group_card(row,2,'MediaDeckDemoBot')), ('私聊首页',private,keys)]
draft = [{'pool':'size','option':'大','amount':100}, {'pool':'single','option':'2','amount':20}, {'pool':'triple','option':'任意豹子','amount':10}]
session['payload'] = encode({'view':'draft','draft':draft})
sections.append(('多项确认',*private_card(session,row,1000,[],{})))
sections.append(('最终群战报 · 真实信标模拟局',live['result']['body'],[]))
sections.append(('本人结算 · 真实信标模拟局',live['personal_result']['body'],[]))
css = '''body{margin:0;background:#111a24;color:#e6eef6;font:16px/1.65 system-ui,"Noto Sans CJK SC",sans-serif}header{padding:30px 38px;border-bottom:1px solid #263749}h1{font-size:24px;margin:0}header p{font-size:13px;color:#8da5b9;margin:5px 0 0}.grid{display:grid;grid-template-columns:repeat(3,410px);gap:26px;padding:28px;align-items:start}.section h2{font-size:14px;color:#89a4b9;font-weight:500;margin:0 0 10px}.bubble{background:#203346;border:1px solid #31475c;border-radius:16px;padding:21px 22px;white-space:pre-wrap;overflow-wrap:anywhere;box-shadow:0 8px 30px #0002}code{font-size:12px;color:#9fc4df}a{color:#8bcaff;text-decoration:none;pointer-events:none}b{color:#fff;font-weight:650}.keys{margin-top:7px;display:grid;gap:5px}.keyrow{display:flex;gap:5px}.key{flex:1;background:#294258;border:1px solid #36536a;border-radius:7px;padding:9px 4px;text-align:center;color:#bedbef;font-size:13px}small{color:#829bae;font-size:11px}'''
html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>骰局 · 本地开发预览</title><style>'+css+'</style><header><h1>🎲 骰局 · 玩家对赌</h1><p>当前源码模板 · 本地预览，未向Telegram发送。真实信标模拟局：drand #'+str(record['round_number'])+'</p></header><main class="grid">'
for label,body,keyboard in sections:
    html += '<section class="section"><h2>'+escape(label)+'</h2><div class="bubble">'+body+'</div><div class="keys">'
    for line in keyboard:
        html += '<div class="keyrow">'+''.join('<div class="key">'+escape(b['text'])+'</div>' for b in line)+'</div>'
    html += '</div></section>'
html += '</main></html>'
(ROOT/'preview.html').write_text(html)
if __name__ == '__main__':
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':1360,'height':900},device_scale_factor=1)
        page.goto((ROOT/'preview.html').as_uri())
        page.screenshot(path=str(ROOT/'preview.png'),full_page=True)
        for i, element in enumerate(page.locator('.section').all(),1):
            element.screenshot(path=str(ROOT/f'card-{i}.png'))
        browser.close()
