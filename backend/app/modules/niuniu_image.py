"""One original five-seat picture, from the waiting card to the public result."""
from __future__ import annotations

import json
from io import BytesIO

from PIL import Image, ImageDraw

from app.modules.niuniu import CATEGORIES, strength
from app.modules.niuniu_delivery import result_amount, result_name
from app.modules.poker_image import face, font


def net_label(value):
    return f'赢 {value}积分' if value > 0 else f'输 {-value}积分' if value < 0 else '本局持平'


def render_result(row, players):
    """A compact, readable report; lobby cards retain their existing layout."""
    banker = json.loads(row['config_json']).get('game') == 'niuniu-banker-v1'
    ordered = sorted(players, key=lambda p: p['user_id'] != row['actor_user_id']) if banker else players
    refunded = row['state'] == 'cancelled'
    height = 118 if refunded else 248
    image = Image.new('RGB', (1100, 174+height*len(ordered)), '#10272d')
    d = ImageDraw.Draw(image)
    d.text((48, 39), '牛牛 · 本局战报', font=font(42), fill='#e6cca0')
    for seat, p in enumerate(ordered):
        y = 130+seat*height
        bank = banker and p['user_id'] == row['actor_user_id']
        d.rounded_rectangle((32, y, 1068, y+height-14), radius=20, fill='#19363b', outline='#a78b59' if bank else '#2b4a50', width=2 if bank else 1)
        role = '庄家' if bank else '闲家' if banker else '玩家'
        d.text((52, y+18), role, font=font(22), fill='#d4b77f' if bank else '#95b3b5')
        name = result_name(p)
        size = 30
        while size > 14 and d.textlength(name, font=font(size)) > 574:size -= 1
        d.text((126, y+13), name, font=font(size), fill='#edf3eb')
        amount = result_amount(row, p)
        color = '#a9cebb' if amount.startswith('+') else '#e0aba5' if amount.startswith('-') else '#d4b77f'
        d.text((1036, y+16), amount, font=font(26), fill=color, anchor='ra')
        cards = json.loads(p['cards_json'])
        if refunded:
            d.text((126, y+62), '本局退款', font=font(20), fill='#95b3b5')
        elif cards:
            d.text((1036, y+54), CATEGORIES[strength(cards)[0]], font=font(20), fill='#95b3b5', anchor='ra')
            for index, card_id in enumerate(cards):
                card = face(card_id)
                card.thumbnail((120, 168))
                image.paste(card, (92+index*184, y+59))
    out = BytesIO()
    image.save(out, format='PNG', optimize=True)
    return out.getvalue()


def render_room(row, players):
    if row.get('result_layout') == 'war-report-v1':return render_result(row, players)
    banker = json.loads(row['config_json']).get('game') == 'niuniu-banker-v1'
    image = Image.new('RGB', (1100, 1510), '#102b35')
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((24, 20, 1076, 1490), radius=30, fill='#143840', outline='#c6a468', width=2)
    d.text((48, 47), '牛牛 · 庄闲对决' if banker else '牛牛 · 旧局原规则', font=font(44), fill='#f2dbaa')
    subtitle = f'闲家每位 {row["stake"]} 积分   ·   庄家担保 {4*row["stake"]} 积分' if banker else f'每人 {row["stake"]} 积分'
    d.text((49, 111), subtitle, font=font(26), fill='#c1d8d8')
    phase = '五张揭晓 · 各自对庄' if row['state'] == 'settled' and banker else '五张揭晓' if row['state'] == 'settled' else '本局结束 · 投入已退回' if row['state'] == 'cancelled' else f'闲家已入座 {len(players)-1}/4 · 一位起庄家可开牌' if banker else f'已入座 {len(players)}/5'
    d.text((49, 155), phase, font=font(24), fill='#9cc6c4')
    for seat in range(5):
        p = players[seat] if seat < len(players) else None
        y = 226+seat*248
        is_bank = p is not None and p['user_id'] == row['actor_user_id'] and banker
        role = '庄' if is_bank else '闲' if banker else '玩家'
        name = p['display_name'][:18] if p else '等待入座'
        label = f'{role} · {name}'
        cards = json.loads(p['cards_json']) if p else []
        if row['state'] == 'settled' and cards:
            held = row['stake']*(4 if is_bank else 1)
            label += ' · '+CATEGORIES[strength(cards)[0]]+' · '+net_label(p['result_amount']-held)
        elif row['state'] == 'cancelled' and p:
            label += f' · 已退 {p["result_amount"]}积分'
        d.text((50, y), label, font=font(24), fill='#f2dbaa' if is_bank else '#e0ebdf' if p else '#7fa0a6')
        for index in range(5):
            x, top = 53+index*204, y+45
            if row['state'] == 'settled' and cards:
                card = face(cards[index])
                card.thumbnail((140, 198))
                image.paste(card, (x+20, top))
            else:
                fill = '#225862' if p else '#1c434a'
                d.rounded_rectangle((x+20, top, x+159, top+198), radius=12, fill=fill, outline='#bda776' if is_bank else '#4a7379', width=2)
                d.rounded_rectangle((x+30, top+11, x+149, top+187), radius=8, outline='#547e82', width=1)
                d.text((x+70, top+68), '牛' if p else '·', font=font(41), fill='#8ab5b2' if p else '#45656c')
    out = BytesIO()
    image.save(out, format='PNG', optimize=True)
    return out.getvalue()
