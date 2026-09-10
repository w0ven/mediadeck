"""Daily/weekly ranking poster. Layout copied from EmbyBoss ranks_draw."""
from __future__ import annotations

import io
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

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


def _board_from_assets(weekly: bool) -> Image.Image | None:
    mask_path = ASSETS / ("week_ranks_mask.png" if weekly else "day_ranks_mask.png")
    bg_dir = ASSETS / "bg"
    if not mask_path.is_file() or not bg_dir.is_dir():
        return None
    bgs = [p for p in bg_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
    if not bgs:
        return None
    mask = Image.open(mask_path).convert("RGBA")
    bg = Image.open(random.choice(bgs)).convert("RGBA").resize(mask.size, Image.Resampling.LANCZOS)
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
