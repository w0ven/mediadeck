"""Private request-center panels. Persisted card-bound capabilities, no TG side effects on import."""

from __future__ import annotations

import json
import secrets
import time
from html import escape

from app.modules.bot_views import short_label
from app.modules.requests import (
    STATUS_LABELS,
    RequestError,
    display_title,
    normalize_demand,
    number_ranges,
    numbers,
)
from app.modules.tmdb import parse_link, poster_url

ACCEPT_NOTICE = "已接受，等待下载并入库。"


def button(label, action):
    return (label, action)


class RequestBotMixin:
    def _requests_ready(self):
        return self._requests is not None

    @staticmethod
    def _remaining_text(left):
        return "不限" if left is None else f"{left} 次"

    @staticmethod
    def _rq_staff(member):
        return bool(set((member or {}).get("roles") or []) & {"uploader", "admin"})

    @property
    def _rq_db(self):
        return self._requests._db

    def _rq_clear_input(self, chat):
        if self._requests_ready() and getattr(self._requests, "_db", None):
            self._rq_db.execute("DELETE FROM request_inputs WHERE chat_id=?", (str(chat),))

    def _rq_abandon(self, chat):
        self._rq_clear_input(chat)
        if self._requests_ready() and getattr(self._requests, "_db", None):
            for card in self._rq_db.query(
                "SELECT token,payload FROM request_cards WHERE chat_id=?", (str(chat),)
            ):
                if json.loads(card["payload"]).get("draft"):
                    self._rq_db.execute("DELETE FROM request_cards WHERE token=?", (card["token"],))

    def _rq_card_view(self, card, payload=None):
        """Presentation provenance is not the member's current role.

        Legacy own-ticket cards mixed both roles and have no reliable provenance;
        recover them as user cards. Management can be reopened from the workbench.
        """
        p = payload if payload is not None else json.loads(card["payload"])
        if p.get("view") in ("user", "staff"):
            return p["view"]
        if p.get("mode") == "staff":
            return "staff"
        if p.get("mode") in ("mine", "follow") or p.get("draft"):
            return "user"
        rid = card.get("request_id")
        row = self._requests.get(rid) if rid else None
        if (
            row
            and row["emby_user_id"] != card["user_id"]
            and set(json.loads(card["actions"])) & {"accept", "reject", "internal", "correct"}
        ):
            return "staff"
        return "user"

    async def _rq_render(
        self,
        chat,
        mid,
        member,
        body,
        rows,
        payload=None,
        *,
        rid=None,
        revision=None,
        photo="",
        input_kind=None,
        refresh_only=False,
    ):
        """Rotate capabilities only after successful edit. An old token cannot act on a new card."""
        payload = dict(payload or {})
        payload.setdefault("view", "staff" if payload.get("mode") == "staff" else "user")
        token = secrets.token_hex(8)
        actions, keyboard = [], []
        for row in rows:
            keys = []
            for item in row:
                if isinstance(item, dict):
                    keys.append(item)
                else:
                    label, action = item
                    actions.append(action)
                    keys.append({"text": label, "callback_data": f"rq:{token}:{action}"})
            keyboard.append(keys)
        uid = str(member["emby_user_id"])
        existing = self._rq_db.one(
            "SELECT * FROM request_cards WHERE chat_id=? AND message_id=?",
            (str(chat), int(mid or 0)),
        )
        old_photo = (
            bool(existing and existing["photo"]) or (str(chat), int(mid or 0)) in self._photo_panels
        )
        is_photo = bool(photo) or old_photo
        # Telegram captions have a hard 1024-character ceiling. Full requirements
        # are never truncated; a long detail transitions to a text panel once.
        if len(body.encode('utf-16-le')) // 2 > 1000:
            photo, is_photo = "", False
        markup = {"inline_keyboard": keyboard}
        result = None
        if mid and is_photo:
            method = "editMessageMedia" if photo else "editMessageCaption"
            data = {"chat_id": chat, "message_id": mid, "reply_markup": markup}
            if photo:
                data["media"] = {
                    "type": "photo",
                    "media": photo,
                    "caption": body,
                    "parse_mode": "HTML",
                }
            else:
                data.update(caption=body, parse_mode="HTML")
            result = await self._call(method, data)
            if result is None and "not modified" not in str(self._last_error).lower():
                # Poster fetch failure must not prevent submitting a known TMDB ID.
                if not old_photo:
                    is_photo = False
                    result = await self._edit(chat, mid, body + "\n（海报暂不可用）", keyboard)
                else:
                    # A stale poster URL is irrelevant to a status-only refresh.
                    result = await self._call('editMessageCaption', {
                        'chat_id': chat, 'message_id': mid, 'caption': body,
                        'parse_mode': 'HTML', 'reply_markup': markup,
                    })
                    if not result and 'not modified' not in str(self._last_error).lower():
                        if refresh_only:
                            return None
                        replacement = await self._rq_render(
                            chat,
                            None,
                            member,
                            body + "\n（海报暂不可用）",
                            rows,
                            payload,
                            rid=rid,
                            revision=revision,
                            input_kind=input_kind,
                        )
                        if replacement:
                            await self._call(
                                "editMessageReplyMarkup",
                                {
                                    "chat_id": chat,
                                    "message_id": mid,
                                    "reply_markup": {"inline_keyboard": []},
                                },
                            )
                            self._rq_db.execute(
                                "DELETE FROM request_cards WHERE chat_id=? AND message_id=?",
                                (str(chat), int(mid)),
                            )
                        return replacement
        elif mid and not old_photo:
            result = await self._edit(chat, mid, body, keyboard)
        else:
            if refresh_only:
                return None  # keep the original locator and retryable refresh receipt
            if photo and is_photo:
                result = await self._call(
                    "sendPhoto",
                    {
                        "chat_id": chat,
                        "photo": photo,
                        "caption": body,
                        "parse_mode": "HTML",
                        "reply_markup": markup,
                    },
                )
                new_mid = (result or {}).get("message_id") if isinstance(result, dict) else None
            else:
                new_mid = None
            if not new_mid:
                new_mid = await self.send_message(chat, body, keyboard)
                is_photo = False
            if not new_mid:
                return None
            if mid:
                await self._call(
                    "editMessageReplyMarkup",
                    {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": []}},
                )
                self._rq_db.execute(
                    "DELETE FROM request_cards WHERE chat_id=? AND message_id=?",
                    (str(chat), int(mid)),
                )
            mid, result = new_mid, True
        if not result and "not modified" not in str(self._last_error).lower():
            return None
        expiry = int(time.time()) + (1200 if input_kind or (payload or {}).get("draft") else 86400)
        with self._rq_db.write() as c:
            c.execute(
                "INSERT OR REPLACE INTO request_cards VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    token,
                    str(chat),
                    int(mid),
                    uid,
                    json.dumps(payload or {}, ensure_ascii=False),
                    json.dumps(actions),
                    rid,
                    revision,
                    int(is_photo),
                    expiry,
                ),
            )
            if existing:
                c.execute(
                    "DELETE FROM request_inputs WHERE chat_id=? AND token=?",
                    (str(chat), existing["token"]),
                )
            if input_kind:
                c.execute("DELETE FROM request_inputs WHERE chat_id=?", (str(chat),))
                c.execute(
                    "INSERT INTO request_inputs VALUES(?,?,?,?,?)",
                    (str(chat), uid, token, input_kind, expiry),
                )
        if self._in_bound_chat(chat):
            self._touch_panel(chat, int(mid))
            self._remember_menu(chat, int(mid), keyboard)
        if is_photo:
            self._photo_panels.add((str(chat), int(mid)))
        else:
            self._photo_panels.discard((str(chat), int(mid)))
        return int(mid)

    async def _rq_home(self, chat, mid, member):
        if not self._requests_ready() or not member:
            await self._show(chat, "求片功能未开启或当前 Telegram 未绑定账号。")
            return
        self._pending.pop(self._pkey(chat), None)
        left = self._requests.remaining(member["emby_user_id"])
        rows = [
            [button("➕ 发起求片", "new")],
            [button("我的求片", "list:mine"), button("我的关注", "list:follow")],
        ]
        if self._rq_staff(member):
            rows[0].append(button("📥 上片员工作台", "list:staff"))
        rows.append([{"text": "◀ 主菜单", "callback_data": "home"}])
        await self._rq_render(
            chat,
            mid,
            member,
            f"🎬 <b>求片中心</b>\n本月剩余：{'不限' if left is None else str(left) + ' 次'}\n提交扣 1 次 · 关注免费",
            rows,
        )

    async def _request_start(self, chat_id, message_id, member):
        if not self._requests_ready() or not member:
            return await self._rq_home(chat_id, message_id, member)
        await self._rq_render(
            chat_id,
            message_id,
            member,
            "🔎 <b>查找影片</b>\n发送片名、TMDB 链接或编号。\n编号需选择电影 / 剧集。\n搜索与关注免费。",
            [[button("◀ 求片中心", "home")]],
            {"draft": True},
            input_kind="search",
        )

    async def _my_requests(self, chat_id, message_id, member):
        await self._rq_list(chat_id, message_id, member, {"mode": "mine", "status": "", "page": 0})

    async def _rq_list(self, chat, mid, member, p):
        p = dict(p)
        mode = p.get("mode", "mine")
        if mode == "staff" and not self._rq_staff(member):
            raise RequestError("你不是上片员")
        page = max(0, int(p.get("page", 0)))
        status = p.get("status", "open" if mode == "staff" else "")
        rows = self._requests.list(
            status=status, limit=6, offset=page * 5,
            user_id=None if mode == "staff" else member["emby_user_id"],
            followed=mode == "follow", media_type=p.get("type", ""), search=p.get("search", ""),
        )
        title = {"mine": "我的求片", "follow": "我的关注", "staff": "上片员工作台"}[mode]
        group = "待处理" if status == "open" else "已处理" if status else "全部工单"
        text = [f"📋 <b>{title} · {group}</b>\n第 {page + 1} 页",
                f"状态：{STATUS_LABELS.get(status, '全部')} · 类型："
                + {"movie": "电影", "tv": "剧集"}.get(p.get("type"), "全部")]
        if p.get("search"):
            text.append("搜索：" + escape(p["search"]))
        keys = []
        for row in rows[:5]:
            state = '待处理' if row['status'] == 'open' else '已处理 · ' + row['status_label']
            text.append(f"<b>{state}</b> · #{row['id']}\n"
                        + escape(short_label(row['display_title'], 48))
                        + '\n' + row['media_label']
                        + (' · ' + self._rq_season_label(row['demand'])
                           if row['media_type'] == 'tv' and row['demand']['scope'] in ('series', 'season') else '')
                        + ('\n求片人：' + escape(row['username']) if mode == 'staff' else ''))
            label = f"#{row['id']} {row['display_title']} · {row['status_label']}"
            keys.append([button(short_label(label, 55), f"view:{row['id']}")])
        if not rows:
            text.append("暂无符合条件的工单。")
        nav = []
        if page:
            nav.append(button("上一页", "page:-1"))
        if len(rows) > 5:
            nav.append(button("下一页", "page:1"))
        if nav:
            keys.append(nav)
        keys.append([button("筛选 / 搜索", "filters")])
        keys.append([button("◀ 求片中心", "home")])
        p.update(mode=mode, status=status, page=page, view="staff" if mode == "staff" else "user")
        return await self._rq_render(chat, mid, member, "\n\n".join(text), keys, p)

    async def _rq_filters(self, chat, mid, member, p, *, candidates=False):
        if candidates:
            rows = [[button("电影", "searchtype:movie"), button("剧集", "searchtype:tv"),
                     button("全部类型", "searchtype:")], [button("按年份筛选", "year")]]
            if p.get("year") or p.get("type"):
                rows[1].append(button("清除筛选", "searchclear"))
            rows.append([button("◀ 候选影片", "candidates")])
        else:
            rows = [[button("待处理", "status:open"), button("已接受", "status:accepted"),
                     button("已拒绝", "status:rejected")],
                    [button("已撤回", "status:cancelled"), button("全部状态", "status:"),
                     button("类型", "filtertypes")], [button("搜索工单", "find")]]
            default = "open" if p.get("mode") == "staff" else ""
            if p.get("status", default) != default or p.get("type") or p.get("search"):
                rows[-1].append(button("清除筛选", "listclear"))
            rows[-1].append(button("◀ 工单列表", "listback"))
        return await self._rq_render(chat, mid, member, "🔎 <b>筛选与搜索</b>", rows, p)

    async def _rq_candidates(self, chat, mid, member, p, *, fetch=False):
        if fetch:
            result = (
                await self._tmdb.search(
                    p["query"], p.get("page", 1), p.get("type", ""), p.get("year")
                )
                if self._tmdb
                else {"available": False}
            )
            if not result.get("available"):
                return await self._rq_render(
                    chat,
                    mid,
                    member,
                    "片名搜索暂不可用（TMDB 未配置或查询失败）。可重试，或返回用明确类型的 TMDB 链接/编号提交。",
                    [[button("重试", "searchretry"), button("重新输入", "new")]],
                    p,
                )
            p.update(results=result["results"], total_pages=result["total_pages"], index=0)
        results = p.get("results", [])
        index = max(0, min(int(p.get("index", 0)), len(results) - 1))
        p["index"] = index
        rows = []
        photo = ""
        if results:
            item = results[index]
            photo = poster_url(item.get("poster_path", ""))
            body = (
                f"🎞 {self._rq_title(item)}\n"
                f"{'电影' if item['media_type'] == 'movie' else '剧集'} · TMDB {item['tmdb_id']}\n"
                f"搜索：{escape(p['query'])}\n"
                f"第 {p.get('page', 1)}/{p.get('total_pages', 1)} 页 · 候选 {index + 1}/{len(results)}\n"
                + escape(str(item.get("overview") or "")[:120])
            )
            rows.insert(0, [button("选择这部", "pick")])
        else:
            body = "本页没有匹配的电影/剧集，可翻页或修改类型/年份。"
        nav = []
        if index > 0 or p.get("page", 1) > 1:
            nav.append(button("◀ 上一候选", "candidate:-1"))
        if index + 1 < len(results) or p.get("page", 1) < p.get("total_pages", 0):
            nav.append(button("下一候选 ▶", "candidate:1"))
        if nav:
            rows.append(nav)
        rows.append([button("筛选", "searchfilters"), button("◀ 重新搜索", "new")])
        await self._rq_render(chat, mid, member, body, rows, p, photo=photo)

    async def _rq_selected(self, chat, mid, member, p):
        lookup = getattr(self._tmdb, "lookup_request", getattr(self._tmdb, "lookup", None))
        meta = await lookup(p["media_type"], p["tmdb_id"]) if lookup else None
        p["meta"] = meta or p.get("meta") or {}
        p.update(
            draft=True,
            simple=True,
            demand=normalize_demand(p["media_type"], {}),
            note="",
            create_key=secrets.token_hex(16),
        )
        p["library"] = await self._requests.library_hint(
            p["media_type"], p["tmdb_id"], member["emby_user_id"]
        )
        p["history"] = self._requests.history(p["media_type"], p["tmdb_id"])
        await self._rq_preview(chat, mid, member, p)

    def _rq_library(self, hint):
        if not hint.get("available"):
            return "⚠ 媒体库暂不可用，未知是否已有。\n仍可确认提交。", []
        if not hint.get("items"):
            return "媒体库未找到匹配内容。", []
        lines, links = ["媒体库已有："], []
        for item in hint["items"][:3]:
            label = str(item.get("name") or item.get("id"))
            eps = item.get("episodes") or []
            suffix = (
                " · " + ", ".join(f"S{e['season']}E{e['episode']}" for e in eps[:12]) if eps else ""
            )
            if len(eps) > 12 or item.get("partial"):
                suffix += " …（详见媒体库）"
            lines.append(escape(label + suffix))
            if str(item.get("url", "")).startswith(("https://", "http://")):
                lines.append(f'<a href="{escape(item["url"], quote=True)}">打开媒体库</a>')
        return "\n".join(lines), links

    async def _rq_legacy_draft(self, chat, mid, member, p):
        """Never execute a pre-simplification draft under a different meaning."""
        if p.get("editing"):
            return await self._rq_detail(
                chat,
                mid,
                member,
                int(p["editing"]),
                full=True,
                prefix="原要求未更改；Bot 沟通已停用。",
            )
        p.update(simple=True, demand=normalize_demand(p["media_type"], {}), note="")
        return await self._rq_preview(
            chat, mid, member, p, notice="流程已简化，本次尚未提交；请按当前影片和季范围重新确认。"
        )

    @staticmethod
    def _rq_season_label(demand):
        if demand.get("scope") == "series":
            return "全部季"
        return "第" + number_ranges(demand.get("seasons") or []).replace(",", "、") + "季"

    async def _rq_preview(self, chat, mid, member, p, *, entering_seasons=False, notice=""):
        p["demand"] = normalize_demand(p["media_type"], p["demand"])
        meta = dict(p.get("meta") or {}, tmdb_id=p["tmdb_id"], media_type=p["media_type"])
        body = (
            f"🎬 <b>确认求片</b>\n{self._rq_title(meta)}\n"
            f"{'电影' if p['media_type'] == 'movie' else '剧集'}"
        )
        if p["media_type"] == "tv":
            body += "\n季范围：" + self._rq_season_label(p["demand"])
        if p.get("history"):
            body += (
                "\n\n已接受工单（"
                + ",".join("#" + str(r["id"]) for r in p["history"])
                + "），等待下载入库。"
            )
        hint, _links = self._rq_library(p.get("library") or {})
        body += "\n\n" + hint
        keys = []
        if entering_seasons:
            body += (
                "\n\n请发送季号，例如 1,3,5-7（0 为特别篇），下一步确认。"
            )
        else:
            same = self._requests.same_demand(p["media_type"], p["tmdb_id"], p["demand"])
            if same:
                return await self._rq_detail(chat, mid, member, same["id"], discovery=True)
            else:
                left = self._requests.remaining(member["emby_user_id"])
                body += f"\n\n确认求片扣 1 次\n本月剩余：{'不限' if left is None else str(left) + ' 次'}\n提交后为待处理。"
                keys.append([button("确认（扣 1 次）", "submit")])
        if p["media_type"] == "tv":
            checked = "☑ " if p["demand"]["scope"] == "series" else "☐ "
            keys.append(
                [
                    button(checked + "全部季", "allseasons"),
                    button(
                        "修改指定季" if p["demand"]["scope"] == "season" else "指定季",
                        "selectseasons",
                    ),
                ]
            )
        if entering_seasons:
            keys.append([button("返回确认卡", "preview")])
        if notice:
            body += "\n\n" + escape(notice)
        keys += [[button("◀ 重新选片", "new")]]
        return await self._rq_render(
            chat,
            mid,
            member,
            body,
            keys,
            p,
            photo=poster_url(meta.get("poster_path", "")),
            input_kind="simple_seasons" if entering_seasons else None,
        )

    @staticmethod
    def _rq_title(row):
        title = escape(display_title(row))
        ident = str(row.get("tmdb_id") or "")
        kind = row.get("media_type")
        if kind in ("movie", "tv") and ident.isascii() and ident.isdigit() and int(ident) > 0:
            return f'<a href="https://www.themoviedb.org/{kind}/{ident}">{title}</a>'
        return title

    def _rq_result_text(self, row, *, event_id=None, snapshot=False):
        # Corrections may leave claimed_by pointing to an earlier processor.
        # Read the latest authoritative status event, never infer a new actor.
        if snapshot:
            # The outbox identifies the exact committed event. Never borrow a
            # later processor/time for an older status-only legacy payload.
            event = self._rq_db.one(
                'SELECT * FROM request_events WHERE id=? AND request_id=?',
                (event_id, row['id']),
            ) if event_id is not None else None
        else:
            event = self._rq_db.one(
                "SELECT * FROM request_events WHERE request_id=? "
                "AND kind IN ('accepted','rejected','cancelled','correction') ORDER BY id DESC LIMIT 1",
                (row["id"],),
            )
        if not event or not (event["kind"] == row["status"] or
                             (event["kind"] == "correction" and
                              f" -> {row['status']}:" in event["body"])):
            return "处理人 / 时间：暂不可用"
        actor = self._members.get(event["actor"]) or {}
        name = actor.get("username") or event["actor"] or "未知"
        stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(event['created_at']))
        return f"处理人：{escape(name)}\n处理时间：{stamp}"

    def _rq_body(self, row, *, compact=False, requirements=True, event_id=None, snapshot=False):
        if compact:
            row = dict(row, title=short_label(row['title'], 40) if row['title'] else '',
                       username=short_label(row['username'], 30),
                       result_note=short_label(row['result_note'], 80) if row['result_note'] else '',
                       note='', demand_text='')
        status = {"open": "🟠 待处理", "accepted": "✅ 已处理 · 已接受",
                  "rejected": "⛔ 已处理 · 已拒绝", "cancelled": "↩ 已处理 · 已撤回"}[row["status"]]
        body = f"<b>{status}</b>\n{self._rq_title(row)}\n{row['media_label']} · #{row['id']}\n"
        body += "求片人：" + escape(row["username"])
        demand = "\n".join(line for line in row["demand_text"].splitlines()
                           if not line.endswith("：无要求"))
        if requirements and not compact and row["media_type"] == "tv" and row["demand"]["scope"] in ("series", "season"):
            demand = "季范围：" + self._rq_season_label(row["demand"])
        if requirements and demand:
            body += "\n" + escape(demand)
        if requirements and row["note"]:
            body += "\n备注：" + escape(row["note"])
        if row["status"] != "open" or snapshot:
            body += "\n\n" + self._rq_result_text(row, event_id=event_id, snapshot=snapshot)
        if row["status"] == "accepted":
            body += "\n" + ACCEPT_NOTICE
        if row["status"] == "rejected" and row["result_note"]:
            body += "\n原因：" + escape(row["result_note"])
        if compact:
            body += '\n完整信息请从工单列表重新打开。'
        return body

    def _rq_result_notice(self, row, payload):
        # State and rejection reason belong to this queued event, not the
        # request's mutable current state. Creation/quota/outbox keys stay intact.
        snapshot = dict(row, status=payload['status'], result_note=payload.get('note') or '')
        body = self._rq_body(snapshot, requirements=False,
                             event_id=payload.get('event_id'), snapshot=True)
        if payload.get('correction'):
            if snapshot['status'] == 'open':
                body = body.replace('<b>🟠 待处理</b>', '<b>🟠 状态已纠正 · 待处理</b>', 1)
            body += '\n管理员已纠正工单状态。'
        if row['status'] != snapshot['status']:
            body += '\n状态已有更新，请查看工单。'
        return body

    async def _rq_detail(
        self, chat, mid, member, rid, *, prefix="", thread=None, full=False, view="user",
        discovery=False, refresh_only=False,
    ):
        staff = view == "staff"
        row = (self._requests.get(rid) if discovery and not staff else
               self._requests.authorize(rid, member["emby_user_id"], staff=staff))
        if not row:
            raise RequestError("求片记录不存在")
        own = row["emby_user_id"] == member["emby_user_id"]
        p = {"rid": rid, "view": view, "discovery": discovery}
        keys = []
        body = self._rq_body(row)
        if mid and row['status'] != 'open':
            refresh_only = True
            old = self._rq_db.one('SELECT photo FROM request_cards WHERE chat_id=? AND message_id=?',
                                  (str(chat), int(mid)))
            if old and old['photo'] and len(body.encode('utf-16-le')) // 2 > 900:
                body = self._rq_body(row, compact=True)
        if prefix:
            body += "\n\n" + escape(prefix)
        if row["status"] == "open":
            if staff:
                keys.append([button("✅ 接受请求", "accept"), button("拒绝请求", "reject")])
            elif own:
                keys.append([button("撤回求片", "cancel")])
            elif discovery:
                p["same"] = rid
                keys.append([button("免费关注", "follow")])
        keys.append([button("◀ 返回工作台" if staff else "◀ 返回求片中心" if discovery
                            else "◀ 我的求片" if own else "◀ 我的关注",
                            "list:staff" if staff else "home" if discovery else
                            "list:mine" if own else "list:follow")])
        return await self._rq_render(chat, mid, member, body, keys, p, rid=rid,
                                     revision=row["revision"], photo=poster_url(row["poster_path"]),
                                     refresh_only=refresh_only)

    async def _rq_prompt(
        self, chat, mid, member, p, kind, body, keys=None, *, rid=None, revision=None
    ):
        return await self._rq_render(
            chat,
            mid,
            member,
            body + "\n\n发送 /cancel 放弃本次输入。",
            keys
            or (
                [
                    [
                        button("◀ 返回工单", f"view:{rid}"),
                    ]
                ]
                if rid
                else [[button("求片中心", "home")]]
            ),
            p,
            input_kind=kind,
            rid=rid,
            revision=revision,
        )

    async def _rq_callback(self, data, chat, mid, callback_id, member):
        acked = False
        try:
            if not self._requests_ready() or not member:
                raise RequestError("当前账号未绑定或求片未开启")
            _, token, action = data.split(":", 2)
            card = self._rq_db.one("SELECT * FROM request_cards WHERE token=?", (token,))
            if not card:
                current = self._rq_db.one(
                    'SELECT * FROM request_cards WHERE chat_id=? AND message_id=? AND user_id=?',
                    (str(chat), int(mid), member['emby_user_id']),
                )
                row = self._requests.get(current['request_id']) if current and current['request_id'] else None
                if row and row['status'] != 'open':
                    self._requests.authorize(row['id'], member['emby_user_id'],
                                             staff=self._rq_card_view(current) == 'staff')
                    await self._answer_callback(callback_id, short_label(row['status_label'] + '；未重复执行。'
                                                + self._rq_result_text(row).replace('\n', ' · '), 180))
                    return
            if (
                not card
                or card["chat_id"] != str(chat)
                or card["message_id"] != int(mid)
                or card["user_id"] != member["emby_user_id"]
                or card["expires_at"] < time.time()
                or action not in json.loads(card["actions"])
            ):
                raise RequestError("这张卡片已过期或不属于你，请从 /requests 或 /uploader 重新打开")
            p = json.loads(card["payload"])
            p["view"] = self._rq_card_view(card, p)
            self._rq_clear_input(chat)
            await self._answer_callback(callback_id)
            acked = True
            await self._rq_action(action, chat, mid, member, p, card)
        except (RequestError, ValueError, KeyError) as exc:
            if not acked:
                await self._answer_callback(
                    callback_id, str(exc) if isinstance(exc, RequestError) else "操作参数无效"
                )
            # A validated callback is already acked; show the actionable error on
            # its own card, never borrow a newer draft or execute old semantics.
            if (
                isinstance(exc, RequestError)
                and "card" in locals()
                and card
                and card["chat_id"] == str(chat)
                and card["message_id"] == int(mid)
                and member
                and card["user_id"] == member["emby_user_id"]
            ):
                await self._rq_render(
                    chat, mid, member, "⚠ " + escape(str(exc)), [[button("求片中心", "home")]]
                )

    async def _rq_action(self, action, chat, mid, member, p, card):
        uid = member["emby_user_id"]
        cmd, _, arg = action.partition(":")
        if cmd in ("ask", "reply", "internal", "thread", "correct", "refund", "retry", "modify"):
            raise RequestError("此 Bot 操作已停用，请重新打开工单；管理低频操作请使用 Web 后台")
        if cmd == "searchfilters":
            return await self._rq_filters(chat, mid, member, p, candidates=True)
        if cmd == "filters":
            return await self._rq_filters(chat, mid, member, p)
        if cmd == "filtertypes":
            return await self._rq_render(chat, mid, member, "影片类型", [
                [button("电影", "filter:movie"), button("剧集", "filter:tv"),
                 button("全部类型", "filter:")], [button("◀ 筛选", "filters")]], p)
        if cmd == "candidates":
            return await self._rq_candidates(chat, mid, member, p)
        if cmd == "searchclear":
            p.update(type="", year=None, page=1)
            return await self._rq_candidates(chat, mid, member, p, fetch=True)
        if cmd == "home":
            return await self._rq_home(chat, mid, member)
        if cmd == "new":
            return await self._request_start(chat, mid, member)
        if cmd in ("list", "status", "page", "filter", "find", "listback", "listclear"):
            if cmd == "list":
                p = {"mode": arg, "status": "open" if arg == "staff" else "", "page": 0}
            elif cmd == "listclear":
                p.update(status="open" if p.get("mode") == "staff" else "", type="", search="", page=0)
            elif cmd == "listback":
                pass
            elif cmd == "status":
                p.update(status=arg, page=0)
            elif cmd == "page":
                p["page"] = int(p.get("page", 0)) + int(arg)
            elif cmd == "filter":
                p.update(type=arg, page=0)
            else:
                return await self._rq_prompt(
                    chat,
                    mid,
                    member,
                    p,
                    "find",
                    "发送片名、TMDB 编号或工单号搜索；发送 - 清除搜索。",
                )
            return await self._rq_list(chat, mid, member, p)
        if cmd in ("searchretry", "searchtype", "clearyear", "candidate", "year"):
            fetch = cmd != "candidate"
            if cmd == "searchtype":
                p.update(type=arg, page=1)
            if cmd == "clearyear":
                p.update(year=None, page=1)
            if cmd == "year":
                return await self._rq_prompt(
                    chat, mid, member, p, "year", "发送四位年份；建议先选择电影/剧集。"
                )
            if cmd == "candidate":
                index = int(p.get("index", 0)) + int(arg)
                if 0 <= index < len(p.get("results", [])):
                    p["index"] = index
                else:
                    p.update(page=max(1, p.get("page", 1) + int(arg)), index=0)
                    fetch = True
            return await self._rq_candidates(chat, mid, member, p, fetch=fetch)
        if cmd == "pick":
            item = p["results"][p["index"]]
            return await self._rq_selected(
                chat,
                mid,
                member,
                {"media_type": item["media_type"], "tmdb_id": item["tmdb_id"], "meta": item},
            )
        if cmd == "type":
            p["media_type"] = arg
            return await self._rq_selected(chat, mid, member, p)
        if cmd in ("rescope", "scope", "option", "options", "save"):
            return await self._rq_legacy_draft(chat, mid, member, p)
        if cmd in ("allseasons", "selectseasons", "preview"):
            if not p.get("simple"):
                return await self._rq_legacy_draft(chat, mid, member, p)
            if cmd != "preview" and p["media_type"] != "tv":
                raise RequestError("只有剧集可选择季范围")
            if cmd == "allseasons":
                p["demand"] = normalize_demand("tv", {})
            return await self._rq_preview(
                chat, mid, member, p, entering_seasons=cmd == "selectseasons"
            )
        if cmd == "submit":
            if not p.get("simple"):
                return await self._rq_legacy_draft(chat, mid, member, p)
            row = await self._requests.create(
                uid,
                p["media_type"],
                p["tmdb_id"],
                p.get("note", ""),
                demand=p["demand"],
                confirmed_type=True,
                create_key=p["create_key"],
            )
            await self._rq_detail(
                chat,
                mid,
                member,
                row["id"],
                prefix="已提交，扣 1 次。"
                if row.get("created")
                else "已有同需求，未重复创建/扣次。",
            )
            return await self.flush_request_notifications()
        if cmd == "follow":
            row = self._requests.follow(p["same"], uid)
            return await self._rq_detail(
                chat, mid, member, row["id"], prefix="已关注原单，未扣次。"
            )
        if cmd == "view":
            return await self._rq_detail(chat, mid, member, int(arg), view=p.get("view", "user"))
        rid = int(p.get("rid") or card.get("request_id") or 0)
        view = p.get("view", "user")
        staff = view == "staff"
        if (
            cmd
            in (
                "accept",
                "reject",
                "rejectok",
                "reason",
                "rejectreason",
                "ask",
                "internal",
                "correct",
                "refund",
                "retry",
            )
            and not staff
        ):
            raise RequestError("这是用户求片卡，请从独立上片员通知或工作台处理")
        row = self._requests.authorize(rid, uid, staff=staff)
        if cmd == "full":
            return await self._rq_detail(chat, mid, member, rid, full=True, view=view)
        if row["revision"] != card["revision"] or row["status"] != "open":
            return await self._rq_detail(chat, mid, member, rid, view=view,
                                         prefix="工单已变化；未重复执行。")
        if cmd in ("accept", "reject", "rejectok", "reason", "rejectreason") and not staff:
            raise RequestError("你不是上片员")
        if cmd == "rejectreason":
            reasons = {"source": "暂无片源", "library": "已有资源"}
            if arg not in reasons:
                raise RequestError("拒绝理由无效")
            p["reason"] = reasons[arg]
            cmd = "rejectok"
        if cmd in ("accept", "rejectok", "cancelok"):
            status = {"accept": "accepted", "rejectok": "rejected", "cancelok": "cancelled"}[cmd]
            self._requests.finish(
                rid,
                uid,
                status,
                note=p.get("reason", "") if status == "rejected" else "",
                revision=card["revision"],
            )
            await self._rq_detail(chat, mid, member, rid, view=p.get("view", "user"))
            return await self.flush_request_notifications()
        if cmd == "reject":
            p["reason"] = ""
            return await self._rq_render(
                chat,
                mid,
                member,
                f"拒绝工单 #{rid}\n选择下列明确拒绝动作即完成；拒绝不退额度。",
                [
                    [button("拒绝 · 暂无片源", "rejectreason:source"),
                     button("拒绝 · 已有资源", "rejectreason:library")],
                    [button("确认拒绝 · 无理由", "rejectok"), button("其他原因拒绝", "reason:custom")],
                    [button("◀ 返回工单", f"view:{rid}")],
                ],
                p,
                rid=rid,
                revision=row["revision"],
            )
        if cmd == "reason":
            # Old quick-reason callbacks also return to the current explicit input;
            # never execute a legacy confirmation with a newly inferred reason.
            p["reject_on_text"] = True
            return await self._rq_prompt(
                chat,
                mid,
                member,
                p,
                "reason",
                f"工单 #{rid}：请发送拒绝理由（1–500 字）。发送即拒绝。",
                keys=[
                    [button("无理由拒绝", "rejectok"), button("返回详情", f"view:{rid}")]
                ],
                rid=rid,
                revision=row["revision"],
            )
        if cmd == "cancel" and row["emby_user_id"] != uid:
            raise RequestError("只能撤回自己的工单")
        if cmd == "cancel":
            return await self._rq_render(
                chat,
                mid,
                member,
                f"确认撤回待处理工单 #{rid}？撤回不自动退回额度。",
                [[button("确认撤回", "cancelok"), button("返回详情", f"view:{rid}")]],
                p,
                rid=rid,
                revision=row["revision"],
            )
        raise RequestError("操作已失效，请重新打开工单")

    async def _rq_text(self, chat, member, text, message):
        if not self._requests_ready() or not member or not getattr(self._requests, "_db", None):
            return False
        waiting = self._rq_db.one(
            "SELECT * FROM request_inputs WHERE chat_id=? AND user_id=?",
            (str(chat), member["emby_user_id"]),
        )
        if not waiting:
            return False
        if waiting["kind"] in ("ask", "reply", "internal", "correct", "refund"):
            self._rq_clear_input(chat)
            await self._show(chat, "原 Bot 输入已停用，请重新打开工单。历史记录保留。")
            return True
        if text.startswith("/"):
            self._rq_clear_input(chat)
            return False
        card = self._rq_db.one("SELECT * FROM request_cards WHERE token=?", (waiting["token"],))
        if not card or waiting["expires_at"] < time.time():
            self._rq_clear_input(chat)
            await self._show(chat, "求片输入已过期，请从 /requests 或 /uploader 重新打开。")
            return True
        reply = (message.get("reply_to_message") or {}).get("message_id")
        if reply and reply != card["message_id"]:
            await self.send(chat, f"请回复当前求片卡 #{card['message_id']}；本条未转达任何工单。")
            return True
        p, kind, mid = json.loads(card["payload"]), waiting["kind"], card["message_id"]
        p["view"] = self._rq_card_view(card, p)
        try:
            if kind in ("ask", "internal", "reason", "correct", "refund") and p["view"] != "staff":
                raise RequestError("此用户卡不能执行管理操作，请重新打开工作台")
            if kind == "search":
                parsed = parse_link(text)
                if parsed:
                    media_type, tmdb_id = parsed
                    p = {"tmdb_id": tmdb_id, "draft": True}
                    if not media_type:
                        await self._rq_render(
                            chat,
                            mid,
                            member,
                            f"TMDB {tmdb_id} 的电影和剧集编号可能对应不同作品，请明确选择类型。",
                            [
                                [button("电影", "type:movie"), button("剧集", "type:tv")],
                                [button("重新输入", "new")],
                            ],
                            p,
                        )
                    else:
                        p["media_type"] = media_type
                        await self._rq_selected(chat, mid, member, p)
                else:
                    await self._rq_candidates(
                        chat,
                        mid,
                        member,
                        {"query": text[:100], "page": 1, "draft": True},
                        fetch=True,
                    )
            elif kind == "find":
                p.update(search="" if text == "-" else text[:100], page=0)
                await self._rq_list(chat, mid, member, p)
            elif kind == "year":
                if not text.isdigit() or not 1870 <= int(text) <= 2200:
                    raise RequestError("请输入有效四位年份")
                p.update(year=int(text), page=1)
                await self._rq_candidates(chat, mid, member, p, fetch=True)
            elif kind == "simple_seasons":
                try:
                    seasons = numbers(text)
                    if not seasons:
                        raise RequestError("请填写季号，例如 1,3,5-7")
                    p["demand"] = normalize_demand("tv", {"scope": "season", "seasons": seasons})
                except RequestError as exc:
                    await self._rq_preview(
                        chat, mid, member, p, entering_seasons=True, notice="⚠ " + str(exc)
                    )
                    return True
                await self._rq_preview(chat, mid, member, p)
            elif kind in ("seasons", "episodes", "version", "option"):
                # Persisted old input may survive an upgrade. Do not interpret it
                # as a new submit, or overwrite requirements on an existing ticket.
                await self._rq_legacy_draft(chat, mid, member, p)
            else:
                rid = card["request_id"]
                if kind == "reason":
                    row = self._requests.authorize(rid, member['emby_user_id'], staff=True)
                    if row['status'] != 'open' or row['revision'] != card['revision']:
                        self._rq_clear_input(chat)
                        await self._rq_detail(chat, mid, member, rid, view='staff',
                                              prefix='工单已变化；未重复执行。')
                        return True
                    if not text.strip() or len(text) > 500:
                        raise RequestError("理由须为 1–500 字")
                    if not p.get("reject_on_text"):
                        # An old input promised another confirmation. Honor that
                        # promise once, instead of silently rejecting after upgrade.
                        p["reason"] = text
                        await self._rq_render(
                            chat,
                            mid,
                            member,
                            f"拒绝工单 #{rid}\n理由：{escape(text)}",
                            [
                                [
                                    button("按此理由拒绝", "rejectok"),
                                    button("返回详情", f"view:{rid}"),
                                ]
                            ],
                            p,
                            rid=rid,
                            revision=card["revision"],
                        )
                        return True
                    self._requests.finish(
                        rid,
                        member["emby_user_id"],
                        "rejected",
                        note=text,
                        revision=card["revision"],
                    )
                    self._rq_clear_input(chat)
                    await self._rq_detail(chat, mid, member, rid, view=p.get("view", "user"))
                    await self.flush_request_notifications()
                    return True
                else:
                    raise RequestError("输入已过期，请重新打开工单")
        except RequestError as exc:
            # Keep the exact bound input to let the sender correct a typo.
            await self.send(chat, "⚠ " + escape(str(exc)))
        return True

    async def _rq_refresh(self, rid):
        row = self._requests.get(rid)
        if not row:
            return True
        ok = True
        for card in self._rq_db.query("SELECT * FROM request_cards"):
            payload = json.loads(card["payload"])
            if card["request_id"] != rid and payload.get("mode") != "staff":
                continue
            # Immutable communication/result receipts have only a read-only view
            # button. Do not erase the question as soon as its refresh runs.
            if json.loads(card["payload"]).get("notice"):
                continue
            if (
                row["status"] == "open"
                and row["revision"] == card["revision"]
                and self._rq_db.one("SELECT 1 FROM request_inputs WHERE token=?", (card["token"],))
            ):
                continue  # another question must not erase an in-flight response
            member = self._members.get(card["user_id"])
            if not member or str(member.get("tg_user_id") or "") != card["chat_id"]:
                await self._call(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": card["chat_id"],
                        "message_id": card["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
                self._rq_db.execute("DELETE FROM request_cards WHERE token=?", (card["token"],))
                continue
            try:
                with self._bind_session("", ""):
                    if payload.get("mode") == "staff":
                        mid = await self._rq_list(card["chat_id"], card["message_id"], member, payload)
                    else:
                        mid = await self._rq_detail(
                            card["chat_id"], card["message_id"], member, rid,
                            view=self._rq_card_view(card), discovery=payload.get("discovery", False),
                            refresh_only=True,
                        )
                if not mid:
                    await self._call("editMessageReplyMarkup", {
                        "chat_id": card["chat_id"], "message_id": card["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    })
                ok = bool(mid) and ok
            except RequestError:
                self._rq_db.execute("DELETE FROM request_cards WHERE token=?", (card["token"],))
                await self._call(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": card["chat_id"],
                        "message_id": card["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
        # Legacy fan-out messages have no modern capability. Retire rather than
        # interpret their old claim/done buttons under the new meaning.
        for notice in self._requests.notices(rid):
            if self._rq_db.one(
                "SELECT 1 FROM request_cards WHERE chat_id=? AND message_id=?",
                (notice["tg_user_id"], notice["message_id"]),
            ):
                continue
            with self._bind_session("", ""):
                result = await self._edit(
                    notice["tg_user_id"],
                    notice["message_id"],
                    self._rq_body(row) + "\n\n旧卡已停用。\n用 /uploader 打开工作台。",
                    [],
                )
            if not result:
                # Some old group receipts are photo messages. Caption editing
                # is safe on the exact persisted ID; no history scan or send.
                result = await self._call("editMessageCaption", {
                    "chat_id": notice["tg_user_id"], "message_id": notice["message_id"],
                    "caption": self._rq_body(row, compact=True), "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": []},
                })
                if not result and 'not modified' in str(self._last_error).lower():
                    result = True
                if not result:
                    await self._call("editMessageReplyMarkup", {
                        "chat_id": notice["tg_user_id"], "message_id": notice["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    })
            ok = bool(result) and ok
        return ok

    async def flush_request_notifications(self, limit=50):
        # Outbound cards (including a requester who is also an uploader) must not
        # become that private chat's active user menu or password input panel.
        with self._bind_session("", ""):
            return await self._flush_request_notifications(limit)

    async def _flush_request_notifications(self, limit=50):
        """Durable leased outbox. Retry delivers messages only, never business actions."""
        if not self._requests_ready() or not self.enabled:
            return 0
        delivered = 0
        for _ in range(limit):
            job = self._requests.claim_delivery()
            if not job:
                break
            ok, mid = False, None
            try:
                row = self._requests.get(job["request_id"])
                if not row:
                    ok = True
                elif job["kind"] == "refresh":
                    ok = await self._rq_refresh(row["id"])
                else:
                    member = self._members.get(job["user_id"])
                    if not member:
                        ok = True  # deleted account; never redirect private data
                    elif job["kind"] == "new" and not self._rq_staff(member):
                        ok = True
                    elif member.get("tg_user_id"):
                        chat = str(member["tg_user_id"])
                        payload = json.loads(job["payload"])
                        if job["kind"] == "new":
                            # A queued new notice may have become terminal before delivery.
                            mid = await self._rq_detail(
                                chat, None, member, row["id"],
                                prefix="新求片" if row['status'] == 'open' else '', view="staff"
                            )
                        else:
                            body = f"工单 #{row['id']} · {escape(row['display_title'])}\n\n"
                            if job["kind"] == "message":
                                # Recheck staff recipients at delivery after role changes.
                                if not payload["staff"] and not self._rq_staff(member):
                                    self._requests.delivery_result(job["id"], True)
                                    continue
                                body += (
                                    (
                                        "上片员询问（待处理）："
                                        if payload["staff"]
                                        else "用户补充 / 回复："
                                    )
                                    + "\n"
                                    + escape(payload["body"])
                                )
                            elif job["kind"] == "library":
                                body += (
                                    "部分内容已入库，可先观看。"
                                    if payload.get("stage") == "partial"
                                    else "已入库，可开始观看。"
                                )
                            else:
                                body = self._rq_result_notice(row, payload)
                            mid = await self._rq_render(
                                chat,
                                None,
                                member,
                                body,
                                [[button("查看工单", f"view:{row['id']}")]],
                                {
                                    "rid": row["id"],
                                    "notice": True,
                                    "view": "staff"
                                    if job["kind"] == "message" and not payload["staff"]
                                    else "user",
                                },
                                rid=row["id"],
                                revision=row["revision"],
                            )
                        ok = bool(mid)
            except Exception:  # noqa: BLE001 - optional upstream delivery must not replay a business action
                ok = False
            self._requests.delivery_result(job["id"], ok, mid)
            delivered += bool(ok)
        return delivered

    async def announce_request(self, request):
        return await self.flush_request_notifications()

    async def announce_request_claimed(self, request_id, holder_id):
        return await self.flush_request_notifications()

    async def notify_request_resolved(self, request):
        return bool(await self.flush_request_notifications())

    async def _cmd_req(self, chat_id, actor, args):
        member = self._member_for_chat(str(chat_id))
        if member:
            return await self._rq_list(
                chat_id,
                self._panel.get(self._pkey(chat_id)),
                member,
                {
                    "mode": "staff",
                    "status": args[0] if args and args[0] in STATUS_LABELS else "open",
                    "page": 0,
                },
            )
