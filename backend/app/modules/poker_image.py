"""Original vector-drawn real 52-card faces; no external assets or emoji cards."""
from __future__ import annotations

import threading
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.modules.poker import CATEGORIES, strength

FONT = Path(__file__).parent / 'rank_assets/font/PingFang-Bold.ttf'


@lru_cache(maxsize=64)
def _font(size, thread_id):
    # FreeType objects are reused within, not shared between, rendering threads.
    return ImageFont.truetype(str(FONT), size)


def font(size):
    return _font(size, threading.get_ident())


def suit(draw, x, y, s, kind, color):
    """0 spade, 1 heart, 2 club, 3 diamond, rendered as vector shapes."""
    if kind == 3:
        draw.polygon([(x, y-s), (x+s*.7, y), (x, y+s), (x-s*.7, y)], fill=color)
    elif kind == 1:
        r = s*.49
        draw.ellipse((x-s*.92, y-s*.75, x-s*.92+2*r, y-s*.75+2*r), fill=color)
        draw.ellipse((x-s*.06, y-s*.75, x-s*.06+2*r, y-s*.75+2*r), fill=color)
        draw.polygon([(x-s*.87, y-s*.02), (x+s*.87, y-s*.02), (x, y+s)], fill=color)
    elif kind == 0:
        draw.polygon([(x, y-s), (x-s*.85, y+s*.15), (x+s*.85, y+s*.15)], fill=color)
        draw.ellipse((x-s*.9, y-s*.18, x+s*.02, y+s*.72), fill=color)
        draw.ellipse((x-s*.02, y-s*.18, x+s*.9, y+s*.72), fill=color)
        draw.polygon([(x-s*.08, y+s*.3), (x+s*.08, y+s*.3), (x+s*.3, y+s), (x-s*.3, y+s)], fill=color)
    else:
        for dx, dy in [(0, -.5), (-.5, .15), (.5, .15)]:
            draw.ellipse((x+(dx-.47)*s, y+(dy-.47)*s, x+(dx+.47)*s, y+(dy+.47)*s), fill=color)
        draw.polygon([(x-s*.08, y+s*.25), (x+s*.08, y+s*.25), (x+s*.3, y+s), (x-s*.3, y+s)], fill=color)


def face(card):
    strength([card, (card+13)%52, (card+26)%52])  # validate card domain
    rank, kind = card % 13+2, card // 13
    color = '#b63544' if kind in (1, 3) else '#1c2937'
    image = Image.new('RGB', (188, 266), '#ffffff')
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((1, 1, 186, 264), radius=14, fill='#fffcf7', outline='#d8dee2', width=2)
    label = {11: 'J', 12: 'Q', 13: 'K', 14: 'A'}.get(rank, str(rank))
    corner = Image.new('RGBA', (44, 68))
    cd = ImageDraw.Draw(corner)
    cd.text((4, -1), label, font=font(30), fill=color)
    suit(cd, 20, 51, 10, kind, color)
    image.paste(corner, (8, 6), corner)
    rotated = corner.rotate(180)
    image.paste(rotated, (136, 192), rotated)
    if rank in (11, 12, 13):
        # Self-created royal portrait, border and mirrored decorative suit.
        d.rounded_rectangle((43, 52, 145, 214), radius=8, outline=color, width=2, fill='#f4e9ce')
        d.ellipse((70, 86, 120, 147), fill='#e5b99b', outline=color, width=2)
        d.polygon([(65, 88), (58, 61), (77, 72), (94, 56), (110, 72), (131, 61), (124, 88)], fill='#c6a154', outline=color)
        d.line((68, 88, 122, 88), fill=color, width=3)
        d.line((81, 110, 87, 110), fill=color, width=2)
        d.line((104, 110, 110, 110), fill=color, width=2)
        d.arc((85, 119, 108, 133), 0, 160, fill=color, width=2)
        d.polygon([(73, 149), (115, 149), (137, 197), (51, 197)], fill=color)
        suit(d, 94, 173, 15, kind, '#e8d7af')
        d.text((76, 197), label, font=font(17), fill=color)
    else:
        # Standard visible pip count, with two columns and centre additions.
        n = 1 if rank == 14 else rank
        if n <= 3:
            ys = {1: [133], 2: [82, 184], 3: [75, 133, 191]}[n]
            points = [(94, y) for y in ys]
        else:
            rows = 2 if n <= 5 else 3 if n <= 8 else 4
            ys = [75 + i*116/(rows-1) for i in range(rows)]
            points = [(x, y) for y in ys for x in (61, 127)]
            extra = n-len(points)
            points += [(94, y) for y in ({1: [133], 2: [103, 163]}.get(extra, []))]
        for x, y in points: suit(d, x, y, 15 if n > 1 else 28, kind, color)
    return image


def render(content, *, private=False, niuniu=False):
    if not 1 <= len(content) <= 5: raise ValueError('invalid poker image rows')
    width, row_height = (1100 if niuniu else 780), 340
    image = Image.new('RGB', (width, 92+len(content)*row_height+24), '#102f36')
    d = ImageDraw.Draw(image)
    title = '牛牛 · 五张揭晓' if niuniu else '炸金花 · 我的手牌' if private else '炸金花 · 摊牌结果'
    d.text((34, 20), title, font=font(30), fill='#f0dbb1')
    for i, player in enumerate(content):
        cards = player['cards']
        if niuniu:
            from app.modules.niuniu import CATEGORIES as BULL_CATEGORIES
            from app.modules.niuniu import strength as bull_strength
            category = BULL_CATEGORIES[bull_strength(cards)[0]]
        else:
            category = CATEGORIES[strength(cards)[0]]
        y = 92 + i*row_height
        name = str(player.get('name') or '成员')[:25]
        label = f'{name}  ·  {category}'
        if not private and player.get('award'): label += f'  ·  获得 {player["award"]} 积分'
        d.text((38, y), label, font=font(22), fill='#f5f7f4')
        for j, card in enumerate(cards):
            x = (36 if niuniu else 88)+j*208
            d.rounded_rectangle((x+3, y+43, x+191, y+310), radius=15, fill='#08212a')
            image.paste(face(card), (x, y+38))
    output = BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue()
