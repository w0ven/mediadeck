"""Invite tree in the private Bot, and the group card that settles an inviter."""
from __future__ import annotations

import asyncio
import contextlib
import time
from html import escape
from typing import Any

from app.modules.invite_tree import (
    STATUS_LABELS,
    InviteTree,
    active_suspension,
    decide_case,
)

STATUS_ICONS = {"active": "🟢", "pending": "🕓", "expired": "⚪", "suspended": "🔴",
                "exhausted": "🔴", "deleted": "⛔"}
INVITEES_PAGE = 8


def _day(value: Any) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(int(value))) if value else '—'


def _stamp(value: Any) -> str:
    return time.strftime('%m-%d %H:%M', time.localtime(int(value))) if value else '—'


def _mention(node: dict[str, Any]) -> str:
    label = escape(str(node.get('label') or '未知账号'))
    tg = str(node.get('tg_user_id') or '')
    if tg.isascii() and tg.isdigit() and 0 < int(tg) < 2**63:
        return f'<a href="tg://user?id={int(tg)}">{label}</a>'
    return label


def _activity(node: dict[str, Any]) -> str:
    if node.get('status') == 'deleted':
        return ''
    flag = node.get('active_7d')
    return '' if flag is None else (' · 近7天活跃' if flag else ' · 近7天未观看')


