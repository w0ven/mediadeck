"""Public game-result labels, anchored to Telegram identity, never account logins."""
from __future__ import annotations

import re
from html import escape, unescape


def telegram_username(value):
    value = str(value or '').strip().lstrip('@')
    return value if re.fullmatch(r'[A-Za-z0-9_]{1,32}', value) else ''


def result_mention(tg_id, display_name, username=''):
    handle = telegram_username(username)
    label = '@'+handle if handle else str(display_name or '成员')[:40]
    label = escape(label)
    identity = str(tg_id or '')
    if identity.isascii() and identity.isdecimal() and int(identity) > 0:
        return f'<a href="tg://user?id={int(identity)}">{label}</a>'
    return label  # Historical rows without a reliable ID are never guessed.


def text_units(html):
    return len(unescape(re.sub(r'<[^>]*>', '', html)).encode('utf-16-le'))//2
