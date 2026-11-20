"""Member activity UX lives only in the private Bot; admin Web is separate."""
from __future__ import annotations

import secrets
import time
from datetime import datetime
from html import escape

from app.modules.bot_views import bandwidth_value, duration
from app.modules.economy_rules import BEIJING, economy_write, encode
from app.modules.levels import DAY
from app.modules.member_devices import device_cap
from app.modules.realm_icons import PACK_URL, custom_emoji_id, tg_level_emoji, tg_realm_emoji


def beijing_stamp(value):
    return datetime.fromtimestamp(value, BEIJING).strftime('%Y-%m-%d %H:%M') if value else '永久'


def remain_label(left):
    if left >= 3 * DAY:
        return f'剩 {int(left // DAY)} 天'
    if left >= DAY:
        days = int(left // DAY)
        hours = int((left % DAY) // 3600)
        return f'剩 {days} 天 {hours} 小时' if hours else f'剩 {days} 天'
    hours = int(left // 3600)
    return f'剩 {hours} 小时' if hours >= 1 else '剩不到 1 小时'


def expiry_line(expires_at, now=None):
    if not expires_at:
        return '♾ 永久有效'
    left = expires_at - (time.time() if now is None else now)
    if left <= 0:
        return f'⌛ 已于 {beijing_stamp(expires_at)} 到期'
    return f'⏳ {beijing_stamp(expires_at)} 到期 · {remain_label(left)}'


def growth_bar(level, slots=8):
    if not level.get('next_threshold'):
        if level['level'] == 9:
            return '<code>✦踏天逆仙✦</code>'
        if level['level'] == 8:
            return '<code>✦道祖敕封✦</code>'
        return '<code>' + '▰' * slots + '</code>'
    cur = max(0, int(level.get('growth') or 0))
    nxt = max(1, int(level['next_threshold']))
    filled = min(slots, int(cur * slots // nxt))
    if cur > 0 and filled == 0:
        filled = 1
    return '<code>' + '▰' * filled + '▱' * (slots - filled) + '</code>'


def growth_line(level):
    bar = growth_bar(level)
    text = f"📈 {bar} 成长 {level['growth']}"
    if level.get('next_threshold'):
        gap = max(0, level['next_threshold'] - level['growth'])
        return text + f" / {level['next_threshold']} · 距 Lv{level['level'] + 1} 还差 {gap}"
    return text + (' · 专属等级' if level['level'] in (8, 9) else ' · 已满级')


def renewal_blockers(info):
    reasons = []
    if info['exempt']:
        reasons.append('白名单账号永久有效，无需续期')
    elif not info.get('expires_at'):
        reasons.append('当前账号永久有效，无需续期')
    elif not info['managed']:
        reasons.append('账号尚未开通等级续期')
    if info['lifecycle'] != 'ready':
        reasons.append('账号删除流程待确认，请联系管理员')
    if info.get('expires_at') and not info['window_open']:
        reasons.append('到期前 72 小时内才能续期')
    if info['shortfall']:
        reasons.append(f"积分不足，还差 {info['shortfall']} 积分")
    return reasons


class ActivityBotMixin:
    @staticmethod
    def _settled_reward_lines(info):
        """Only paid rewards for this Beijing viewing date; no pending/zero claim."""
        today = info['today']
        parts = [f"+{today['points']} 积分" if today['points'] else '',
                 f"+{today['growth']} 成长" if today['growth'] else '']
        parts = [part for part in parts if part]
        if not parts:
            return []
        day = str(info['day'])
        current_day = datetime.fromtimestamp(time.time(), BEIJING).date().isoformat()
        label = '今日观影奖励' if day == current_day else f"{escape(day[5:])} 观影奖励"
        return [f"🎁 {label} " + ' · '.join(parts)]

    @staticmethod
    def _renewal_lines(info):
        if info['exempt'] or not info['managed']:
            return []
        price = f"🔁 续期 {info['renewal_price']} 积分 / 30 天"
        if info['shortfall']:
            price += f" · 还差 {info['shortfall']}"
        return [price, '🤖 自动保号：' + ('已开启' if info['auto_renew'] else '未开启')]

    def _activity_summary(self, member):
        levels = self._members._levels
        info = levels.status(member['emby_user_id'])
        level = info['level']
        title = info['worn_title'] or level['title']
        status = self._status_label(member)
        term = '♾ 永久有效' if info['exempt'] else expiry_line(info['expires_at'])
        hero = [*self._identity_plaque_lines(member, title=title), f"{status} · {term}"]
        econ = [f"🪙 积分 <b>{info['balance']}</b>", growth_line(level), *self._settled_reward_lines(info)]
        renewal = self._renewal_lines(info)
        if renewal:
            econ += renewal
        lines = [
            f"👤 <b>{escape(str(member.get('username') or '—'))}</b>",
            '<blockquote>' + '\n'.join(hero) + '</blockquote>',
            '<blockquote>' + '\n'.join(econ) + '</blockquote>',
        ]
        return lines, info

    @staticmethod
    def _activity_menu(info):
        rows = []
        if info['managed'] and not info['exempt']:
            rows.append([{'text':'⏳ 续期 30 天', 'callback_data':'activity:renew'},
                         {'text':'🛑 关闭自动保号' if info['auto_renew'] else '🤖 开启自动保号',
                          'callback_data':'activity:auto:off' if info['auto_renew'] else 'activity:auto:on'}])
        rows += [
            [{'text': '收藏全套境界表情包', 'icon_custom_emoji_id': custom_emoji_id('orn'), 'url': PACK_URL}],
            [{'text': '◀ 我的账号', 'callback_data': 'me'}, {'text': '背包', 'icon_custom_emoji_id': custom_emoji_id('btn_bag'), 'callback_data': 'bag'}],
        ]
        return rows

    def _activity_page(self, member, info):
        """Level detail: jade-tablet identity, power gauge, rights, watch stats and 9-realm ladder."""
        level = info['level']
        levels_svc = getattr(self._members, '_levels', None)
        all_levels = (levels_svc.config()['config']['levels'] if levels_svc is not None else [])
        hero_lines = [*self._identity_plaque_lines(member, title=info['worn_title'] or level['title']),
                      growth_line(level), *self._settled_reward_lines(info)]
        if info['exempt']:
            hero_lines.append(f"{self._status_label(member)} · ♾ 永久有效")
        else:
            hero_lines += [expiry_line(info['expires_at']), *self._renewal_lines(info)]
        streams = member.get('max_streams')
        quota = member['group'].get('request_quota')
        right_lines = [f"🚀 带宽 <b>{bandwidth_value(member)}</b>",
                       f"📺 同播 <b>{streams or '不限'}{' 路' if streams else ''}</b> · 🎬 每月求片 <b>{quota or '不限'}{' 次' if quota else ''}</b>",
                       ('📥 可下载' if member.get('allow_download') else '📥 不可下载')
                       + (' · 🎞 可转码' if member.get('allow_transcode') else ' · 🎞 不可转码')]
        cap = device_cap(member)
        if cap:
            right_lines.append(f"📱 最多登录 <b>{cap} 台</b>设备")
        sections = [
            '🌱 <b>等级与权益</b> · 仙籍玉简',
            '<blockquote>' + '\n'.join(hero_lines) + '</blockquote>',
            '<blockquote><b>✨ 当前权益</b>\n' + '\n'.join(right_lines) + '</blockquote>',
        ]
        if self._stats is not None and hasattr(self._stats, 'watch_summary'):
            history = self._stats.watch_summary(member['emby_user_id']) or {}
            sections.append(
                '<blockquote><b>📊 观看时长</b>\n'
                f"近 24 小时 <b>{duration(history.get('seconds_24h'))}</b> · 近 30 天 <b>{duration(history.get('seconds_30d'))}</b>\n"
                f"累计 <b>{duration(history.get('recorded_seconds'))}</b></blockquote>"
            )
        if all_levels and not info['exempt']:
            ladder = []
            for row in all_levels:
                lv_no = row['level']
                cur = lv_no == level['level']
                mark = '🔥' if cur else ('✦' if lv_no < level['level'] and level['level'] <= 7 else '·')
                gate = f"{row['threshold']}成长" if row.get('threshold') is not None else f"{tg_realm_emoji('edict', '🌟')} 圣谕敕封"
                lv_icon = tg_level_emoji(lv_no, str(row['icon']))
                entry = f"{mark} {lv_icon} Lv{lv_no} {escape(row['name'])}「{escape(row['title'])}」· {gate}"
                ladder.append(f"<b>{entry} ◀ 当前</b>" if cur else entry)
            sections.append('<blockquote expandable><b>🌌 九重仙途 · 境界总览</b>\n' + '\n'.join(ladder) + '</blockquote>')
        if member.get('note'):
            sections.append('📝 备注：' + escape(str(member['note'])))
        return '\n'.join(sections)

    async def _activity_action(self, chat_id, message_id, member, action, *, actor, in_group=False, callback=False):
        # Even a forwarded/foreign group card never performs a private money action.
        if in_group or str(chat_id) != str(actor):
            await self.send(chat_id, '💬 请私聊 Bot，打开「我的账号 → 等级与权益」')
            return
        levels = getattr(self._members, '_levels', None)
        if not member or str(member.get('tg_user_id')) != str(actor):
            await self.send(chat_id, '🔗 请先绑定账号。')
            return
        if levels is None:
            await self.send(chat_id, '⏳ 等级服务暂不可用，请稍后再试。')
            return
        uid = member['emby_user_id']
        async def show(text, keyboard):
            if callback:
                return await self._edit(chat_id, message_id, text, keyboard)
            return await self._show(chat_id, text, keyboard)
        back = [[{'text':'◀ 等级与权益','callback_data':'activity:status'}]]
        try:
            if action.startswith('confirm:'):
                nonce = action.split(':', 1)[1]
                result = levels.renew(uid, 'bot-renew:'+nonce, bot_intent=(nonce, str(actor), str(chat_id)))
                warning = await self._after_member_change(member)
                suspended = member.get('state', member.get('status')) == 'suspended'
                await show(f"✅ <b>续期成功</b>\n\n💰 已扣 {result['cost']} 积分\n"
                           f"⏩ 新到期：{beijing_stamp(result['expires_at'])}\n"
                           f"⏪ 原到期：{beijing_stamp(result['previous_expires_at'])}"
                           + ('\n\nℹ️ 账号仍处于停用状态，续期不会解除停用，请联系管理员。' if suspended else '')
                           + warning, back)
                return
            if action.startswith('cancel:'):
                nonce = action.split(':', 1)[1]
                with economy_write(levels.db) as conn:
                    conn.execute('UPDATE activity_renew_intents SET expires_at=0 WHERE nonce=? AND user_id=? '
                                 'AND tg_user_id=? AND chat_id=? AND result_json IS NULL',
                                 (nonce, uid, str(actor), str(chat_id)))
                await show('已取消，没有扣除积分。', back)
                return
            if action in ('auto:on', 'auto:off'):
                enabling = action == 'auto:on'
                text = ('🤖 <b>开启自动保号</b>\n<i>默认关闭，确认后才会开启。</i>\n\n'
                        '到期前 72 小时内，如果积分足够，会按当时的等级价格自动扣积分续期 30 天。\n'
                        '积分不足时不扣分，也不会续期。\n\n'
                        '⚠️ 账号到期后无法播放，超过 72 小时可能被删除。' if enabling else
                        '🛑 <b>关闭自动保号</b>\n\n'
                        '关闭后不再自动扣积分续期；当前到期时间不变，仍可手动续期。')
                nonce = secrets.token_hex(12)
                self._pending[self._pkey(chat_id)] = ('activity_auto', time.time()+300,
                    {'nonce':nonce, 'uid':uid, 'actor':str(actor), 'enabled':enabling,
                     'binding':self._account_binding_revision(uid)})
                await show(text, [[{'text':'✅ 确认开启' if enabling else '🛑 确认关闭',
                                   'callback_data':'activity:auto-confirm:'+nonce}], *back])
                return
            if action.startswith('auto-confirm:'):
                pending = self._pending.get(self._pkey(chat_id))
                nonce = action.split(':', 1)[1]
                if (not pending or pending[0] != 'activity_auto' or pending[1] <= time.time()
                        or pending[2]['nonce'] != nonce or pending[2]['uid'] != uid or pending[2]['actor'] != str(actor)
                        or pending[2]['binding'] != self._account_binding_revision(uid)):
                    raise ValueError('确认已过期，请重新操作')
                enabled = pending[2]['enabled']
                levels.set_auto(uid, enabled, tg_uid=str(actor), binding_revision=pending[2]['binding'])
                self._pending.pop(self._pkey(chat_id), None)
                await show('✅ 自动保号已开启\n到期前 72 小时内，积分足够时会自动续期。' if enabled
                           else '✅ 自动保号已关闭\n到期前请记得手动续期。', back)
                return
            self._pending.pop(self._pkey(chat_id), None)
            _, info = self._activity_summary(member)
            if action == 'renew':
                now = int(time.time())
                reasons = renewal_blockers(info)
                expiry = info['expires_at']
                new_expiry = max(now, expiry)+30*DAY if expiry else None
                text = (f"🧾 <b>续期 30 天</b>\n\n💰 费用：{info['renewal_price']} 积分（余额 {info['balance']}）\n"
                        f"⏪ 当前到期：{beijing_stamp(expiry)}\n"
                        f"⏩ 续期后：{beijing_stamp(new_expiry)}\n"
                        '<i>未到期从原到期日顺延；已到期从确认时起算。</i>\n')
                if reasons:
                    await show(text+'\n🚫 <b>暂时不能续期</b>\n'+'\n'.join('· '+escape(r) for r in reasons), back)
                    return
                nonce = secrets.token_hex(12)
                snapshot = {'expiry': expiry, 'price': info['renewal_price'], 'level': info['level']['level'],
                            'revision': levels.config()['revision'], 'binding': levels.binding_revision(uid)}
                with economy_write(levels.db) as conn:
                    conn.execute('INSERT INTO activity_renew_intents VALUES(?,?,?,?,?,?,NULL)',
                                 (nonce, uid, str(actor), str(chat_id), encode(snapshot), now+300))
                await show(text+'\n请在 5 分钟内确认，确认时会重新核对余额和价格。',
                           [[{'text':f"✅ 确认续期（扣 {info['renewal_price']} 积分）", 'callback_data':'activity:confirm:'+nonce}],
                            [{'text':'✖️ 取消', 'callback_data':'activity:cancel:'+nonce}]])
                return
            if action != 'status':
                raise ValueError('操作已过期，请重新打开「等级与权益」')
            await show(self._activity_page(member, info), self._activity_menu(info))
        except ValueError as exc:
            await show('⚠️ 操作未完成：'+escape(str(exc)), back)
        except Exception:  # noqa: BLE001 - do not expose internal/transport details or invent success
            retry = [[{'text':'🔄 重试本次续期（不会重复扣分）', 'callback_data':'activity:'+action}]] if action.startswith('confirm:') else []
            await show('❓ 结果暂未确认。请点重试或联系管理员核查，不要重新发起续期。', retry+back)
