"""COLA EMBY posters shared by title charts, member charts and personal reports."""
from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

ASSETS = Path(__file__).resolve().parent / 'rank_assets'
CANVAS = (1200, 2400)
BRAND = 'COLA EMBY'
WHITELIST_GROUP_ID = 'whitelist'
FONT_PATHS = (
    ASSETS / 'font' / 'PingFang-Bold.ttf',
    Path(__file__).resolve().parents[2] / 'data/fonts/NotoSansCJK-Bold.ttc',
    Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'),
    Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
    Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
)
BG = (8, 22, 27)
GOLD = (230, 205, 147)
TEXT = (238, 238, 226)
MUTED = (151, 166, 163)
UP = (211, 129, 108)
DOWN = (130, 169, 148)
_FONT_CACHE = threading.local()


def _font(size: int) -> Any:
    # FreeType faces are kept per rendering thread, not shared between workers.
    if not hasattr(_FONT_CACHE, 'fonts'):
        _FONT_CACHE.fonts = {}
    if size not in _FONT_CACHE.fonts:
        for path in FONT_PATHS:
            if path.is_file():
                try:
                    _FONT_CACHE.fonts[size] = ImageFont.truetype(str(path), size=size)
                    break
                except OSError:
                    continue
        else:
            _FONT_CACHE.fonts[size] = ImageFont.load_default()
    return _FONT_CACHE.fonts[size]


def _line(value: Any) -> str:
    return ' '.join(str(value or '').split()).strip()


def _visible_label(value: Any) -> str:
    label = _line(value).strip(' \u200b\u200c\u200d\ufeff\u3164\u2800\u115f\u1160')
    return label if label and _font(32).getmask(label).getbbox() else ''


def _clip(value: str, width: int, size: int) -> str:
    value = _line(value) or '—'
    if _font(size).getlength(value) <= width:
        return value
    while value and _font(size).getlength(value + '…') > width:
        value = value[:-1]
    return value + '…'


def _wrap(value: str, width: int, size: int, limit: int = 4) -> list[str]:
    lines, current = [], ''
    for char in _line(value) or '—':
        if current and _font(size).getlength(current + char) > width:
            lines.append(current)
            current = char
        else:
            current += char
    if current:
        lines.append(current)
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = _clip(lines[-1] + '…', width, size)
    return lines


def _open(blob: bytes | None, size: tuple[int, int], *, center=(0.5, 0.35)) -> Image.Image | None:
    if not blob:
        return None
    try:
        with Image.open(io.BytesIO(blob)) as image:
            image.load()
            return ImageOps.fit(image.convert('RGB'), size, Image.Resampling.LANCZOS,
                                centering=center).convert('RGBA')
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _watch_label(row: dict[str, Any] | None) -> str:
    row = row or {}
    if _visible_label(row.get('tg_display_name')):
        return _visible_label(row['tg_display_name'])
    handle = _visible_label(row.get('tg_username')).lstrip('@')
    if handle:
        return '@' + handle
    return 'Telegram用户' if str(row.get('tg_user_id') or '') else '未绑定'


def _is_whitelist(row: dict[str, Any] | None) -> bool:
    return str((row or {}).get('group_id') or '') == WHITELIST_GROUP_ID


