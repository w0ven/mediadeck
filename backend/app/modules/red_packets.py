"""Atomic group envelopes: user escrow and administrator rewards are distinct.

One new confirmation card has a known message ID. Publishing edits that same
card; a lost ACK is safely retried without a second post or financial replay.
Only a real claim button on that original Bot card can establish delivery.
"""
from __future__ import annotations

import json
import re
import secrets
import time
import unicodedata

from app.modules.economy_rules import economy_write, encode
from app.modules.game_mentions import telegram_username
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.groups import WHITELIST_GROUP_ID

DEFAULT_PACKET_CONFIG = {'max_total': 5000, 'max_parts': 50, 'ttl_hours': 24,
                         'enabled_for_members': True}
CALLBACKS = ('rpok:', 'rpcancel:', 'rpclaim:', 'rpedit:', 'rppage:', 'rpresult:')
RESULTS_PER_PAGE = 10
COMMANDS = {'/红包', '/redpacket', '/packet'}
SQL_MAX = 2**63 - 1


class PacketError(ValueError):
    """Controlled refusal; never embeds private balances or transport errors."""


def positive_int(raw, label):
    if isinstance(raw, (bool, float)) or not re.fullmatch(r'[0-9]{1,19}', str(raw)):
        raise PacketError(f'{label}必须是正整数')
    value = int(raw)
    if not 0 < value <= SQL_MAX:
        raise PacketError(f'{label}必须是可存储的正整数')
    return value


def validate_request(total, parts, mode, audience, config):
    total, parts = positive_int(total, '总额'), positive_int(parts, '份数')
    if mode not in ('equal', 'random') or audience not in ('all', 'whitelist'):
        raise PacketError('红包模式或领取范围无效')
    if total > config['max_total'] or parts > config['max_parts']:
        raise PacketError(f"当前最多{config['max_total']}分、{config['max_parts']}份")
    if total < parts:
        raise PacketError('每份至少1积分，总额不能少于份数')
    if mode == 'equal' and total % parts:
        raise PacketError('等额红包总额必须能被份数整除')
    return total, parts


