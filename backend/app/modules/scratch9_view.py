"""The original shared card: one PNG/caption/keyboard, never odds or private receipts."""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from io import BytesIO

from PIL import Image, ImageDraw

from app.modules.economy_rules import BEIJING
from app.modules.poker_image import font


def view(row, cells):
    cfg = json.loads(row['config_json'])
    found = {c['cell']: c for c in cells}
    ended = row['state'] == 'closed'
    text = f'🎟 <b>九宫格刮刮乐</b>\n每格 <b>{cfg["cost"]}</b> 积分 · 每人最多 {cfg["per_person"]} 格'
    text += '\n' + ('🍃 本场已结束' if ended else '⏳ 截止 '+datetime.fromtimestamp(row['expires_at'], BEIJING).strftime('%H:%M'))
    text += f' · 已刮 {len(cells)}/9\n'
    for c in cells:
        text += '\n'+f'{c["cell"]}号 · {escape(c["display_name"][:20])} · '+('🌟 ' if c['reward'] >= 300 else '')+f'<b>{c["reward"]}积分</b>'
    if not cells:
        text += '\n九格同享，选一格揭晓你的好运。'
    if not ended:
        text += '\n\n<i>同一格点击两次确认扣费；不再点击不扣费。奖励可能为0或低于投入。</i>'
    keyboard = []
    for start in (1, 4, 7):
        line = []
        for cell in range(start, start+3):
            winner = found.get(cell)
            label = (('🌟 ' if winner['reward'] >= 300 else '✓ ')+str(winner['reward'])+'分') if winner else ('— 未刮' if ended else '🎟 '+str(cell)+'号')
            line.append({'text': label, 'callback_data': f'gg:{row["nonce"]}:{cell}'})
        keyboard.append(line)
    return text, keyboard


def render(row, cells):
    """Render exactly the shared activity state used by its caption/keyboard."""
    cfg = json.loads(row['config_json'])
    found = {c['cell']: c for c in cells}
    image = Image.new('RGB', (1100, 1110), '#111e30')
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((32, 26, 1068, 1084), radius=34, fill='#172940', outline='#dcb867', width=2)
    d.text((72, 58), '九宫格刮刮乐', font=font(54), fill='#fff0c6')
    d.text((74, 139), f'每格 {cfg["cost"]} 积分   /   每人最多 {cfg["per_person"]} 格', font=font(27), fill='#becbdc')
    ended = row['state'] == 'closed'
    phase = '本场已结束 · 好运留在这里' if ended else '截止 '+datetime.fromtimestamp(row['expires_at'], BEIJING).strftime('%H:%M')+' · 同一格点两次，确认后揭晓'
    d.text((74, 185), phase, font=font(24), fill='#c9d3df')
    for cell in range(1, 10):
        col, line = (cell-1)%3, (cell-1)//3
        x, y = 73+col*326, 263+line*248
        c = found.get(cell)
        big = c is not None and c['reward'] >= 300
        fill = '#654923' if big else '#253f52' if c else '#263548'
        border = '#f4cd76' if big else '#6ca4a0' if c else '#596779'
        d.rounded_rectangle((x, y, x+300, y+219), radius=22, fill=fill, outline=border, width=3)
        d.text((x+22, y+17), f'{cell}号', font=font(23), fill='#b8c6d5')
        if c:
            d.text((x+22, y+65), str(c['reward'])+' 积分', font=font(40 if c['reward'] < 1000 else 31), fill='#ffdf91' if big else '#d6eade')
            name = c['display_name'][:9]
            d.text((x+22, y+145), name, font=font(23), fill='#f5eedf')
            if big:
                d.text((x+185, y+22), '大奖', font=font(24), fill='#ffe08e')
        else:
            d.text((x+55, y+75), '未刮' if ended else '好运待揭晓', font=font(31), fill='#b2c1d1')
            d.text((x+72, y+145), '—' if ended else '点击下方同号格', font=font(19), fill='#8199b2')
    d.text((74, 1039), f'已刮 {len(cells)}/9 · '+('本场结果保留' if ended else '奖励可能为0或低于投入'), font=font(23), fill='#becbdc')
    out = BytesIO()
    image.save(out, format='PNG', optimize=True)
    return out.getvalue()


def html_preview(row, cells, png_name):
    caption, keys = view(row, cells)
    buttons = ''.join('<div class="line">'+''.join('<span>'+escape(b['text'])+'</span>' for b in line)+'</div>' for line in keys)
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>共享九宫格刮刮乐 · 原卡预览</title><style>body{margin:0;background:#0d1825;color:#dbe6ef;font:16px system-ui}.card{max-width:550px;margin:30px auto;background:#192b40;border-radius:22px;overflow:hidden}img{width:100%;display:block}.caption{padding:20px;white-space:pre-line;line-height:1.7}.line{display:flex;gap:4px;margin:4px 8px}.line span{flex:1;text-align:center;padding:12px 0;background:#304962;border-radius:8px}.note{padding:12px;color:#899bad;font-size:12px}</style><div class="card"><img src="'+escape(png_name, quote=True)+'"><div class="caption">'+caption+'</div>'+buttons+'<div class="note">静态真实renderer预览；非Telegram投递。全群共享同一张九格卡。</div></div></html>'
