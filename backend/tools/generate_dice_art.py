"""Regenerate bundled art locally with Pillow and NotoSansCJK-Bold; not run on servers."""

import math
import random
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parents[1] / "app/assets/dice"
OUT.mkdir(parents=True, exist_ok=True)
S = 2
W = 1000
H = 880
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"


def font(n):
    return ImageFont.truetype(FONT, n * S)


def box(b):
    return tuple(int(v * S) for v in b)


def text(d, xy, t, n, c, anchor=None):
    d.text((xy[0] * S, xy[1] * S), t, font=font(n), fill=c, anchor=anchor)


def ellipse(d, b, fill, outline=None, width=1):
    d.ellipse(box(b), fill=fill, outline=outline, width=width * S)


def line(d, pts, fill, width):
    d.line([(int(x * S), int(y * S)) for x, y in pts], fill=fill, width=width * S)


@lru_cache(maxsize=1)
def cup():
    im = Image.new("RGBA", (660 * S, 530 * S))
    d = ImageDraw.Draw(im)
    # solid tapered polished shell
    polygon = [(160, 115), (500, 115), (580, 424), (80, 424)]
    mask = Image.new("L", im.size)
    md = ImageDraw.Draw(mask)
    md.polygon([(x * S, y * S) for x, y in polygon], fill=255)
    md.ellipse(box((80, 375, 580, 473)), fill=255)
    grad = Image.new("RGBA", im.size)
    gd = ImageDraw.Draw(grad)
    for x in range(660 * S):
        a = x / S
        glow = math.exp(-(((a - 235) / 85) ** 2))
        edge = math.exp(-(((a - 495) / 38) ** 2))
        col = (
            int(38 + 60 * glow + 17 * edge),
            int(26 + 35 * glow + 8 * edge),
            int(34 + 40 * glow + 12 * edge),
            255,
        )
        gd.line((x, 0, x, 530 * S), fill=col)
    im.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(im)
    for a, b in [((156, 99, 504, 158), 5), ((83, 382, 577, 477), 7)]:
        ellipse(d, a, None, "#d5ac68", b)
    ellipse(d, (158, 94, 502, 153), "#3d3037", "#f2d297", 4)
    ellipse(d, (177, 99, 483, 139), "#211f2c", "#856747", 2)
    line(d, [(170, 162), (117, 380)], "#bb9460", 3)
    line(d, [(489, 162), (544, 380)], "#8a6946", 3)
    # gold geometric crest
    pts = [(330, 208), (388, 259), (330, 319), (272, 259), (330, 208)]
    line(d, pts, "#caa46b", 3)
    text(d, (330, 260), "骰", 43, "#efd4a2", "mm")
    text(d, (330, 350), "D I C E   C L U B", 13, "#c6a778", "mm")
    ellipse(d, (99, 390, 561, 456), None, "#e0bd7b", 3)
    return im


def die(im, cx, cy, value, size=110):
    d = ImageDraw.Draw(im)
    x = cx - size / 2
    y = cy - size / 2
    sh = Image.new("RGBA", im.size)
    sd = ImageDraw.Draw(sh)
    ellipse(sd, (x - 5, y + 40, x + size + 20, y + size + 37), (0, 0, 0, 155))
    im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(12 * S)))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(
        box((x + 6, y + 15, x + size + 6, y + size + 19)),
        radius=20 * S,
        fill="#a89b8a",
        outline="#e4d1ae",
        width=2 * S,
    )
    d.rounded_rectangle(
        box((x, y, x + size, y + size)),
        radius=20 * S,
        fill="#fff3da",
        outline="#e1c38b",
        width=2 * S,
    )
    d.rounded_rectangle(
        box((x + 6, y + 5, x + size - 6, y + size - 6)),
        radius=15 * S,
        outline="#ffffff",
        width=2 * S,
    )
    pos = {
        1: [(0, 0)],
        2: [(-1, -1), (1, 1)],
        3: [(-1, -1), (0, 0), (1, 1)],
        4: [(-1, -1), (1, -1), (-1, 1), (1, 1)],
        5: [(-1, -1), (1, -1), (0, 0), (-1, 1), (1, 1)],
        6: [(-1, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (1, 1)],
    }[value]
    for a, b in pos:
        px = cx + a * size * 0.26
        py = cy + b * size * 0.26
        r = 8
        ellipse(d, (px - r, py - r, px + r, py + r), "#b1303d" if value in (1, 4) else "#273249")
        ellipse(
            d,
            (px - r + 2, py - r + 2, px + r - 3, py + r - 3),
            "#d45056" if value in (1, 4) else "#3e4b63",
        )


def render(values=None):
    opened = values is not None
    im = Image.new("RGBA", (W * S, H * S))
    d = ImageDraw.Draw(im)
    for y in range(H * S):
        q = y / (H * S)
        d.line((0, y, W * S, y), fill=(int(10 + 7 * q), int(15 + 6 * q), int(29 + 9 * q), 255))
    glow = Image.new("RGBA", im.size)
    gd = ImageDraw.Draw(glow)
    ellipse(gd, (145, 200, 855, 760), (69, 104, 130, 75))
    im.alpha_composite(glow.filter(ImageFilter.GaussianBlur(95 * S)))
    d = ImageDraw.Draw(im)
    random.seed(9)
    for _ in range(75):
        x = random.randint(35, 965)
        y = random.randint(160, 785)
        r = random.choice([1, 1, 2])
        ellipse(d, (x, y, x + r, y + r), "#756546")
    d.rounded_rectangle(box((24, 24, 976, 856)), radius=28 * S, outline="#5d503e", width=S)
    line(d, [(62, 133), (938, 133)], "#514b42", 1)
    text(d, (65, 57), "D I C E   /   大 小 对 决", 20, "#ddbd83")
    text(d, (936, 64), "02  ·  已揭晓" if opened else "01  ·  静候揭晓", 17, "#e7d4ae", "ra")
    # broad felt stage
    ellipse(d, (145, 539, 855, 775), "#060b13")
    ellipse(d, (140, 510, 860, 750), "#72593b", "#e4bc76", 3)
    ellipse(d, (153, 510, 847, 728), "#1a2935", "#a2804e", 3)
    ellipse(d, (177, 522, 823, 702), "#213d47", "#39545a", 2)
    ellipse(d, (201, 536, 799, 684), None, "#557071", 1)
    c = cup()
    if opened:
        c = c.rotate(-23, resample=Image.Resampling.BICUBIC, expand=True)
        im.alpha_composite(c, (int(300 * S), int(84 * S)))
        die(im, 340, 604, values[0], 103)
        die(im, 479, 580, values[1], 103)
        die(im, 606, 625, values[2], 103)
    else:
        im.alpha_composite(c, (170 * S, 184 * S))
    d = ImageDraw.Draw(im)
    text(
        d,
        (500, 781),
        (
            "  +  ".join(map(str, values))
            + f"    =    {sum(values)} 点  ·  "
            + ("豹子" if len(set(values)) == 1 else "大" if sum(values) >= 11 else "小")
        )
        if opened
        else "盅盖闭合  ·  静候揭晓",
        27,
        "#f0d49b",
        "mm",
    )
    im = im.convert("RGB").resize((W, H), Image.Resampling.LANCZOS)
    im.save(
        OUT / (("".join(map(str, values)) if opened else "covered") + ".jpg"),
        quality=88,
        optimize=True,
    )
    return im


if __name__ == "__main__":
    from itertools import product

    render()
    for values in product(range(1, 7), repeat=3):
        render(values)
