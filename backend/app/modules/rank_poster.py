"""Daily/weekly ranking poster. Layout copied from EmbyBoss ranks_draw."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ASSETS = Path(__file__).resolve().parent / "rank_assets"
MOVIE_SIZE = (144, 210)
SHOW_SIZE = (144, 210)
MOVIE_XY = (601, 162)
MOVIE_STEP = 230
SHOW_XY = (770, 985)
SHOW_STEP = 232
_DATA_FONT = Path(__file__).resolve().parents[2] / "data" / "fonts" / "NotoSansCJK-Bold.ttc"
FONT_PATHS = (
    str(ASSETS / "font" / "PingFang-Bold.ttf"),
    str(_DATA_FONT),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


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


def _paste_or_name(canvas: Image.Image, draw: ImageDraw.ImageDraw, xy: tuple[int, int],
                   size: tuple[int, int], cover: Image.Image | None, title: str,
                   font: ImageFont.ImageFont) -> None:
    if cover is not None:
        canvas.paste(cover, xy)
        return
    x, y = xy
    draw.rectangle((x, y, x + size[0], y + size[1]), fill=(28, 36, 52))
    draw.text((x + 8, y + size[1] // 2 - 10), _clip(title, 7), fill=(210, 220, 235), font=font)


def _glass_bg(size: tuple[int, int]) -> Image.Image:
    """Dark glass plate. The old random JPEGs stretched badly and looked cheap."""
    width, height = size
    ramp = Image.new("RGB", (1, height))
    pix = ramp.load()
    for y in range(height):
        t = y / max(1, height - 1)
        pix[0, y] = (int(6 + 16 * t), int(10 + 20 * t), int(18 + 34 * t))
    bg = ramp.resize((width, height), Image.Resampling.BILINEAR).convert("RGBA")
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (-int(width * 0.15), -int(height * 0.25), int(width * 1.15), int(height * 0.55)),
        fill=(90, 120, 170, 38))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=max(12, width // 40)))
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, 0, width, int(height * 0.16)), fill=(255, 255, 255, 16))
    inset = max(16, width // 48)
    draw.rounded_rectangle((inset, inset, width - inset, height - inset),
                           radius=max(18, width // 40), outline=(186, 206, 236, 46), width=2)
    return Image.alpha_composite(Image.alpha_composite(bg, glow), overlay).convert("RGB")


def _board_from_assets(weekly: bool) -> Image.Image | None:
    mask_path = ASSETS / ("week_ranks_mask.png" if weekly else "day_ranks_mask.png")
    if not mask_path.is_file():
        return None
    mask = Image.open(mask_path).convert("RGBA")
    bg = _glass_bg(mask.size).convert("RGBA")
    bg.paste(mask, (0, 0), mask)
    return bg.convert("RGB")


def _fallback_board(movies: list[dict[str, Any]], shows: list[dict[str, Any]],
                    covers: dict[str, bytes], when: str, weekly: bool) -> bytes:
    width, height = 1280, 920
    canvas = Image.new("RGB", (width, height), (8, 12, 22))
    draw = ImageDraw.Draw(canvas)
    title_font = _font(42)
    section_font = _font(26)
    small_font = _font(18)
    heading = "播放周榜" if weekly else "播放日榜"
    draw.text((48, 28), heading, fill=(240, 246, 255), font=title_font)
    if when:
        draw.text((width - 280, 42), when, fill=(150, 168, 196), font=small_font)
    tile_w, tile_h, gap = 200, 280, 20
    rows = (("▎电影", movies[:5], 108), ("▎电视剧", shows[:5], 500))
    for label, items, top in rows:
        draw.text((48, top), label, fill=(186, 206, 236), font=section_font)
        for i in range(5):
            x = 48 + i * (tile_w + gap)
            y = top + 42
            item = items[i] if i < len(items) else None
            if not item:
                draw.rounded_rectangle((x, y, x + tile_w, y + tile_h), radius=16,
                                       fill=(16, 22, 34), outline=(32, 42, 60), width=1)
                continue
            cover = _open_cover(covers.get(str(item.get("item_id") or "")), (tile_w, tile_h))
            _paste_or_name(canvas, draw, (x, y), (tile_w, tile_h), cover,
                           str(item.get("title") or "—"), small_font)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88)
    return out.getvalue()


def _circle(cover: Image.Image, size: int, ring: tuple[int, int, int]) -> Image.Image:
    fitted = _fit(cover.convert("RGB"), (size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size + 12, size + 12), (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(out)
    ring_draw.ellipse((0, 0, size + 11, size + 11), fill=ring + (255,))
    out.paste(fitted, (6, 6), mask)
    return out


def _letter_avatar(title: str, size: int, ring: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGB", (size, size), (36, 48, 72))
    draw = ImageDraw.Draw(canvas)
    glyph = _clip(title, 1)
    font = _font(max(24, size // 2))
    box = draw.textbbox((0, 0), glyph, font=font)
    draw.text(((size - (box[2] - box[0])) / 2, (size - (box[3] - box[1])) / 2 - 4),
              glyph, fill=(240, 246, 255), font=font)
    return _circle(canvas, size, ring)


def render_watch_poster(rows: list[dict[str, Any]], *, weekly: bool = False,
                        avatars: dict[str, bytes] | None = None, when: str = "") -> bytes:
    """Podium card for the top three watchers; caption still lists the page."""
    avatars = avatars or {}
    width, height = 1280, 720
    canvas = _glass_bg((width, height))
    draw = ImageDraw.Draw(canvas)
    title_font = _font(44)
    name_font = _font(26)
    meta_font = _font(20)
    heading = "观影周榜" if weekly else "观影日榜"
    draw.text((48, 32), heading, fill=(240, 246, 255), font=title_font)
    if when:
        box = draw.textbbox((0, 0), when, font=meta_font)
        draw.text((width - 48 - (box[2] - box[0]), 44), when, fill=(150, 168, 196), font=meta_font)
    podium = (
        (1, (width // 2 - 110, 150), 220, (232, 197, 92), "🥇"),
        (2, (180, 250), 180, (196, 206, 220), "🥈"),
        (3, (width - 180 - 180, 270), 170, (205, 140, 92), "🥉"),
    )
    for rank, (x, y), size, ring, medal in podium:
        row = rows[rank - 1] if rank <= len(rows) else None
        title = str((row or {}).get("username") or "—")
        blob = avatars.get(str((row or {}).get("tg_user_id") or "")) if row else None
        face = _open_cover(blob, (size, size))
        badge = _circle(face, size, ring) if face is not None else _letter_avatar(title, size, ring)
        canvas.paste(badge, (x, y), badge)
        label = _clip(title, 8)
        seconds = int((row or {}).get("seconds") or ((row or {}).get("hours") or 0) * 3600)
        hours, minutes = divmod(max(0, seconds) // 60, 60)
        time_text = f"{hours}小时{minutes}分" if hours else f"{minutes}分"
        draw.text((x, y + size + 22), f"{medal} {label}", fill=(240, 246, 255), font=name_font)
        draw.text((x, y + size + 58), time_text if row else "—", fill=(150, 168, 196), font=meta_font)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88)
    return out.getvalue()


def render_rank_poster(movies: list[dict[str, Any]], shows: list[dict[str, Any]], *,
                       weekly: bool = False, covers: dict[str, bytes] | None = None,
                       when: str = "") -> bytes:
    """EmbyBoss vertical board: movies down the left column, shows up the right."""
    covers = covers or {}
    canvas = _board_from_assets(weekly)
    if canvas is None:
        return _fallback_board(movies, shows, covers, when, weekly)
    draw = ImageDraw.Draw(canvas)
    name_font = _font(18)
    for i, item in enumerate(movies[:5]):
        xy = (MOVIE_XY[0], MOVIE_XY[1] + MOVIE_STEP * i)
        cover = _open_cover(covers.get(str(item.get("item_id") or "")), MOVIE_SIZE)
        _paste_or_name(canvas, draw, xy, MOVIE_SIZE, cover, str(item.get("title") or "—"), name_font)
    for i, item in enumerate(shows[:5]):
        xy = (SHOW_XY[0], SHOW_XY[1] - SHOW_STEP * i)
        cover = _open_cover(covers.get(str(item.get("item_id") or "")), SHOW_SIZE)
        _paste_or_name(canvas, draw, xy, SHOW_SIZE, cover, str(item.get("title") or "—"), name_font)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88)
    return out.getvalue()
