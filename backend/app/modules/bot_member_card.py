"""Sectioned member snapshots: the group account card and the /kk admin card."""
from __future__ import annotations

from html import escape
from typing import Any

from app.modules.bot_activity import beijing_stamp, expiry_line, growth_line
from app.modules.bot_views import bandwidth_value, duration, short_label
from app.modules.game_mentions import telegram_username

DEVICE_NOTE = '<i>按设备名称归并，非硬件唯一识别；空/未知按ID单计</i>'


def box(title: str, lines: list[str], *, keep: bool = False) -> str:
    """One Telegram blockquote section; an empty one is left out unless kept."""
    body = [line for line in lines if line]
    if not body and not keep:
        return ''
    return f'<blockquote><b>{title}</b>' + ''.join('\n' + line for line in body) + '</blockquote>'


def card(head: list[str], boxes: list[str]) -> str:
    return '\n'.join([*head, *[b for b in boxes if b]])


class MemberCardMixin:
    def _member_nickname(self, member: dict[str, Any], *, link: bool, fallback: str = '') -> str:
        """Telegram nickname, then @username, then the fallback; '' when no Telegram is bound."""
        tg = str(member.get('tg_user_id') or '')
        if not (tg.isascii() and tg.isdigit() and 0 < int(tg) < 2**63):
            return ''
        profile = self._tg_profiles.get(tg) or {}
        name = str(profile.get('display_name') or member.get('tg_display_name') or '').strip()
        handle = telegram_username(profile.get('username') or member.get('tg_username'))
        label = name or ('@' + handle if handle else fallback)
        if not label:
            return ''
        label = escape(short_label(label, 32))
        return f'<a href="tg://user?id={int(tg)}">{label}</a>' if link else label

    def _member_level_info(self, member: dict[str, Any]) -> dict[str, Any] | bool | None:
        """Level status; None outside the level system, False when it cannot be read."""
        levels = getattr(self._members, '_levels', None)
        if levels is None or not member.get('activity_level'):
            return None
        try:
            return levels.status(str(member['emby_user_id']))
        except Exception:  # noqa: BLE001 - shown as unavailable, never invented
            return False

    def _member_head(self, member: dict[str, Any], info: Any) -> list[str]:
        title = (info['worn_title'] or info['level']['title']) if info else None
        identity = self._group_label(member, title=title) + ' · ' + self._status_label(member)
        if member.get('activity_exempt') or (info and info['exempt']):
            term = '♾ 永久有效'
        elif 'expires_at_effective' in member or 'expires_at' in member:
            term = expiry_line(member.get('expires_at_effective', member.get('expires_at')))
        else:
            term = '⏳ 有效期暂不可用'
        return [identity, term]

    @staticmethod
    def _member_rights(member: dict[str, Any]) -> list[str]:
        streams = member.get('max_streams')
        streams = '暂不可用' if streams in (None, '') else f'{int(streams)} 路' if int(streams) else '不限'
        flags = []
        for field, icon, name in (('allow_download', '📥', '下载'), ('allow_transcode', '🎞️', '转码')):
            if field in member:
                value = member[field]
                flags.append(f'{icon} ' + (name + '暂不可用' if value is None else ('可' if value else '不可') + name))
        return [f'🚀 带宽 {bandwidth_value(member)} · 📺 同播 {streams}', ' · '.join(flags)]

    def _member_watch(self, member: dict[str, Any], *, managing: bool) -> list[str]:
        if self._stats is None or not hasattr(self._stats, 'watch_summary'):
            return ['观看统计暂不可用']
        try:
            summary = self._stats.watch_summary(str(member['emby_user_id'])) or {}
        except Exception:  # noqa: BLE001 - unavailable is not zero viewing
            return ['观看统计暂不可用']
        lines = [f"近24小时 <b>{duration(summary.get('seconds_24h'))}</b> · 近30天 <b>{duration(summary.get('seconds_30d'))}</b>",
                 f"累计 <b>{duration(summary.get('recorded_seconds'))}</b>"]
        if managing and summary.get('unverified_playback_now'):
            lines.append('当前播放待核验，暂不计时。')
        return [line.replace('本周期暂无播放记录', '暂无记录') for line in lines]

    def _member_points(self, member: dict[str, Any], info: Any, *, managing: bool) -> str:
        if not info:
            return box(f"🪙 积分 {self._balance(str(member.get('emby_user_id') or ''))}",
                       ['等级与续期信息暂不可读取' if info is False else ''], keep=True)
        lines = [growth_line(info['level']), *self._settled_reward_lines(info)]
        if managing:
            lines += self._renewal_lines(info)
        elif not info['exempt']:
            lines.append(f"🔁 续期 {info['renewal_price']} 积分 / 30 天"
                         + (f" · 还差 {info['shortfall']}" if info['shortfall'] else ''))
        return box(f"🪙 积分 {info['balance']}", lines)

    def _member_group_card(self, member: dict[str, Any]) -> str:
        """The account card a member opens in a group; only what they would show others."""
        info = self._member_level_info(member)
        account = str(member.get('username') or '—')
        name = self._member_nickname(member, link=True, fallback=account) or f'<b>{escape(account)}</b>'
        return card([(self._whitelist_decoration(member) or '👤 ') + name, *self._member_head(member, info)],
                    [box('✨ 权益', self._member_rights(member)),
                     box('🎬 观看', self._member_watch(member, managing=False)),
                     self._member_points(member, info, managing=False)])

    def _member_admin_card(self, member: dict[str, Any], *, public: bool,
                           access_lines: list[str] | None = None) -> str:
        """Everything an operator needs at once; profile details stay out of groups."""
        info = self._member_level_info(member)
        # A group card never pings the account being looked up.
        nickname = self._member_nickname(member, link=not public)
        heading = f"🛠 <b>管理目标 · {escape(str(member.get('username') or '—'))}</b>" + (f' · {nickname}' if nickname else '')
        rights = self._member_rights(member)
        count = member.get('device_count')
        rights += [f"📱 设备分组 {count if count is not None else '暂不可用'}{' 组' if count is not None else ''}", DEVICE_NOTE]
        boxes = [box('🔒 封禁', access_lines or []),
                 box('✨ 权益', rights),
                 box('🎬 观看', self._member_watch(member, managing=True)),
                 self._member_points(member, info, managing=True)]
        if not public:
            boxes.append(box('📋 资料', self._member_profile(member)))
        return card([heading, *self._member_head(member, info)], boxes)

    def _member_profile(self, member: dict[str, Any]) -> list[str]:
        uid = str(member.get('emby_user_id'))
        tg = str(member.get('tg_user_id') or '')
        handle = telegram_username(member.get('tg_username'))

        def read(lookup, shown):
            # One unreadable detail never hides the rest of the card.
            try:
                return shown(lookup())
            except Exception:  # noqa: BLE001
                return '暂不可用'
        invitees = (f"{member['invitee_count']} 人" if member.get('invitee_count') is not None else
                    read(lambda: self._members.invitees_of(uid), lambda rows: f'{len(rows or [])} 人'))
        inviter = read(lambda: self._members.inviter_of(uid), lambda row: escape(str((row or {}).get('username') or '—')))
        left = read(lambda: self._requests.remaining(uid) if self._requests_ready() else None, self._remaining_text)
        devices = read(lambda: self._members.devices(uid), lambda rows: f'{len(rows or [])} 条')
        seen = member.get('last_seen_at')
        lines = ['Telegram ' + (f'<code>{escape(tg)}</code>' + (f' · @{handle}' if handle else '') if tg else '未关联'),
                 f"注册渠道 {escape(str(member.get('register_via') or 'legacy'))} · 邀请人 {inviter} · 下级 {invitees}",
                 f"求片剩余 {left} · 最近活跃 {beijing_stamp(seen) if seen else '—'}",
                 f"原始登录标识记录 {devices}"]
        if member.get('note'):
            lines.append('📝 ' + escape(str(member['note'])))
        return lines