class InviteBotMixin:
    def _invite_tree(self) -> InviteTree:
        return InviteTree(self._members._db, self._members)

    # -- private: who I invited ------------------------------------------------

    async def _invitees_view(self, chat_id: Any, message_id: int, member: dict[str, Any],
                             page: int = 0) -> None:
        tree = self._invite_tree()
        uid = str(member['emby_user_id'])
        nodes = await asyncio.to_thread(tree.downstream, uid, 2)
        pages = max(1, (len(nodes) + INVITEES_PAGE - 1) // INVITEES_PAGE)
        page = max(0, min(int(page), pages - 1))
        alive = sum(1 for n in nodes if n['status'] != 'deleted')
        lines = ['👥 <b>我邀请的人</b>' + (f' · 第 {page + 1}/{pages} 页' if pages > 1 else '')]
        if nodes:
            lines.append(f'直接邀请 {len(nodes)} 人 · 在册 {alive} 人 · 名下共 '
                         f'{len(nodes) + sum(n.get("descendants", 0) for n in nodes)} 人')
        paused = active_suspension(tree.db, uid)
        if paused:
            lines.append(f'⏸ 你的邀请资格暂停至 {_stamp(paused["until"])}')
        lines.append('')
        for node in nodes[page * INVITEES_PAGE:(page + 1) * INVITEES_PAGE]:
            status = STATUS_LABELS.get(node['status'], node['status'])
            if node['status'] == 'deleted':
                lines.append(f"⛔ {_mention(node)} · 已删除 {_day(node.get('deleted_at'))}")
            else:
                lines.append(f"{STATUS_ICONS.get(node['status'], '▫️')} {_mention(node)} · {status}{_activity(node)}")
                expiry = _day(node.get('expires_at')) if node.get('expires_at') else '永久'
                lines.append(f"    注册 {_day(node.get('registered_at'))} · 到期 {expiry}")
            for child in node.get('children') or []:
                child_status = STATUS_LABELS.get(child['status'], child['status'])
                lines.append(f"    └ {STATUS_ICONS.get(child['status'], '▫️')} {_mention(child)} · {child_status}{_activity(child)}")
        if not nodes:
            lines.append('📭 你还没有邀请过别人。')
        lines += ['', ('<i>你邀请的人如果被管理员删号或因违规被删号，管理员可能连带处理你的账号，'
                       '或暂停你的邀请资格。请只邀请你信得过的人。</i>')]
        keyboard = []
        pager = []
        if page > 0:
            pager.append({'text': '◀ 上一页', 'callback_data': f'invitees:{page - 1}'})
        if page + 1 < pages:
            pager.append({'text': '下一页 ▶', 'callback_data': f'invitees:{page + 1}'})
        if pager:
            keyboard.append(pager)
        keyboard.append([{'text': '◀ 返回邀请', 'callback_data': 'invites'}])
        await self._edit(chat_id, message_id, '\n'.join(lines), keyboard)

    # -- group card ------------------------------------------------------------

    def _invite_case_target(self) -> tuple[str, int | None]:
        cfg = self._cfg()
        chat = str(cfg.get('registration_notify_chat_id') or '').strip()
        if chat:
            thread = cfg.get('registration_notify_thread_id')
            return chat, int(thread) if thread else None
        chats = list(self._group_allowlist()) if hasattr(self, '_group_allowlist') else []
        return (str(chats[0]), None) if chats else ('', None)

    @staticmethod
    def _invite_case_text(case: dict[str, Any], *, confirming: bool = False) -> str:
        deleted, inviter = case.get('deleted') or {}, case.get('inviter') or {}
        level = inviter.get('level') or {}
        rank = f" · {escape(str(level.get('icon') or ''))} Lv{level['level']}" if level.get('level') else ''
        status = STATUS_LABELS.get(inviter.get('status'), inviter.get('status') or '')
        lines = [f"⚠️ <b>邀请连带处理</b> · #{case['id']}", '',
                 f"被删账号：{_mention(deleted)}",
                 f"删除原因：{escape(str(case.get('source_label') or ''))}",
                 f"邀请人：{_mention(inviter)}{rank} · {escape(status)}"]
        if inviter.get('invitees') is not None:
            lines.append(f"邀请人名下：共 {inviter['invitees']} 人")
        state = case.get('status')
        when = _stamp(case.get('decided_at'))
        who = str(case.get('decided_by') or '')
        who = escape(who[3:]) if who.startswith('tg:') else '管理员'
        if state == 'deleted':
            lines += ['', f'🗑 <b>已连带删除邀请人</b>（{who} · {when}）']
        elif state == 'suspended':
            until = _stamp(case.get('suspended_until')) if case.get('suspended_until') else '—'
            lines += ['', f'⏸ <b>已暂停邀请资格至 {until}</b>（{who} · {when}）']
        elif state == 'ignored':
            lines += ['', f'✖ <b>不处理</b>（{who} · {when}）']
        elif state == 'gone':
            lines += ['', '邀请人已不在系统中，无需处理。']
        elif confirming:
            lines += ['', f"⚠️ 确认删除邀请人 {_mention(inviter)}？",
                      '其 Emby 账号会一并删除，<b>不可恢复</b>。']
        else:
            if case.get('result'):
                lines += ['', '⚠️ ' + escape(str(case['result']))]
            lines += ['', '请管理员选择如何处理邀请人：']
        return '\n'.join(lines)

    @staticmethod
    def _invite_case_keyboard(case: dict[str, Any], *, confirming: bool = False) -> list:
        if case.get('status') not in ('pending', 'processing'):
            return []
        cid = case['id']
        if confirming:
            return [[{'text': '⚠️ 确认删除', 'callback_data': f'ic:delok:{cid}'},
                     {'text': '◀ 返回', 'callback_data': f'ic:back:{cid}'}]]
        return [[{'text': '🗑 连带删除邀请人', 'callback_data': f'ic:del:{cid}'}],
                [{'text': '⏸ 暂停邀请 30 天', 'callback_data': f'ic:sus:{cid}'},
                 {'text': '✖ 不处理', 'callback_data': f'ic:ign:{cid}'}]]

    async def _edit_invite_case(self, case: dict[str, Any], *, confirming: bool = False) -> None:
        if not case.get('chat_id') or not case.get('message_id'):
            return
        await self._call('editMessageText', {
            'chat_id': case['chat_id'], 'message_id': int(case['message_id']),
            'text': self._invite_case_text(case, confirming=confirming), 'parse_mode': 'HTML',
            'disable_web_page_preview': True,
            'reply_markup': {'inline_keyboard': self._invite_case_keyboard(case, confirming=confirming)}})

    async def refresh_invite_case(self, case_id: int) -> None:
        with contextlib.suppress(Exception):
            case = await asyncio.to_thread(self._invite_tree().case, case_id)
            if case:
                await self._edit_invite_case(case)

    async def _invite_case_callback(self, data: str, chat_id: Any, message_id: Any,
                                    callback_id: str, tg_user_id: str, tg_name: str) -> None:
        parts = data.split(':')
        if len(parts) != 3 or not parts[2].isdigit():
            await self._answer_callback(callback_id)
            return
        action, case_id = parts[1], int(parts[2])
        admin = self._member_for_chat(tg_user_id)
        if not self.is_admin(admin):
            await self._answer_callback(callback_id, '只有管理员可以处理。')
            return
        tree = self._invite_tree()
        case = await asyncio.to_thread(tree.case, case_id)
        if (not case or str(case.get('chat_id')) != str(chat_id)
                or int(case.get('message_id') or 0) != int(message_id)):
            await self._answer_callback(callback_id, '这张卡片已失效，请到 Deck 处理。')
            return
        if case['status'] not in ('pending', 'processing'):
            await self._answer_callback(callback_id, '已处理过。')
            await self._edit_invite_case(case)
            return
        if action in ('del', 'back'):
            await self._answer_callback(callback_id)
            await self._edit_invite_case(case, confirming=action == 'del')
            return
        decision = {'delok': 'delete', 'sus': 'suspend', 'ign': 'ignore'}.get(action)
        if not decision:
            await self._answer_callback(callback_id)
            return
        await self._answer_callback(callback_id, '处理中…')
        actor = 'tg:' + (str(admin.get('tg_display_name') or '').strip() or tg_name or str(admin.get('username') or ''))

        def authorized() -> bool:
            return self.is_admin(self._member_for_chat(tg_user_id))
        try:
            result = await decide_case(tree, self._emby, case_id, decision, actor=actor, authorize=authorized)
        except Exception:  # noqa: BLE001 - the card shows the reopened case, not a guess
            result = await asyncio.to_thread(tree.case, case_id) or case
        await self._edit_invite_case(result)

    # -- delivery ----------------------------------------------------------------

    def announce_invite_cases(self) -> None:
        if not self.enabled or self._db is None:
            return
        task = asyncio.create_task(self._flush_invite_cases())
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _flush_invite_cases(self) -> None:
        lock = self.__dict__.setdefault('_invite_case_lock', asyncio.Lock())
        async with lock:
            tree = self._invite_tree()
            rows = await asyncio.to_thread(
                tree.db.query, "SELECT id,status FROM invite_cases WHERE delivery='pending' ORDER BY id LIMIT 20")
            for row in rows:
                if row['status'] not in ('pending', 'processing'):
                    await asyncio.to_thread(tree.db.execute,
                        "UPDATE invite_cases SET delivery='skipped' WHERE id=?", (row['id'],))
                    continue
                chat, thread = self._invite_case_target()
                if not chat:
                    await asyncio.to_thread(tree.db.execute,
                        "UPDATE invite_cases SET delivery='skipped',delivery_error=? WHERE id=?",
                        ('未配置通知群，请在 Deck 处理', row['id']))
                    continue
                changed = await asyncio.to_thread(tree.db.execute,
                    "UPDATE invite_cases SET delivery='sending' WHERE id=? AND delivery='pending'", (row['id'],))
                if not changed:
                    continue
                case = await asyncio.to_thread(tree.case, row['id'])
                mid = None
                with contextlib.suppress(Exception):
                    mid = await self.send_message(chat, self._invite_case_text(case),
                                                  self._invite_case_keyboard(case), thread_id=thread)
                await asyncio.to_thread(tree.db.execute,
                    "UPDATE invite_cases SET delivery=?,chat_id=?,thread_id=?,message_id=?,delivery_error=? WHERE id=?",
                    ('sent' if mid else 'failed', chat, thread, mid,
                     '' if mid else '群消息未确认送达，请在 Deck 处理', row['id']))

    async def _invite_case_worker(self) -> None:
        tree = self._invite_tree()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(tree.recover)
            # A send that lost its acknowledgement is never repeated automatically.
            await asyncio.to_thread(tree.db.execute,
                "UPDATE invite_cases SET delivery='failed',delivery_error='发送被中断，请在 Deck 处理' "
                "WHERE delivery='sending'")
        while True:
            with contextlib.suppress(Exception):
                await self._flush_invite_cases()
            await asyncio.sleep(30)
