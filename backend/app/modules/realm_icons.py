"""Custom Telegram emoji & MiniApp icon registry for Peekpeek+ realm badges.

Pack: https://t.me/addemoji/peekpeek_realms_by_cola_embybot
Lv1-Lv3 are crisp static badges; Lv4-Lv9, whitelist, ornament and edict are
animated VP9 WebM custom emojis (and animated WebP in MiniApp).
"""
from __future__ import annotations

import re
from html import escape

PACK_NAME = "peekpeek_realms_by_cola_embybot"
PACK_URL = f"https://t.me/addemoji/{PACK_NAME}"

# key -> (custom_emoji_id, exact Telegram fallback emoji registered in sticker set)
REALM_CUSTOM_EMOJI: dict[str, tuple[str, str]] = {
    "lv1": ("6048768009651101568", "🌱"),
    "lv2": ("6050704202382974828", "🗡️"),
    "lv3": ("6050929121230331778", "🔥"),
    "lv4": ("6048531984018316141", "⚔️"),
    "lv5": ("6050766921790399318", "🌩️"),
    "lv6": ("6051041232761661291", "☯️"),
    "lv7": ("6048662336275750664", "👑"),
    "lv8": ("6051056333866672566", "🪷"),
    "lv9": ("6048888337454867197", "🐉"),
    "wl": ("6048861618463318199", "💠"),
    "orn": ("6050968476015665060", "✨"),
    "edict": ("6048412558157684859", "🌟"),
}

_TG_EMOJI_RE = re.compile(r'<tg-emoji\s+emoji-id="[^"]*">([^<]*)</tg-emoji>')


def _normalize_emoji(value: str) -> str:
    return str(value or "").replace("\ufe0f", "").strip()


def tg_realm_emoji(key: str, fallback: str = "") -> str:
    """Return a Telegram HTML `<tg-emoji>` tag for a registered realm icon."""
    entry = REALM_CUSTOM_EMOJI.get(key)
    if entry is None:
        return escape(fallback)
    eid, registered_fallback = entry
    return f'<tg-emoji emoji-id="{eid}">{registered_fallback}</tg-emoji>'


def tg_level_emoji(level: int, fallback: str = "") -> str:
    """Return the custom Telegram emoji tag for Lv1..Lv9 when using default icon.

    If an admin explicitly configured a different icon for a level, respect it.
    """
    entry = REALM_CUSTOM_EMOJI.get(f"lv{int(level)}")
    if entry is None:
        return escape(fallback)
    eid, registered_fallback = entry
    if fallback and _normalize_emoji(fallback) != _normalize_emoji(registered_fallback):
        return escape(fallback)
    return f'<tg-emoji emoji-id="{eid}">{registered_fallback}</tg-emoji>'


def strip_tg_emoji_tags(html: str) -> str:
    """Replace `<tg-emoji ...>fallback</tg-emoji>` with its plain fallback emoji."""
    return _TG_EMOJI_RE.sub(r"\1", html)
