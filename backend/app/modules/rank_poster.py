"""Daily/weekly ranking poster for the scheduled Telegram bulletin."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1280, 920
MARGIN = 48
TILE_W, TILE_H = 200, 280
GAP = 20
_DATA_FONT = Path(__file__).resolve().parents[2] / "data" / "fonts" / "NotoSansCJK-Bold.ttc"
FONT_PATHS = (
    str(_DATA_FONT),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
RANK_FILL = (
    (232, 197, 92),
    (196, 206, 220),
    (205, 140, 92),
    (88, 108, 148),
    (88, 108, 148),
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


def _placeholder(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                 title: str, font: ImageFont.ImageFont) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=16, fill=(28, 36, 52), outline=(90, 108, 148), width=2)
    draw.text((x0 + 16, y0 + (y1 - y0) // 2 - 12), _clip(title, 8),
              fill=(210, 220, 235), font=font)


def _badge(draw: ImageDraw.ImageDraw, xy: tuple[int, int], rank: int,
           font: ImageFont.ImageFont) -> None:
    x, y = xy
    fill = RANK_FILL[rank - 1] if 1 <= rank <= 5 else RANK_FILL[-1]
    draw.ellipse((x, y, x + 36, y + 36), fill=fill)
    label = str(rank)
    box = draw.textbbox((0, 0), label, font=font)
    draw.text((x + 18 - (box[2] - box[0]) / 2, y + 6), label, fill=(16, 18, 24), font=font)


def render_rank_poster(movies: list[dict[str, Any]], shows: list[dict[str, Any]], *,
                       weekly: bool = False, covers: dict[str, bytes] | None = None,
                       when: str = "") -> bytes:
    """Dark-glass 5+5 poster board. Missing covers become named tiles."""
    covers = covers or {}
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (8, 12, 22))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, 96), fill=(14, 22, 38))
    draw.rectangle((0, HEIGHT - 36, WIDTH, HEIGHT), fill=(14, 22, 38))
    title_font = _font(42)
    section_font = _font(26)
    small_font = _font(18)
    rank_font = _font(18)
    heading = "播放周榜" if weekly else "播放日榜"
    draw.text((MARGIN, 28), heading, fill=(240, 246, 255), font=title_font)
    if when:
        stamp_box = draw.textbbox((0, 0), when, font=small_font)
        draw.text((WIDTH - MARGIN - (stamp_box[2] - stamp_box[0]), 42),
                  when, fill=(150, 168, 196), font=small_font)
    rows = (
        ("▎电影", movies[:5], 108),
        ("▎电视剧", shows[:5], 500),
    )
    for label, items, top in rows:
        draw.text((MARGIN, top), label, fill=(186, 206, 236), font=section_font)
        for i in range(5):
            x = MARGIN + i * (TILE_W + GAP)
            y = top + 42
            box = (x, y, x + TILE_W, y + TILE_H)
            item = items[i] if i < len(items) else None
            if not item:
                draw.rounded_rectangle(box, radius=16, fill=(16, 22, 34),
                                       outline=(32, 42, 60), width=1)
                continue
            blob = covers.get(str(item.get("item_id") or ""))
            pasted = False
            if blob:
                try:
                    cover = _fit(Image.open(io.BytesIO(blob)), (TILE_W, TILE_H))
                    mask = Image.new("L", (TILE_W, TILE_H), 0)
                    ImageDraw.Draw(mask).rounded_rectangle(
                        (0, 0, TILE_W, TILE_H), radius=16, fill=255)
                    canvas.paste(cover, (x, y), mask)
                    pasted = True
                except OSError:
                    pasted = False
            if not pasted:
                _placeholder(draw, box, str(item.get("title") or "—"), small_font)
            _badge(draw, (x + 10, y + 10), i + 1, rank_font)
            title = _clip(str(item.get("title") or "—"), 10)
            meta = f"{int(item.get('plays') or 0)} 次"
            draw.text((x, y + TILE_H + 10), title, fill=(230, 236, 246), font=small_font)
            draw.text((x, y + TILE_H + 34), meta, fill=(150, 168, 196), font=small_font)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88)
    return out.getvalue()
