"""Explicit synthetic Bot private updates for isolated lottery fixtures."""
import time

from app.modules import telegram_private


def private_start(service, tg, update_id=1, date=None):
    telegram_private.observe(service.db, telegram_private.identity(service.bot), {
        'update_id': update_id, 'message': {'message_id': update_id + 1,
        'date': int(time.time()) if date is None else date, 'chat': {'id': int(tg), 'type': 'private'},
        'from': {'id': int(tg), 'is_bot': False, 'first_name': 'Fixture'}, 'text': '/start'}})