def _duration(row: dict[str, Any]) -> str:
    seconds = int(row.get('seconds') or (row.get('hours') or 0) * 3600)
    if row.get('incomplete') and seconds <= 0:
        return '记录不完整'
    hours, minutes = divmod(max(0, seconds) // 60, 60)
    label = f'{hours}小时{minutes}分' if hours else f'{minutes}分钟'
    return ('≥ ' if row.get('incomplete') else '') + label


def _title_count(row: dict[str, Any]) -> str:
    plays = max(0, int(row.get('plays') or 0))
    viewers = row.get('viewers')
    return f'{int(viewers)}人 · {plays}次' if viewers is not None and int(viewers) > 0 else f'{plays}次播放'


async def fetch_rank_images(fetch: Callable | None, rows: list[dict[str, Any]], *,
                            limit: int = 20, timeout: float = 12.0) -> dict[str, bytes]:
    """Bound both image concurrency and total wait; optional art may be missing."""
    if not callable(fetch):
        return {}
    ids = list(dict.fromkeys(str(row.get('item_id') or '') for row in rows))
    ids = [item for item in ids if item][:limit]
    semaphore = asyncio.Semaphore(4)
    found: dict[str, bytes] = {}

    async def one(item: str) -> None:
        async with semaphore:
            try:
                blob = await fetch(item)
                if blob:
                    found[item] = blob
            except Exception:  # noqa: BLE001 - optional image; no credential-bearing errors
                return

    tasks = [asyncio.create_task(one(item)) for item in ids]
    if tasks:
        try:
            await asyncio.wait(tasks, timeout=timeout)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return {item: found[item] for item in ids if item in found}


class _Poster:
    def __init__(self, size: tuple[int, int] = CANVAS):
        self.image = Image.new('RGBA', size, (*BG, 255))
        self.width, self.height = size
        self.draw = ImageDraw.Draw(self.image)

    def text(self, x: int, y: int, value: Any, size: int = 30,
             color: tuple = TEXT, anchor: str = 'lt') -> None:
        self.draw.text((x, y), str(value), font=_font(size), fill=color, anchor=anchor)

    def rule(self, x: int, y: int, end: int, alpha: int = 80) -> None:
        self.draw.line((x, y, end, y), fill=(151, 153, 119, alpha), width=1)

    def fade(self, x: int, y: int, width: int, height: int, start: int, end: int) -> None:
        mask = Image.new('RGBA', (width, height))
        draw = ImageDraw.Draw(mask)
        for line in range(height):
            opacity = round(start + (end - start) * line / max(1, height - 1))
            draw.line((0, line, width, line), fill=(*BG, opacity))
        self.image.alpha_composite(mask, (x, y))

    def header(self, title: str, subtitle: str, when: str, heroes: list[bytes]) -> None:
        tile_width = self.width if len(heroes) == 1 else self.width // 2
        pictures = [_open(blob, (tile_width, 575)) for blob in heroes[:2]]
        for index, picture in enumerate(pictures):
            if picture is not None:
                picture = ImageEnhance.Color(picture).enhance(0.82)
                self.image.alpha_composite(picture, (index * tile_width, 0))
        self.fade(0, 0, self.width, 250, 60, 25)
        self.fade(0, 140, self.width, 440, 0, 255)
        self.draw.rounded_rectangle((36, 34, 300, 106), radius=10, fill=(*BG, 185))
        self.draw.rectangle((54, 54, 60, 84), fill=GOLD)
        self.text(77, 52, BRAND, 34, GOLD)
        self.text(600, 330, title, 75, TEXT, 'mt')
        self.text(600, 427, subtitle, 27, GOLD, 'mt')
        if when:
            self.text(600, 478, when, 26, TEXT, 'mt')

    def footer(self, note: str = '') -> None:
        top = self.height - 325
        path = ASSETS / 'cola-landscape.jpg'
        try:
            picture = _open(path.read_bytes(), (self.width, 325))
        except OSError:
            picture = None
        if picture is not None:
            picture = ImageEnhance.Color(picture).enhance(0.55)
            picture = ImageEnhance.Brightness(picture).enhance(0.50)
            self.image.alpha_composite(picture, (0, top))
            self.fade(0, top, self.width, 190, 255, 65)
            self.fade(0, top + 190, self.width, 135, 65, 205)
        self.text(600, self.height - 183, '让每一部好片，都被看见。', 40, GOLD, 'mt')
        self.text(600, self.height - 111, BRAND + '  ·  ENJOY THE SHOW', 24, MUTED, 'mt')
        if note:
            self.text(600, self.height - 48, note, 21, MUTED, 'mt')

    def cover(self, blob: bytes | None, box: tuple[int, int, int, int]) -> None:
        x, y, width, height = box
        picture = _open(blob, (width, height))
        if picture is None:
            picture = Image.new('RGBA', (width, height), (30, 45, 48, 255))
            mark = ImageDraw.Draw(picture)
            mark.line((width // 3, height // 2, width * 2 // 3, height // 2), fill=GOLD, width=2)
        self.draw.rectangle((x + 3, y + 5, x + width + 3, y + height + 5), fill=(0, 0, 0, 90))
        self.image.alpha_composite(picture, (x, y))
        self.draw.rectangle((x, y, x + width - 1, y + height - 1), outline=(184, 168, 121, 95), width=1)

    def crown(self, x: int, y: int) -> None:
        self.draw.polygon([(x, y + 6), (x + 9, y + 20), (x + 18, y), (x + 27, y + 20),
                           (x + 37, y + 6), (x + 32, y + 31), (x + 5, y + 31)], fill=GOLD)

    def avatar(self, blob: bytes | None, x: int, y: int, size: int, label: str) -> None:
        face = _open(blob, (size, size), center=(0.5, 0.5))
        if face is None:
            face = Image.new('RGBA', (size, size), (34, 51, 53, 255))
            draw = ImageDraw.Draw(face)
            glyph = next((char for char in _line(label).lstrip('@')
                          if _font(32).getmask(char).getbbox()), '?')
            draw.text((size // 2, size // 2), glyph, font=_font(max(28, size // 2)), fill=GOLD, anchor='mm')
        mask = Image.new('L', (size, size))
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        self.draw.ellipse((x - 3, y - 3, x + size + 2, y + size + 2), fill=GOLD)
        self.image.paste(face, (x, y), mask)

    def badge(self, x: int, y: int) -> None:
        self.draw.rounded_rectangle((x, y, x + 158, y + 34), radius=5,
                                    fill=(100, 91, 53, 65), outline=(196, 178, 120, 130), width=1)
        self.text(x + 13, y + 5, '白名单 · 专属', 21, GOLD)

    def movement(self, x: int, y: int, row: dict[str, Any]) -> None:
        if not row.get('comparison_available'):
            self.text(x, y, '—', 24, MUTED, 'rt')
        elif row.get('previous_rank') is None:
            self.text(x, y, '新上榜', 24, GOLD, 'rt')
        else:
            delta = int(row.get('rank_delta') or 0)
            value = '持平' if not delta else ('↑ ' if delta > 0 else '↓ ') + str(abs(delta))
            self.text(x, y, value, 25, MUTED if not delta else UP if delta > 0 else DOWN, 'rt')

    def jpeg(self) -> bytes:
        output = io.BytesIO()
        self.image.convert('RGB').save(output, format='JPEG', quality=93, subsampling=0)
        return output.getvalue()


def _heroes(rows: list[dict[str, Any]], covers: dict[str, bytes], backdrops: dict[str, bytes]) -> list[bytes]:
    result = []
    for row in rows:
        key = str(row.get('item_id') or '')
        blob = backdrops.get(key) or covers.get(key)
        if blob:
            result.append(blob)
        if len(result) >= 2:
            break
    if not result:
        result = list(backdrops.values())[:2] or list(covers.values())[:2]
    return result


def _period_title(kind: str, weekly: bool, days: int | None) -> str:
    period = days if days is not None else 7 if weekly else 1
    return ('每日' if period == 1 else '每周' if period == 7 else f'近{period}天') + kind


def render_rank_poster(movies: list[dict[str, Any]], shows: list[dict[str, Any]], *,
                       weekly: bool = False, covers: dict[str, bytes] | None = None,
                       when: str = '', backdrops: dict[str, bytes] | None = None,
                       days: int | None = None) -> bytes:
    covers, backdrops = covers or {}, backdrops or {}
    movies, shows = list(movies[:10]), list(shows[:10])
    poster = _Poster()
    leads = movies[:1] + shows[:1]
    poster.header(_period_title('观影榜', weekly, days), '电影与剧集 · 按播放次数排序', when,
                  _heroes(leads, covers, backdrops))
    poster.draw.line((600, 567, 600, 2060), fill=(122, 132, 111, 70), width=1)
    for column, (rows, label) in enumerate(((movies, '电影'), (shows, '剧集'))):
        x = column * 600
        heading = f'{label} TOP {len(rows)}' if rows else label
        poster.text(x + 42, 560, heading, 39, GOLD)
        poster.rule(x + 40, 615, x + 571, 130)
        if not rows:
            poster.text(x + 300, 847, f'本期暂无{label}播放记录', 30, MUTED, 'mt')
            continue
        first = rows[0]
        poster.crown(x + 43, 644)
        poster.text(x + 35, 690, '01', 62, GOLD)
        poster.cover(covers.get(str(first.get('item_id') or '')), (x + 124, 646, 218, 327))
        for index, line in enumerate(_wrap(first.get('title') or '—', 208, 35)):
            poster.text(x + 365, 682 + 45 * index, line, 35)
        poster.text(x + 365, 873, _title_count(first), 25)
        poster.movement(x + 571, 932, first)
        poster.rule(x + 40, 996, x + 571)
        for rank, row in enumerate(rows[1:], 2):
            y = 1021 + (rank - 2) * 116
            poster.text(x + 40, y + 23, f'{rank:02d}', 35)
            poster.cover(covers.get(str(row.get('item_id') or '')), (x + 119, y, 70, 100))
            poster.text(x + 209, y + 9, _clip(row.get('title') or '—', 353, 31), 31)
            poster.text(x + 209, y + 58, _title_count(row), 24, MUTED)
            poster.movement(x + 571, y + 59, row)
            poster.rule(x + 40, y + 110, x + 571, 60)
        if len(rows) < 10:
            y = 1040 if len(rows) == 1 else 1040 + (len(rows) - 1) * 116
            poster.text(x + 300, y, f'本期共 {len(rows)} 部 · 展示全部', 24, MUTED, 'mt')
    poster.footer('人数为去重观众；升降比较上一等长周期的同类榜单')
    return poster.jpeg()


def render_watch_poster(rows: list[dict[str, Any]], *, weekly: bool = False,
                        avatars: dict[str, bytes] | None = None, when: str = '',
                        covers: dict[str, bytes] | None = None,
                        backdrops: dict[str, bytes] | None = None,
                        days: int | None = None) -> bytes:
    rows, avatars = list(rows[:10]), avatars or {}
    poster = _Poster()
    poster.header(_period_title('观影达人榜', weekly, days), '把时间，留给喜欢的故事 · 按观影时长排序', when,
                  list((backdrops or {}).values())[:2] or list((covers or {}).values())[:2])
    poster.text(48, 560, f'观影时长 TOP {len(rows)}' if rows else '观影时长', 38, GOLD)
    poster.rule(48, 615, 1152, 130)
    if rows:
        first = rows[0]
        name = _watch_label(first)
        poster.crown(54, 662)
        poster.text(45, 719, '01', 75, GOLD)
        poster.avatar(avatars.get(str(first.get('tg_user_id') or '')), 177, 660, 186, name)
        poster.text(413, 678, _clip(name, 700, 47), 47)
        poster.text(413, 756, _duration(first), 43, GOLD)
        poster.text(413, 822, f"{int(first.get('plays') or 0)} 次播放", 26, MUTED)
        if _is_whitelist(first):
            poster.badge(964, 827)
        poster.rule(48, 927, 1152)
        for rank, row in enumerate(rows[1:], 2):
            y = 967 + (rank - 2) * 119
            name = _watch_label(row)
            poster.text(48, y + 22, f'{rank:02d}', 36)
            poster.avatar(avatars.get(str(row.get('tg_user_id') or '')), 142, y + 5, 76, name)
            poster.text(254, y + 7, _clip(name, 565, 33), 33)
            poster.text(1147, y + 9, _duration(row), 32, GOLD, 'rt')
            poster.text(254, y + 58, f"{int(row.get('plays') or 0)} 次播放", 24, MUTED)
            if _is_whitelist(row):
                poster.badge(986, y + 54)
            poster.rule(48, y + 107, 1152, 60)
    else:
        poster.text(600, 986, '本期暂无观影时长记录', 38, MUTED, 'mt')
    poster.footer('使用 Telegram 昵称展示；记录不完整时标明已知时长')
    return poster.jpeg()


def render_viewing_poster(*, name: str, label: str, days: int, hours: float,
                          plays: int, traffic: str,
                          titles: list[dict[str, Any]] | None = None,
                          covers: dict[str, bytes] | None = None,
                          whitelist: bool = False,
                          backdrops: dict[str, bytes] | None = None) -> bytes:
    titles, covers, backdrops = list((titles or [])[:5]), covers or {}, backdrops or {}
    height = max(1440, 1270 + len(titles) * 166) if titles else 1500
    poster = _Poster((1200, height))
    poster.header('我的观影' + label, _clip(_visible_label(name) or '会员', 920, 32), f'近 {int(days)} 天 · 你的私人片单',
                  _heroes(titles, covers, backdrops))
    if whitelist:
        poster.badge(998, 54)
    metrics = ((225, f'{hours:g}', '小时观影'), (600, str(int(plays)), '次播放'), (975, traffic or '暂无实测', '流量用量'))
    for x, value, caption in metrics:
        size = 60
        while size > 32 and _font(size).getlength(value) > 320:
            size -= 2
        poster.rule(x - 153, 569, x + 153, 110)
        poster.text(x, 620, value, size, GOLD, 'mt')
        poster.text(x, 706, caption, 27, MUTED, 'mt')
    poster.text(58, 808, '这段时间，看得最多', 38, GOLD)
    poster.rule(58, 864, 1142, 100)
    if titles:
        for rank, row in enumerate(titles, 1):
            y = 897 + (rank - 1) * 166
            poster.text(57, y + 37, f'{rank:02d}', 36, GOLD if rank == 1 else TEXT)
            poster.cover(covers.get(str(row.get('item_id') or '')), (135, y, 96, 142))
            for index, line in enumerate(_wrap(row.get('title') or '—', 864, 35, 2)):
                poster.text(267, y + 13 + index * 43, line, 35)
            poster.text(267, y + 108, f"{int(row.get('plays') or 0)} 次播放", 27, MUTED)
            poster.rule(58, y + 156, 1142, 55)
    else:
        poster.text(600, 1015, '本期暂无观影记录', 40, TEXT, 'mt')
        poster.text(600, 1085, '下一段好故事，等你开启。', 29, MUTED, 'mt')
    poster.footer('私人观影报告 · 仅发送给本人')
    return poster.jpeg()
