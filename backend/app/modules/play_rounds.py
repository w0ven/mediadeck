"""Group identity and durable original-card publication for the two games."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from app.modules.economy_rules import economy_write
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.play_money import PlayError


def fair_split(total, winners, secret):
    ordered = sorted(winners, key=lambda uid: hmac.new(bytes.fromhex(secret), str(uid).encode(), hashlib.sha256).digest())
    base, remainder = divmod(total, len(ordered))
    return {uid: base + (i < remainder) for i, uid in enumerate(ordered)}


class PlayAccess:
    def __init__(self, db, members, allowed, bot_id):
        self.db, self.members, self.allowed, self.bot_id = db, members, allowed, str(bot_id)

    def actor(self, actor, *, now=None):
        try:
            tg = reliable_user({'from': actor})
        except GroupPointsError as exc:
            raise PlayError(str(exc)) from None
        member = self.members.find_by_telegram(tg)
        clock = time.time() if now is None else now
        expires = (member or {}).get('expires_at_effective')
        if (not member or member.get('state') != 'active' or member.get('emby_missing_since')
                or (expires is not None and expires <= clock)):
            raise PlayError('仅当前有效绑定成员可参与')
        return tg, member

    def group(self, message):
        chat = message.get('chat') or {}
        if (chat.get('type') not in ('group', 'supergroup') or type(chat.get('id')) is not int
                or not self.allowed(chat) or message.get('sender_chat') or message.get('is_automatic_forward')
                or message.get('forward_origin') or message.get('forward_date')):
            raise PlayError('请在授权群内用本人账号操作')
        thread = message.get('message_thread_id') or 0
        if type(thread) is not int or thread < 0:
            raise PlayError('话题无效')
        # Non-topic replies can carry a changing reply-chain thread id.
        # Match TelegramBot._thread_id: only a real forum topic binds a scope.
        return str(chat['id']), thread if message.get('is_topic_message') else 0


class RoundCards(PlayAccess):
    def get(self, nonce):
        return self.db.one('SELECT * FROM play_rounds WHERE nonce=?', (str(nonce),))

    def players(self, nonce):
        return self.db.query('SELECT * FROM play_players WHERE nonce=? ORDER BY id', (str(nonce),))

    def context(self, conn, row, message, data):
        chat, thread = self.group(message)
        sender = message.get('from') or {}
        mid = message.get('message_id')
        actions = [b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard', []) for b in line]
        if (row['bot_id'] != self.bot_id or row['chat_id'] != chat or row['thread_id'] != thread
                or type(mid) is not int or mid <= 0 or sender.get('is_bot') is not True
                or str(sender.get('id')) != self.bot_id or data not in actions):
            raise PlayError('请使用原群、原话题的原Bot游戏卡')
        if row['card_message_id'] is None:
            # Recover only a real original-card callback after a lost send ACK.
            original = (message.get('reply_to_message') or {}).get('message_id')
            if row['card_send_state'] not in ('sending', 'unknown') or original != row['command_message_id']:
                raise PlayError('原卡回执尚未确认')
            conn.execute("UPDATE play_rounds SET card_message_id=?,card_send_state='sent',next_publish_at=0 WHERE nonce=? AND card_message_id IS NULL", (mid, row['nonce']))
        elif row['card_message_id'] != mid:
            raise PlayError('请使用原游戏消息')

    def begin_publish(self, nonce, *, now=None):
        clock = time.time() if now is None else now
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if (not row or row['bot_id'] != self.bot_id or row['next_publish_at'] > clock
                    or row['publish_lease'] > clock or row['rendered_revision'] >= row['revision']):
                return None
            if row['card_message_id'] is None and row['card_send_state'] != 'pending':
                return None  # uncertain initial send must not create a second card
            token = secrets.token_hex(12)
            conn.execute("UPDATE play_rounds SET publish_token=?,publish_lease=?,card_send_state=? WHERE nonce=?",
                         (token, clock + 90, 'sending' if row['card_message_id'] is None else 'sent', nonce))
            return dict(row, publish_token=token, _players=self.players(nonce))

    def published(self, row, mid):
        if type(mid) is not int or mid <= 0:
            return False
        with economy_write(self.db) as conn:
            current = self.get(row['nonce'])
            if current['publish_token'] != row['publish_token']:
                return False
            if current['card_message_id'] not in (None, mid):
                return False
            conn.execute("UPDATE play_rounds SET card_message_id=?,card_send_state='sent',rendered_revision=MAX(rendered_revision,?),publish_token='',publish_lease=0,publish_error='',next_publish_at=0 WHERE nonce=? AND publish_token=?",
                         (mid, row['revision'], row['nonce'], row['publish_token']))
            return True

    def publish_failed(self, row, failure, *, now=None):
        clock = time.time() if now is None else now
        with economy_write(self.db) as conn:
            current = self.get(row['nonce'])
            if current['publish_token'] != row['publish_token']:
                return
            # Bound-card edits are always retryable on exactly the same message.
            state = 'sent' if current['card_message_id'] is not None else ('pending' if failure.get('state') == 'retry' else 'unknown')
            conn.execute("UPDATE play_rounds SET card_send_state=?,publish_token='',publish_lease=0,next_publish_at=?,publish_error=? WHERE nonce=? AND publish_token=?",
                         (state, clock + max(10, int(failure.get('retry_after') or 0)),
                          '群卡更新未确认，等待原卡重试' if current['card_message_id'] else '发卡回执未确认，未重复发卡', row['nonce'], row['publish_token']))

    def pending_cards(self, kind, *, now=None):
        clock = time.time() if now is None else now
        self.db.execute("UPDATE play_rounds SET card_send_state='unknown',publish_token='',publish_lease=0,publish_error='重启后发卡结果不明，等待原卡确认' WHERE kind=? AND bot_id=? AND card_message_id IS NULL AND card_send_state='sending' AND publish_lease<=?", (kind, self.bot_id, clock))
        return self.db.query("SELECT nonce FROM play_rounds WHERE kind=? AND bot_id=? AND revision>rendered_revision AND (card_message_id IS NOT NULL OR card_send_state='pending') AND next_publish_at<=? AND publish_lease<=? ORDER BY created_at LIMIT 50",
                             (kind, self.bot_id, clock, clock))
