"""New-round timing only; existing deadlines and draw rounds are immutable."""
from app.modules.play_money import integer

DEFAULT_WINDOW = 90
MIN_WINDOW = 30
MAX_WINDOW = 600


def window_seconds(config=None):
    return integer((config or {}).get('window_seconds', DEFAULT_WINDOW), '封盘秒数',
                   low=MIN_WINDOW, high=MAX_WINDOW)