def allocations(total, parts, mode):
    if mode == 'equal':
        return [total // parts] * parts
    remaining, values = total, []
    for count in range(parts, 1, -1):
        upper = min(remaining - count + 1, max(1, 2 * remaining // count))
        amount = secrets.randbelow(upper) + 1
        values.append(amount)
        remaining -= amount
    values.append(remaining)
    return values


def public_name(actor):
    """Only Telegram's public display name, never an account/handle/numeric ID."""
    name = ' '.join(actor.get(k, '') for k in ('first_name', 'last_name')
                    if isinstance(actor.get(k), str))
    name = ' '.join(''.join(c for c in name if unicodedata.category(c) not in ('Cc', 'Cf')).split())
    return (name[:128] + ('…' if len(name) > 128 else '')) if name and not name.isdecimal() else '成员'


class PacketService:
    def __init__(self, db, members, points, config, enabled, allowed, is_admin, bot_id):
        self.db, self.members, self.points = db, members, points
        self.config, self.enabled, self.allowed, self.is_admin = config, enabled, allowed, is_admin
        self.bot_id = str(bot_id)

    def get(self, nonce):
        return self.db.one('SELECT * FROM red_packets WHERE nonce=?', (str(nonce),))

    def _config(self):
        return {k: self.config().get(k, v) for k, v in DEFAULT_PACKET_CONFIG.items()}

    def _group(self, message):
        chat = message.get('chat') or {}
        if (chat.get('type') not in ('group', 'supergroup') or type(chat.get('id')) is not int
                or not self.allowed(chat)):
            raise PacketError('仅限原授权群内操作红包')
        if (message.get('sender_chat') or message.get('is_automatic_forward')
                or message.get('forward_origin') or message.get('forward_date')):
            raise PacketError('不能通过频道代发、自动转发或转发红包领取')
        return str(chat['id']), int(message.get('message_thread_id') or 0)

    def _actor(self, actor):
        try:
            tg = reliable_user({'from': actor})
        except GroupPointsError as exc:
            raise PacketError(str(exc)) from None
        member = self.members.find_by_telegram(tg)
        if not member or member.get('state') != 'active' or member.get('emby_missing_since'):
            raise PacketError('请先绑定当前有效系统账号，失效或禁用账号不能操作红包')
        return tg, member

    def _context(self, row, message):
        cid, thread = self._group(message)
        if row['permanent'] and not message.get('is_topic_message'):thread=0
        sender = message.get('from') or {}
        if (row['chat_id'] != cid or row['thread_id'] != thread
                or type(message.get('message_id')) is not int
                or row['card_message_id'] != message['message_id']
                or sender.get('is_bot') is not True or str(sender.get('id') or '') != self.bot_id):
            raise PacketError('请在原授权群、原话题的原Bot红包卡操作')

    def _sender(self, row, actor):
        tg, member = self._actor(actor)
        if tg != row['actor_tg_id'] or member['emby_user_id'] != row['actor_user_id']:
            raise PacketError('只有原发起人能确认；账号绑定变化请重新发起')
        funding = 'reward' if self.is_admin(member) else 'user'
        if funding != row['funding']:
            raise PacketError('系统角色已变化，请重新发起，不会切换结算方式')
        return member

    def prepare(self, message, total, parts, mode='random', audience='all', *, now=None):
        now = int(time.time()) if now is None else int(now)
        if not self.enabled():
            raise PacketError('红包功能尚未开启')
        cid, thread = self._group(message)
        try:
            reliable_user(message)
        except GroupPointsError as exc:
            raise PacketError(str(exc)) from None
        mid = message.get('message_id')
        if type(mid) is not int or mid <= 0:
            raise PacketError('无法核实原命令消息')
        with economy_write(self.db) as conn:
            tg, member = self._actor(message['from'])
            config = self._config()
            total, parts = validate_request(total, parts, mode, audience, config)
            funding = 'reward' if self.is_admin(member) else 'user'
            if funding == 'user' and not config['enabled_for_members']:
                raise PacketError('当前未开放普通成员发红包')
            command = encode({'total': total, 'parts': parts, 'mode': mode, 'audience': audience})
            prior = conn.execute('SELECT * FROM red_packets WHERE chat_id=? AND command_message_id=?', (cid, mid)).fetchone()
            if prior:
                if (prior['actor_tg_id'] != tg or prior['actor_user_id'] != member['emby_user_id']
                        or prior['funding'] != funding or prior['thread_id'] != (thread if not prior['permanent'] or message.get('is_topic_message') else 0) or prior['command_json'] != command):
                    raise PacketError('原消息已用于其他红包意图，请发送新命令')
                return dict(prior)
            if funding == 'user' and self.points.balance(member['emby_user_id']) < total:
                raise PacketError('积分不足，未扣费；请在本人私聊查看余额')
            row = {'nonce': secrets.token_hex(16), 'chat_id': cid, 'command_message_id': mid,
                   'actor_tg_id': tg, 'actor_user_id': member['emby_user_id'],
                   'actor_name': str(member.get('username') or '已绑定成员'), 'funding': funding,
                   'public_actor_name': public_name(message['from']),
                   'mode': mode, 'audience': audience, 'total': total, 'parts': parts,
                   'config_json': encode(config), 'command_json': command, 'thread_id': thread if message.get('is_topic_message') else 0,
                   'permanent': 1,
                   'created_at': now, 'confirm_by': now + 600}
            conn.execute(f"INSERT INTO red_packets({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
            return dict(conn.execute('SELECT * FROM red_packets WHERE nonce=?', (row['nonce'],)).fetchone())

    def begin_card_send(self, nonce):
        with economy_write(self.db) as conn:
            changed = conn.execute("UPDATE red_packets SET card_send_state='sending' WHERE nonce=? AND status='draft' AND card_send_state='pending' AND card_message_id IS NULL", (nonce,))
            return changed.rowcount == 1

    def card_send_failed(self, nonce, state):
        self.db.execute("UPDATE red_packets SET card_send_state=?,publish_error=? WHERE nonce=? AND card_message_id IS NULL",
                        ('failed' if state == 'failed' else 'unknown', '确认卡未取得回执，未扣费；请发送新命令，不自动重复发送', nonce))

    def bind_card(self, nonce, mid):
        if type(mid) is not int or mid <= 0:
            return False
        with economy_write(self.db) as conn:
            conn.execute("UPDATE red_packets SET card_message_id=?,card_send_state='sent',publish_error='' WHERE nonce=? AND card_message_id IS NULL AND status='draft'", (mid, nonce))
            row = conn.execute('SELECT card_message_id FROM red_packets WHERE nonce=?', (nonce,)).fetchone()
            return bool(row and row['card_message_id'] == mid)

    def edit_draft(self, nonce, actor, message, field, value):
        if field not in ('total', 'parts', 'mode', 'audience'):
            raise PacketError('不可修改此红包字段')
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row:
                raise PacketError('红包确认卡不存在')
            self._context(row, message)
            self._sender(row, actor)
            if row['status'] != 'draft' or row['confirm_by'] <= time.time() or not self.enabled():
                raise PacketError('此确认已结束，请重新发起')
            config = self._config()
            if row['funding'] == 'user' and not config['enabled_for_members']:
                raise PacketError('当前未开放普通成员发红包')
            changed = dict(row, **{field: value})
            total, parts = validate_request(changed['total'], changed['parts'], changed['mode'], changed['audience'], config)
            conn.execute('UPDATE red_packets SET total=?,parts=?,mode=?,audience=?,config_json=?,render_version=render_version+1,next_publish_at=0 WHERE nonce=?',
                         (total, parts, changed['mode'], changed['audience'], encode(config), nonce))
            return self.get(nonce)

    def confirm(self, nonce, actor, message, *, cancel=False, now=None):
        now = int(time.time()) if now is None else int(now)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row:
                raise PacketError('红包确认卡不存在')
            self._context(row, message)
            if (type(actor.get('id')) is not int or str(actor['id']) != row['actor_tg_id']
                    or actor.get('is_bot') is not False):
                raise PacketError('只有发起人能确认或取消')
            if row['status'] != 'draft':
                return row  # transport-only retry never replays settled finance
            self._sender(row, actor)
            if cancel:
                conn.execute("UPDATE red_packets SET status='cancelled',render_version=render_version+1,next_publish_at=0 WHERE nonce=?", (nonce,))
                return self.get(nonce)
            if row['confirm_by'] <= now:
                raise PacketError('确认已过期，未扣费，请重新发起')
            if not self.enabled():
                raise PacketError('红包功能已关闭，未扣费')
            config = self._config()
            if encode(config) != row['config_json']:
                raise PacketError('红包规则已变化，请重新选择参数并确认，未扣费')
            validate_request(row['total'], row['parts'], row['mode'], row['audience'], config)
            if row['funding'] == 'user':
                if not config['enabled_for_members']:
                    raise PacketError('当前未开放普通成员发红包')
                try:
                    self.points._apply(conn, row['actor_user_id'], -row['total'], 'packet.reserve', nonce,
                                       'tg:' + row['actor_tg_id'], now)
                except ValueError:
                    raise PacketError('积分不足，未扣费；余额请在本人私聊查看') from None
            values = allocations(row['total'], row['parts'], row['mode'])
            assert sum(values) == row['total'] and min(values) >= 1
            conn.execute("UPDATE red_packets SET status='active',confirmed_at=?,expires_at=?,allocations_json=?,remaining=total,pin_state=?,"
                         "render_version=render_version+1,next_publish_at=0 WHERE nonce=?",
                         (now, 0 if row['permanent'] else now + config['ttl_hours'] * 3600, encode(values), 'pending' if row['permanent'] else '', nonce))
            conn.execute('INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)',
                         (now, 'tg:' + row['actor_tg_id'], 'points.packet.' + row['funding'] + '.confirm',
                          row['actor_user_id'], encode({k: row[k] for k in ('nonce','chat_id','thread_id','actor_user_id','actor_tg_id','funding','mode','audience','total','parts')})))
            return self.get(nonce)

    def _credit(self, conn, user, amount, reason, nonce, actor, now):
        if self.points.balance(user) + amount > SQL_MAX:
            raise PacketError('积分超出可存储范围，未入账，请管理员处理')
        return self.points._apply(conn, user, amount, reason, nonce, actor, now)

    def claim(self, nonce, actor, message, *, now=None):
        now = int(time.time()) if now is None else int(now)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row:
                raise PacketError('红包不存在')
            self._context(row, message)
            # The Bot-generated button on the known card proves publish delivery,
            # including a successful edit whose ACK was lost. Data alone does not.
            buttons = [b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard', []) for b in line]
            if 'rpclaim:' + nonce not in buttons:
                raise PacketError('请使用原Bot红包卡的领取按钮')
            tg, member = self._actor(actor)
            uid = member['emby_user_id']
            if uid == row['actor_user_id'] or tg == row['actor_tg_id']:
                raise PacketError('发起人不能领取自己发出的红包')
            if row['audience'] == 'whitelist' and member.get('group_id') != WHITELIST_GROUP_ID:
                raise PacketError('此红包仅当前有效白名单成员可领')
            prior = conn.execute('SELECT * FROM red_packet_claims WHERE nonce=? AND (user_id=? OR tg_user_id=?)', (nonce, uid, tg)).fetchone()
            if prior:
                return {'ok': True, 'already': True, 'amount': prior['amount']}
            if row['status'] != 'active' or not row['permanent'] and row['expires_at'] <= now:
                raise PacketError('红包已领完或已到期')
            values = json.loads(row['allocations_json'])
            slot = row['claimed_count']
            amount = values[slot]
            reason = 'packet.claim' if row['funding'] == 'user' else 'packet.reward'
            self._credit(conn, uid, amount, reason, nonce, 'packet:' + row['actor_user_id'], now)
            conn.execute('INSERT INTO red_packet_claims(nonce,user_id,tg_user_id,amount,slot,claimed_at,display_name,tg_username) VALUES(?,?,?,?,?,?,?,?)',
                         (nonce, uid, tg, amount, slot, now, public_name(actor), telegram_username(actor.get('username'))))
            exhausted = slot + 1 == row['parts']
            conn.execute('UPDATE red_packets SET remaining=remaining-?,claimed_count=claimed_count+1,status=?,result_page=?,render_version=render_version+1,next_publish_at=0 WHERE nonce=?',
                         (amount, 'exhausted' if exhausted else 'active', slot // RESULTS_PER_PAGE, nonce))
            if exhausted and row['permanent']:
                from app.modules.packet_delivery import complete_receipt
                payload = complete_receipt(self.get(nonce), self.claims(nonce, slot+1))
                conn.execute("UPDATE red_packets SET receipt_state='pending',unpin_state='pending',receipt_page=0,receipt_payload=? WHERE nonce=?", (encode(payload), nonce))
            conn.execute('INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)',
                         (now, 'tg:' + tg, 'points.packet.' + row['funding'] + '.claim', uid,
                          encode({'nonce': nonce, 'amount': amount, 'slot': slot, 'sender': row['actor_user_id'],
                                  'sender_tg': row['actor_tg_id'], 'chat_id': row['chat_id'], 'funding': row['funding']})))
            return {'ok': True, 'already': False, 'amount': amount}

    def expire(self, nonce, *, now=None):
        now = int(time.time()) if now is None else int(now)
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row:
                return None
            if row['status'] == 'draft' and row['confirm_by'] <= now:
                conn.execute("UPDATE red_packets SET status='cancelled',render_version=render_version+1 WHERE nonce=?", (nonce,))
                return self.get(nonce)
            if row['status'] != 'active' or row['permanent'] or row['expires_at'] > now:
                return row
            remaining = row['remaining']
            refunded, voided = (remaining, 0) if row['funding'] == 'user' else (0, remaining)
            if refunded:
                self._credit(conn, row['actor_user_id'], refunded, 'packet.refund', nonce, 'system', now)
            conn.execute("UPDATE red_packets SET status='expired',remaining=0,refunded=?,voided=?,render_version=render_version+1,"
                         "next_publish_at=0,settlement_error='',settlement_retry_at=0 WHERE nonce=?", (refunded, voided, nonce))
            conn.execute('INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)',
                         (now, 'system', 'points.packet.' + row['funding'] + '.expire', row['actor_user_id'],
                          encode({'nonce': nonce, 'refunded': refunded, 'voided': voided, 'claimed': row['total'] - remaining})))
            return self.get(nonce)

    def expire_due(self):
        now = int(time.time())
        due = self.db.query("SELECT nonce FROM red_packets WHERE settlement_retry_at<=? AND "
                            "((status='active' AND permanent=0 AND expires_at<=?) OR (status='draft' AND confirm_by<=?)) ORDER BY created_at LIMIT 100", (now, now, now))
        for row in due:
            try:
                self.expire(row['nonce'], now=now)
            except Exception as exc:  # noqa: BLE001 - money remains in durable escrow, retry after recovery
                self.db.execute("UPDATE red_packets SET settlement_retry_at=?,settlement_error=?,render_version=render_version+1,next_publish_at=0 WHERE nonce=? AND status IN ('active','draft')",
                                (now + 60, type(exc).__name__ + '：到期结算待重试', row['nonce']))
        return len(due)

    def pending_renders(self):
        return self.db.query('SELECT * FROM red_packets WHERE card_message_id IS NOT NULL AND render_version>rendered_version AND next_publish_at<=? ORDER BY created_at LIMIT 100', (int(time.time()),))

    def rendered(self, nonce, version, success):
        self.db.execute('UPDATE red_packets SET rendered_version=?,next_publish_at=?,publish_error=? WHERE nonce=? AND render_version=?',
                        (version if success else -1, 0 if success else int(time.time()) + 10,
                         '' if success else '群卡更新未确认，后台将重试；不会重复扣费', nonce, version))

    def claims(self, nonce, count):
        # Bound to the rendered row's committed progress, not a later claim.
        return self.db.query('SELECT amount,slot,display_name,tg_user_id,tg_username FROM red_packet_claims '
                             'WHERE nonce=? AND slot<? ORDER BY slot', (nonce, count))

    def page(self, nonce, actor, message, page):
        with economy_write(self.db) as conn:
            row = self.get(nonce)
            if not row:
                raise PacketError('红包不存在')
            self._context(row, message)
            try:
                reliable_user({'from': actor})
            except GroupPointsError as exc:
                raise PacketError(str(exc)) from None
            buttons = [b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard', []) for b in line]
            if ('rppage:' + nonce + ':' + str(page) not in buttons
                    or row['status'] in ('draft', 'cancelled')
                    or not 0 <= page <= max(0, (row['claimed_count'] - 1) // RESULTS_PER_PAGE)):
                raise PacketError('请使用原红包的翻页按钮')
            if row['result_page'] != page:
                conn.execute('UPDATE red_packets SET result_page=?,render_version=render_version+1,next_publish_at=0 WHERE nonce=?',
                             (page, nonce))
            return self.get(nonce)

    def best(self, nonce):
        return self.db.one('SELECT amount,slot,display_name,tg_user_id,tg_username FROM red_packet_claims '
                           'WHERE nonce=? ORDER BY amount DESC,slot ASC LIMIT 1', (nonce,))
