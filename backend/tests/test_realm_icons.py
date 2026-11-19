"""Tests for Peekpeek+ custom realm icons and fallback stripping."""
from __future__ import annotations

from app.modules.realm_icons import (
    PACK_URL,
    REALM_CUSTOM_EMOJI,
    strip_tg_emoji_tags,
    tg_level_emoji,
    tg_realm_emoji,
)
from app.modules.telegram import TelegramBot


def test_realm_custom_emoji_covers_all_nine_levels_and_decorations() -> None:
    for key in ("lv1", "lv2", "lv3", "lv4", "lv5", "lv6", "lv7", "lv8", "lv9", "wl", "orn", "edict"):
        eid, fallback = REALM_CUSTOM_EMOJI[key]
        assert eid.isdigit() and len(eid) >= 15
        assert fallback
        tag = tg_realm_emoji(key, fallback)
        assert tag == f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    assert PACK_URL.startswith("https://t.me/addemoji/")


def test_level_emoji_uses_custom_pack_for_defaults_and_respects_custom_override() -> None:
    lv9_tag = tg_level_emoji(9, "🐉")
    assert lv9_tag == '<tg-emoji emoji-id="6048888337454867197">🐉</tg-emoji>'
    lv4_tag = tg_level_emoji(4, "⚔")
    assert lv4_tag == '<tg-emoji emoji-id="6048531984018316141">⚔️</tg-emoji>'
    assert tg_level_emoji(2, "🍃") == "🍃"


def test_plaque_places_custom_emoji_outside_code_blocks_and_strips_cleanly() -> None:
    member = {
        "group_id": "whitelist",
        "activity_level": {
            "level": 9,
            "name": "踏天",
            "icon": "🐉",
            "title": "顺为凡，逆则仙，只在心中一念间",
        },
    }
    lines = TelegramBot._identity_plaque_lines(member)
    assert len(lines) == 2
    assert '<code><tg-emoji' not in lines[0]
    assert 'emoji-id="6048888337454867197"' in lines[0]
    assert 'emoji-id="6048861618463318199"' in lines[0]
    assert 'emoji-id="6050968476015665060"' in lines[1]
    stripped = strip_tg_emoji_tags("\n".join(lines))
    assert "<tg-emoji" not in stripped
    assert "🐉 <code>Lv9 · 踏天</code>" in stripped
    assert "💠 <code>尊享白名单</code>" in stripped


def test_strip_keyboard_emoji_ids_removes_custom_icons_cleanly() -> None:
    from app.modules.realm_icons import strip_keyboard_emoji_ids
    markup = {
        "inline_keyboard": [
            [{"text": "个人信息", "icon_custom_emoji_id": "6050688598766788438", "callback_data": "me"}],
        ],
    }
    cleaned = strip_keyboard_emoji_ids(markup)
    assert cleaned == {"inline_keyboard": [[{"text": "个人信息", "callback_data": "me"}]]}


def test_watch_poster_renders_realm_badges_and_whitelist_together() -> None:
    from app.modules.rank_poster import render_watch_poster
    data = render_watch_poster(
        [
            {
                "tg_display_name": "踏天尊者",
                "tg_user_id": "101",
                "seconds": 7200,
                "plays": 5,
                "group_id": "whitelist",
                "activity_level": {"level": 9, "name": "踏天"},
            },
            {
                "tg_display_name": "元婴剑修",
                "tg_user_id": "102",
                "seconds": 3600,
                "plays": 2,
                "group_id": "standard",
                "activity_level": {"level": 4, "name": "元婴"},
            },
        ],
        weekly=True,
        when="2026-11-19",
    )
    assert data[:2] == b"\xff\xd8" and len(data) > 6000

