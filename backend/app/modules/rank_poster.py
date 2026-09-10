"""Daily/weekly ranking posters: hard-glass cards over a collage of Emby covers."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parent / "rank_assets"
CANVAS = (1080, 1920)
WHITELIST_GROUP_ID = "whitelist"
_DATA_FONT = Path(__file__).resolve().parents[2] / "data" / "fonts" / "NotoSansCJK-Bold.ttc"
FONT_PATHS = (
    str(ASSETS / "font" / "PingFang-Bold.ttf"),
    str(_DATA_FONT),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

TEXT = (237, 240, 247, 255)
TEXT2 = (171, 182, 205, 255)
MUTED = (135, 149, 173, 255)
ACCENT = (177, 193, 255, 255)
WL_NAME = (227, 217, 255, 255)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size, index=0)
            except OSError:
                continue
    return ImageFont.load_default()


def _fit(cover: Image.Image, size: tuple[int, int]) -> Image.Image:
    cover = cover.convert("RGB")
    tw, th = size
    scale = max(tw / cover.width, th / cover.height)
    resized = cover.resize(
        (max(1, int(cover.width * scale)), max(1, int(cover.height * scale))),
        Image.Resampling.LANCZOS)
    left = max(0, (resized.width - tw) // 2)
    top = max(0, (resized.height - th) // 2)
    return resized.crop((left, top, left + tw, top + th))


def _clip(text: str, limit: int) -> str:
    value = (text or "—").strip() or "—"
    return value if len(value) <= limit else value[: max(1, limit - 1)] + "…"


def _open_cover(blob: bytes | None, size: tuple[int, int]) -> Image.Image | None:
    if not blob:
        return None
    try:
        return _fit(Image.open(io.BytesIO(blob)), size)
    except OSError:
        return None


def _glass_bg(size: tuple[int, int] = CANVAS) -> Image.Image:
    width, height = size
    canvas = Image.new("RGB", (width, height), (11, 13, 19))
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (int(width * 0.2), -int(height * 0.22), int(width * 0.95), int(height * 0.34)),
        fill=(57, 68, 102, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=80))
    return Image.alpha_composite(canvas.convert("RGBA"), glow).convert("RGB")


def _poster_wall(covers: dict[str, bytes] | None, size: tuple[int, int] = CANVAS) -> Image.Image:
    """Collage of ranking posters, darkened so glass cards stay readable."""
    blobs = [b for b in (covers or {}).values() if b]
    if not blobs:
        return _glass_bg(size).convert("RGBA")
    width, height = size
    canvas = Image.new("RGB", (width, height), (11, 13, 19))
    tile_w, tile_h = 420, 620
    slots = (
        (-80, -40), (300, -80), (700, -30),
        (-60, 380), (340, 340), (720, 400),
        (-40, 820), (360, 860), (740, 800),
        (-40, 1280), (360, 1320), (740, 1260),
    )
    opened: list[Image.Image] = []
    for blob in blobs:
        cover = _open_cover(blob, (tile_w, tile_h))
        if cover is not None:
            opened.append(ImageEnhance.Color(cover).enhance(0.92))
    if not opened:
        return _glass_bg(size).convert("RGBA")
    for i, (x, y) in enumerate(slots):
        canvas.paste(opened[i % len(opened)], (x, y))
    canvas = canvas.filter(ImageFilter.GaussianBlur(radius=3.2)).convert("RGBA")
    veil = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(veil)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line([(0, y), (width, y)], fill=(11, 13, 19, int(88 + 100 * t)))
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (int(width * 0.18), -int(height * 0.2), int(width * 0.92), int(height * 0.3)),
        fill=(57, 68, 102, 50))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=70))
    return Image.alpha_composite(Image.alpha_composite(canvas, veil), glow)


def _frame(overlay: Image.Image) -> None:
    draw = ImageDraw.Draw(overlay)
    width, height = overlay.size
    draw.rounded_rectangle((36, 36, width - 36, height - 36),
                           radius=22, outline=(183, 203, 245, 46), width=1)
    draw.line([(80, 42), (width - 80, 42)], fill=(230, 239, 255, 48), width=1)


def _title_plate(overlay: Image.Image, title: str, when: str) -> None:
    """Title over the poster wall, with a shadow so it stays readable."""
    draw = ImageDraw.Draw(overlay)
    title_font = _font(54)
    date_font = _font(22)
    draw.text((82, 76), title, font=title_font, fill=(11, 13, 19, 200))
    draw.text((80, 72), title, font=title_font, fill=TEXT)
    if when:
        draw.text((82, 136), when, font=date_font, fill=(11, 13, 19, 180))
        draw.text((80, 132), when, font=date_font, fill=TEXT2)


def _watch_label(row: dict[str, Any] | None) -> str:
    nick = str((row or {}).get("tg_display_name") or "").strip()
    if nick:
        return nick
    handle = str((row or {}).get("tg_username") or "").strip().lstrip("@")
    if handle:
        return f"@{handle}"
    if str((row or {}).get("tg_user_id") or ""):
        return "Telegram用户"
    return "未绑定"


def _is_whitelist(row: dict[str, Any] | None) -> bool:
    return str((row or {}).get("group_id") or "") == WHITELIST_GROUP_ID


def _duration(row: dict[str, Any] | None) -> str:
    seconds = int((row or {}).get("seconds") or ((row or {}).get("hours") or 0) * 3600)
    hours, minutes = divmod(max(0, seconds) // 60, 60)
    return f"{hours}小时{minutes}分" if hours else f"{minutes}分"


def _circle(cover: Image.Image | None, size: int, ring: tuple[int, int, int]) -> Image.Image:
    if cover is None:
        return _letter_avatar("?", size, ring)
    fitted = _fit(cover.convert("RGB"), (size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size + 12, size + 12), (0, 0, 0, 0))
    ImageDraw.Draw(out).ellipse((0, 0, size + 11, size + 11), fill=ring + (255,))
    out.paste(fitted, (6, 6), mask)
    return out


def _letter_avatar(title: str, size: int, ring: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGB", (size, size), (36, 48, 72))
    draw = ImageDraw.Draw(canvas)
    raw = (title or "").lstrip("@").strip() or "?"
    glyph = _clip(raw, 1).upper()
    font = _font(max(24, size // 2))
    box = draw.textbbox((0, 0), glyph, font=font)
    draw.text(((size - (box[2] - box[0])) / 2, (size - (box[3] - box[1])) / 2 - 4),
              glyph, fill=(240, 246, 255), font=font)
    return _circle(canvas, size, ring)


def _draw_shield(overlay: Image.Image, xy: tuple[int, int], scale: float = 1.15) -> None:
    shield = Image.new("RGBA", (int(34 * scale), int(38 * scale)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shield)
    s = scale
    draw.polygon(
        [(16 * s, 1.5 * s), (29 * s, 7 * s), (29 * s, 19.5 * s),
         (16 * s, 34.5 * s), (3 * s, 19.5 * s), (3 * s, 7 * s)],
        fill=(160, 169, 238, 48), outline=(210, 216, 255, 230))
    draw.polygon(
        [(16 * s, 5 * s), (25 * s, 9.2 * s), (25 * s, 18 * s),
         (16 * s, 29 * s), (7 * s, 18 * s), (7 * s, 9.2 * s)],
        fill=(185, 160, 250, 36), outline=(149, 189, 255, 210))
    draw.polygon(
        [(16 * s, 9 * s), (22 * s, 16 * s), (16 * s, 25 * s), (10 * s, 16 * s)],
        fill=(214, 206, 255, 110), outline=(236, 231, 255, 240))
    overlay.alpha_composite(shield, dest=(int(xy[0]), int(xy[1])))


def _whitelist_badge(overlay: Image.Image, xy: tuple[int, int]) -> None:
    draw = ImageDraw.Draw(overlay)
    x, y = xy
    draw.rounded_rectangle((x, y, x + 176, y + 36), radius=6,
                           fill=(210, 221, 255, 28), outline=(194, 185, 250, 90), width=1)
    _draw_shield(overlay, (x + 8, y + 4), 1.05)
    draw.text((x + 44, y + 8), "白名单", font=_font(16), fill=WL_NAME)
    draw.text((x + 114, y + 10), "专属", font=_font(13), fill=(184, 167, 223, 255))


def _glass_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                whitelist: bool = False) -> None:
    outline = (194, 185, 250, 95) if whitelist else (172, 191, 239, 50)
    draw.rounded_rectangle(box, radius=12, fill=(22, 29, 43, 204), outline=outline, width=1)
    x0, y0, x1, _y1 = box
    draw.line([(x0 + 14, y0 + 1), (x1 - 14, y0 + 1)], fill=(204, 217, 252, 70), width=1)


def _rounded_cover(blob: bytes | None, size: tuple[int, int], radius: int = 14,
                   title: str = "") -> Image.Image:
    cover = _open_cover(blob, size)
    tile = (cover.convert("RGBA") if cover is not None
            else Image.new("RGBA", size, (28, 36, 52, 220)))
    if cover is None:
        draw = ImageDraw.Draw(tile)
        draw.text((10, size[1] // 2 - 12), _clip(title, 6), font=_font(18), fill=TEXT)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1),
                                           radius=radius, fill=255)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(tile, (0, 0), mask)
    return out


def _jpeg(canvas: Image.Image) -> bytes:
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()


def render_watch_poster(rows: list[dict[str, Any]], *, weekly: bool = False,
                        avatars: dict[str, bytes] | None = None, when: str = "",
                        covers: dict[str, bytes] | None = None) -> bytes:
    avatars = avatars or {}
    canvas = _poster_wall(covers)
    overlay = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    _frame(overlay)
    _title_plate(overlay, "观影周榜" if weekly else "观影日榜", when)
    name_font = _font(32)
    meta_font = _font(24)
    y = 214
    height = 148
    for i in range(10):
        row = rows[i] if i < len(rows) else None
        whitelist = _is_whitelist(row)
        label = _watch_label(row)
        box = (64, y, 1016, y + height)
        _glass_card(draw, box, whitelist=whitelist)
        size = 96
        ax, ay = 88, y + 26
        ring = (206, 190, 255) if whitelist else ((232, 197, 92) if i == 0 else (177, 193, 255))
        blob = avatars.get(str((row or {}).get("tg_user_id") or "")) if row else None
        face = _open_cover(blob, (size, size))
        badge = _circle(face, size, ring) if face is not None else _letter_avatar(label, size, ring)
        overlay.alpha_composite(badge, dest=(ax - 6, ay - 6))
        nx = ax + size + 28
        rank = f"{i + 1:02d}"
        draw.text((nx, ay + 4), rank, font=_font(22), fill=ACCENT)
        draw.text((nx + 56, ay), _clip(label, 14), font=name_font,
                  fill=WL_NAME if whitelist else TEXT)
        draw.text((nx + 56, ay + 46), _duration(row) if row else "—", font=meta_font, fill=ACCENT)
        if whitelist:
            _whitelist_badge(overlay, (760, ay + 36))
        y += height + 14
    return _jpeg(Image.alpha_composite(canvas, overlay))


def _cover_grid(overlay: Image.Image, items: list[dict[str, Any]], covers: dict[str, bytes],
                origin_y: int) -> None:
    draw = ImageDraw.Draw(overlay)
    xs = (64, 248, 432, 616, 800)
    tile = (168, 248)
    for i, item in enumerate(items[:10]):
        col, row = i % 5, i // 5
        x, y = xs[col], origin_y + row * 330
        overlay.alpha_composite(
            _rounded_cover(covers.get(str(item.get("item_id") or "")), tile,
                           title=str(item.get("title") or "—")), (x, y))
        draw.text((x, y + 256), _clip(str(item.get("title") or "—"), 8),
                  font=_font(22), fill=TEXT)
        draw.text((x, y + 288), f"{int(item.get('plays') or 0)} 次播放",
                  font=_font(18), fill=TEXT2)


def render_rank_poster(movies: list[dict[str, Any]], shows: list[dict[str, Any]], *,
                       weekly: bool = False, covers: dict[str, bytes] | None = None,
                       when: str = "") -> bytes:
    covers = covers or {}
    canvas = _poster_wall(covers)
    overlay = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    _frame(overlay)
    _title_plate(overlay, "播放周榜" if weekly else "播放日榜", when)
    draw.text((64, 214), "▎电影", font=_font(32), fill=ACCENT)
    _cover_grid(overlay, movies, covers, 262)
    draw.text((64, 980), "▎电视剧", font=_font(32), fill=ACCENT)
    _cover_grid(overlay, shows, covers, 1028)
    return _jpeg(Image.alpha_composite(canvas, overlay))
