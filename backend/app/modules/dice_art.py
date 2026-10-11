"""Pre-rendered cup art: no runtime font or imaging dependency."""
from __future__ import annotations

import json
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / 'assets' / 'dice'


def art_name(row, *, opening=False):
    dice = [] if opening else json.loads(row['dice'])
    if not dice:
        return 'covered'
    if len(dice) != 3 or any(type(d) is not int or not 1 <= d <= 6 for d in dice):
        raise ValueError('invalid persisted dice')
    return ''.join(map(str, dice))


def art_bytes(name):
    if name != 'covered' and (len(name) != 3 or any(d not in '123456' for d in name)):
        raise ValueError('invalid dice asset')
    return (ASSETS / (name + '.jpg')).read_bytes()
