"""Seed persisted v0.42.5 rounds for compatibility tests; never a production opener."""
import hashlib
import secrets
import time

from app.modules import dice_beacon as beacon
from app.modules.dice_group import GROUP_VERSION
from app.modules.economy_rules import economy_write, encode
from app.modules.play_money import PlayError, integer


def legacy_group_create(self, message, *, now=None):
    chat, thread = self.group(message)
    mid = integer(message.get('message_id'), '消息ID')
    with economy_write(self.db) as conn:
        clock = time.time() if now is None else now
        _, member = self.actor(message.get('from') or {}, now=clock)
        if not self.enabled():
            raise PlayError('骰局暂未开放')
        if conn.execute("SELECT 1 FROM dice_rounds WHERE bot_id=? AND error<>'' AND state IN ('open','locked','fixed')", (self.bot_id,)).fetchone():
            raise PlayError('骰局正在核对异常，暂不接受新局')
        previous = conn.execute('SELECT * FROM dice_rounds WHERE bot_id=? AND chat_id=? AND command_mid=?', (self.bot_id, chat, mid)).fetchone()
        if previous:
            if previous['actor_uid'] != member['emby_user_id'] or previous['thread_id'] != thread:
                raise PlayError('原命令归属不符')
            return dict(previous)
        active = conn.execute("SELECT * FROM dice_rounds WHERE bot_id=? AND chat_id=? AND thread_id=? AND state IN ('opening','open','locked','fixed')", (self.bot_id, chat, thread)).fetchone()
        if active:
            return dict(active)
        if conn.execute('SELECT 1 FROM dice_rounds WHERE bot_id=? AND actor_uid=? AND created>?', (self.bot_id, member['emby_user_id'], clock-15)).fetchone():
            raise PlayError('请稍后再开局')
        nonce, secret = secrets.token_hex(12), secrets.token_hex(32)
        deadline = clock + 180
        row = {'nonce': nonce, 'bot_id': self.bot_id, 'chat_id': chat, 'thread_id': thread,
               'command_mid': mid, 'actor_uid': member['emby_user_id'], 'created': clock,
               'deadline': deadline, 'timeout': deadline+600, 'round_number': beacon.future_round(deadline),
               'rules': encode({'version': GROUP_VERSION, 'draw_version': beacon.VERSION, 'presentation': 'caption-v1', 'chain': beacon.CHAIN, 'window': 180, 'wait': 600, 'fee': 0}),
               'secret': secret, 'commitment': hashlib.sha256(bytes.fromhex(secret)).hexdigest()}
        conn.execute(f"INSERT INTO dice_rounds({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
        for pool in ('size',):
            conn.execute('INSERT INTO dice_pools(nonce,pool) VALUES(?,?)', (nonce, pool))
        return self.get(nonce)

