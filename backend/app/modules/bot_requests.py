"""Private request-center panels. Persisted card-bound capabilities, no TG side effects on import."""

from __future__ import annotations

import json
import secrets
import time
from html import escape

from app.modules.requests import (
    ACCEPT_NOTICE,
    STATUS_LABELS,
    RequestError,
    display_title,
    normalize_demand,
    number_ranges,
    numbers,
)
from app.modules.tmdb import parse_link, poster_url


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
    ):
        """Rotate capabilities only after successful edit. An old token cannot act on a new card."""
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
        if len(body) > 1000:
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
            rows.append([button("📥 上片员工作台", "list:staff")])
        rows.append([{"text": "◀ 主菜单", "callback_data": "home"}])
        await self._rq_render(
            chat,
            mid,
            member,
            f"🎬 <b>求片中心</b>\n\n本月剩余：{'不限' if left is None else str(left) + ' 次'}\n确认建单扣一次；关注不扣次。\n提交后待处理；接受即终结，由上片员手动安排下载。",
            rows,
        )

    async def _request_start(self, chat_id, message_id, member):
        if not self._requests_ready() or not member:
            return await self._rq_home(chat_id, message_id, member)
        await self._rq_render(
            chat_id,
            message_id,
            member,
            "🔎 <b>查找想求的片子</b>\n\n发送中英文片名、TMDB 链接或编号。\n纯编号下一步必须选择电影/剧集。\n例如 Fight Club / 流浪地球 / https://www.themoviedb.org/movie/550\n\n搜索不扣次；额度用完仍可关注原单。",
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
            status=status,
            limit=6,
            offset=page * 5,
            user_id=None if mode == "staff" else member["emby_user_id"],
            followed=mode == "follow",
            media_type=p.get("type", ""),
            search=p.get("search", ""),
        )
        text = [
            f"📋 <b>{ {'mine': '我的求片', 'follow': '我的关注', 'staff': '上片员工作台'}[mode] }</b> · 第 {page + 1} 页",
            f"状态：{STATUS_LABELS.get(status, '全部')} · 类型：{ {'movie': '电影', 'tv': '剧集'}.get(p.get('type'), '全部') }"
            + (f" · 搜索：{escape(p['search'])}" if p.get("search") else ""),
        ]
        keys = []
        for row in rows[:5]:
            lines = [
                line for line in row["demand_text"].splitlines() if not line.endswith("：无要求")
            ]
            summary = " · ".join(lines[:3])[:100]
            if row["media_type"] == "tv" and row["demand"]["scope"] in ("series", "season"):
                summary = self._rq_season_label(row["demand"])
            text.append(
                f"#{row['id']} {escape(row['display_title'])} · {row['media_label']} · {row['status_label']}\n{escape(summary)}"
                + (f" · {escape(row['username'])}" if mode == "staff" else "")
            )
            keys.append([button(f"#{row['id']} 详情", f"view:{row['id']}")])
        if not rows:
            text.append("暂无符合条件的工单。")
        keys.append(
            [
                button("待处理", "status:open"),
                button("已接受", "status:accepted"),
                button("已拒绝", "status:rejected"),
            ]
        )
        keys.append([button("已取消", "status:cancelled"), button("全部", "status:")])
        keys.append(
            [
                button("电影", "filter:movie"),
                button("剧集", "filter:tv"),
                button("全部类型", "filter:"),
            ]
        )
        keys.append([button("🔎 搜索工单", "find")])
        nav = []
        if page:
            nav.append(button("上一页", "page:-1"))
        if len(rows) > 5:
            nav.append(button("下一页", "page:1"))
        if nav:
            keys.append(nav)
        keys.append([button("◀ 求片中心", "home")])
        p.update(mode=mode, status=status, page=page)
        return await self._rq_render(chat, mid, member, "\n\n".join(text), keys, p)

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
        rows = [
            [
                button("电影", "searchtype:movie"),
                button("剧集", "searchtype:tv"),
                button("全部", "searchtype:"),
            ],
            [button("按年份筛选", "year"), button("清除年份", "clearyear")],
        ]
        photo = ""
        if results:
            item = results[index]
            photo = poster_url(item.get("poster_path", ""))
            body = (
                f"🎞 <b>{escape(display_title(item))}</b>\n"
                f"{'电影' if item['media_type'] == 'movie' else '剧集'} · TMDB {item['tmdb_id']}\n"
                f"搜索：{escape(p['query'])} · 第 {p.get('page', 1)}/{p.get('total_pages', 1)} 页 · 候选 {index + 1}/{len(results)}\n"
                + escape(str(item.get("overview") or "")[:250])
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
        rows.append([button("重新搜索", "new"), button("求片中心", "home")])
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
            return "⚠ 媒体库查询失败/未配置，未知是否已有；仍可提交。", []
        if not hint.get("items"):
            return "媒体库暂未查到匹配内容（仅本次辅助查询）。", []
        lines, links = ["📚 媒体库已有匹配内容，可先打开详情查看："], []
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
                links.append([{"text": "打开媒体库详情 · " + label[:30], "url": item["url"]}])
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
                prefix="要求编辑已简化，原工单要求未更改；可通过补充 / 回复说明。",
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
            f"🎬 <b>确认求片</b>\n{escape(display_title(meta))}\n"
            f"{'电影' if p['media_type'] == 'movie' else '剧集'}"
        )
        if p["media_type"] == "tv":
            body += "\n季范围：" + self._rq_season_label(p["demand"])
        if p.get("history"):
            body += (
                "\n\n已有接受历史（"
                + ",".join("#" + str(r["id"]) for r in p["history"])
                + "），请耐心等待下载完成并入库。"
            )
        hint, links = self._rq_library(p.get("library") or {})
        body += "\n\n" + hint
        keys = []
        if entering_seasons:
            body += (
                "\n\n请发送季号，例如 1,3,5-7（第 0 季为特别篇）。收到后会更新本卡，直接确认提交。"
            )
        else:
            same = self._requests.same_demand(p["media_type"], p["tmdb_id"], p["demand"])
            if same and same["status"] == "open":
                p["same"] = same["id"]
                if same["emby_user_id"] == member["emby_user_id"]:
                    keys.append(
                        [button(f"查看我的原单 #{same['id']}（不扣次）", f"view:{same['id']}")]
                    )
                else:
                    keys.append([button(f"关注原单 #{same['id']}（不扣次）", "follow")])
                body += f"\n\n已有相同求片 #{same['id']}，不重复创建、不扣次数。"
            elif same:
                body += f"\n\n相同求片 #{same['id']} 已接受，请耐心等待下载完成并入库。"
            else:
                left = self._requests.remaining(member["emby_user_id"])
                body += f"\n\n确认求片扣 1 次；本月剩余 {'不限' if left is None else left}。提交后为待处理。"
                keys.append([button("确认求片（扣 1 次）", "submit")])
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
        keys += links + [[button("重新选片", "new"), button("求片中心", "home")]]
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

    async def _rq_detail(self, chat, mid, member, rid, *, prefix="", thread=None, full=False):
        staff = self._rq_staff(member)
        row = self._requests.authorize(rid, member["emby_user_id"], staff=staff)
        own = row["emby_user_id"] == member["emby_user_id"]
        p = {"rid": rid}
        keys = []
        body = f"🎬 <b>{escape(row['display_title'][:130])}</b>\n{row['media_label']} · 工单 #{rid} · {row['status_label']}\n"
        if row.get("original_title") and row["original_title"] != row["title"]:
            body += escape(row["original_title"][:100]) + "\n"
        body += "\n"
        if thread is not None:
            page = max(0, int(thread))
            events = self._requests.events(rid, internal=staff, limit=2, offset=page)
            if events:
                e = events[0]
                label = {
                    "created": "提交",
                    "accepted": "已接受",
                    "rejected": "已拒绝",
                    "cancelled": "已取消",
                    "question": "上片员询问",
                    "reply": "用户回复",
                    "modified": "修改需求",
                    "internal": "🔒 内部备注",
                    "refund": "🔒 额度退回",
                    "correction": "🔒 管理员纠错",
                }.get(e["kind"], e["kind"])
                body += f"{label} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['created_at']))}\n{escape(e['body']) or '（无附言）'}"
            else:
                body += "暂无记录"
            p["thread"] = page
            nav = []
            if page:
                nav.append(button("较新记录", "thread:-1"))
            if len(events) > 1:
                nav.append(button("较早记录", "thread:1"))
            if nav:
                keys.append(nav)
            keys.append([button("返回详情", f"view:{rid}")])
        else:
            demand = (
                row["demand_text"]
                if full
                else "\n".join(
                    line if len(line) <= 70 else line[:67] + "…"
                    for line in row["demand_text"].splitlines()
                    if not line.endswith("：无要求")
                )
            )
            if row["media_type"] == "tv" and row["demand"]["scope"] in ("series", "season"):
                demand = "季范围：" + self._rq_season_label(row["demand"]) + "\n" + demand
            note = row["note"]
            if not full and len(note) > 150:
                note = note[:147] + "…（完整需求见详情）"
            body += escape(demand)
            if note:
                body += "\n备注：" + escape(note)
            if row.get("overview") and not full:
                body += "\n\n" + escape(row["overview"][:120])
            if not full:
                keys.append([button("详情", "full")])
            else:
                keys.append([button("返回工单", f"view:{rid}")])
            if staff:
                body += "\n求片人：" + escape(row["username"])
            if row["status"] == "accepted":
                body += "\n\n" + ACCEPT_NOTICE
            if row["status"] == "rejected" and row["result_note"]:
                body += "\n拒绝理由：" + escape(row["result_note"])
            if prefix:
                body += "\n\n" + escape(prefix)
            keys.append([button("沟通 / 操作记录", "thread:0")])
            if row["status"] == "open":
                if staff:
                    keys = [
                        [
                            button("✅ 接受请求", "accept"),
                            button("拒绝请求", "reject"),
                            button("询问用户", "ask"),
                        ]
                    ] + keys
                    keys.append([button("🔒 内部备注", "internal")])
                if own:
                    keys += [
                        [button("补充 / 回复", "reply"), button("撤回求片", "cancel")],
                    ]
            if "admin" in (member.get("roles") or []):
                keys.append(
                    [button("🔒 管理员纠错", "correct"), button("🔒 人工退回额度", "refund")]
                )
            if staff:
                keys.append([button("重试失败通知", "retry")])
            keys.append(
                [
                    button(
                        "◀ 工作台" if staff else "◀ 我的求片",
                        "list:staff" if staff else "list:mine",
                    ),
                ]
            )
        keys.append(
            [
                {
                    "text": "TMDB 详情 ↗",
                    "url": f"https://www.themoviedb.org/{row['media_type']}/{row['tmdb_id']}",
                }
            ]
        )
        return await self._rq_render(
            chat,
            mid,
            member,
            body,
            keys,
            p,
            rid=rid,
            revision=row["revision"],
            photo=poster_url(row["poster_path"]) if thread is None and not full else "",
        )

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
                        button("返回详情", f"view:{rid}"),
                        button(
                            "工作台" if self._rq_staff(member) else "我的求片",
                            "list:staff" if self._rq_staff(member) else "list:mine",
                        ),
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
        if cmd == "home":
            return await self._rq_home(chat, mid, member)
        if cmd == "new":
            return await self._request_start(chat, mid, member)
        if cmd in ("list", "status", "page", "filter", "find"):
            if cmd == "list":
                p = {"mode": arg, "status": "open" if arg == "staff" else "", "page": 0}
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
            return await self._rq_detail(chat, mid, member, int(arg))
        rid = int(p.get("rid") or card.get("request_id") or 0)
        staff = self._rq_staff(member)
        row = self._requests.authorize(rid, uid, staff=staff)
        if cmd == "full":
            return await self._rq_detail(chat, mid, member, rid, full=True)
        if cmd == "thread":
            page = int(p.get("thread", 0)) + int(arg) if arg else 0
            return await self._rq_detail(chat, mid, member, rid, thread=page)
        if cmd == "retry":
            if not staff:
                raise RequestError("你不是上片员")
            self._requests.retry_notifications(rid)
            await self.flush_request_notifications()
            return await self._rq_detail(
                chat, mid, member, rid, prefix="已尝试重发未送达通知，未重复处理/扣次。"
            )
        if row["revision"] != card["revision"]:
            raise RequestError("工单已被修改，请重新打开详情")
        if cmd in ("correct", "refund"):
            self._requests.authorize(rid, uid, admin=True)
            body = (
                "输入目标状态和纠错原因：open / accepted / rejected / cancelled 后接空格和原因。纠错不自动退额度。"
                if cmd == "correct"
                else "发送人工退回额度原因。每工单仅可退一次，只退原扣次月份；普通拒绝不退款。"
            )
            return await self._rq_prompt(
                chat,
                mid,
                member,
                p,
                cmd,
                f"🔒 工单 #{rid}\n{body}",
                rid=rid,
                revision=row["revision"],
            )
        if row["status"] != "open":
            raise RequestError("此工单已终结，旧按钮不可再次执行")
        if cmd in ("accept", "reject", "rejectok", "reason", "ask", "internal") and not staff:
            raise RequestError("你不是上片员")
        if cmd in ("accept", "rejectok", "cancelok"):
            status = {"accept": "accepted", "rejectok": "rejected", "cancelok": "cancelled"}[cmd]
            self._requests.finish(
                rid,
                uid,
                status,
                note=p.get("reason", "") if status == "rejected" else "",
                revision=card["revision"],
            )
            await self._rq_detail(chat, mid, member, rid)
            return await self.flush_request_notifications()
        if cmd == "reject":
            p["reason"] = ""
            return await self._rq_render(
                chat,
                mid,
                member,
                f"拒绝工单 #{rid}\n理由完全选填。不填就不向用户显示理由。普通拒绝不自动退回额度。",
                [
                    [button("直接拒绝（无理由）", "rejectok"), button("填写理由", "reason:custom")],
                    [button("返回详情", f"view:{rid}")],
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
                f"工单 #{rid}：请发送拒绝理由（1–500 字）。发送后立即拒绝，无需再次确认。",
                keys=[
                    [button("直接拒绝（无理由）", "rejectok"), button("返回详情", f"view:{rid}")]
                ],
                rid=rid,
                revision=row["revision"],
            )
        if cmd in ("ask", "internal", "reply"):
            if cmd == "reply" and row["emby_user_id"] != uid:
                raise RequestError("关注者不能补充他人的工单")
            text = {
                "ask": "询问求片人（仍待处理）",
                "internal": "内部备注（仅上片员/管理员可见）",
                "reply": "发送补充/回复给上片员",
            }[cmd]
            return await self._rq_prompt(
                chat,
                mid,
                member,
                p,
                cmd,
                f"工单 #{rid} · {text}\n请输入消息（最多 500 字）。",
                rid=rid,
                revision=row["revision"],
            )
        if cmd in ("modify", "cancel") and row["emby_user_id"] != uid:
            raise RequestError("只能修改/撤回自己的工单")
        if cmd == "modify":
            return await self._rq_detail(
                chat,
                mid,
                member,
                rid,
                full=True,
                prefix="要求编辑已简化，原要求未更改；可通过补充 / 回复说明。",
            )
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
        try:
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
                if kind in ("ask", "reply", "internal"):
                    self._requests.message(
                        rid,
                        member["emby_user_id"],
                        text,
                        staff=kind == "ask",
                        internal=kind == "internal",
                        revision=card["revision"],
                        key="input:" + card["token"],
                    )
                elif kind == "reason":
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
                    await self._rq_detail(chat, mid, member, rid)
                    await self.flush_request_notifications()
                    return True
                elif kind == "correct":
                    status, _, reason = text.partition(" ")
                    self._requests.correct(
                        rid, member["emby_user_id"], status, reason, card["revision"]
                    )
                elif kind == "refund":
                    self._requests.refund(rid, member["emby_user_id"], text)
                else:
                    raise RequestError("输入已过期")
                self._rq_clear_input(chat)
                await self._rq_detail(
                    chat,
                    mid,
                    member,
                    rid,
                    prefix="已记录。"
                    if kind in ("internal", "refund", "correct")
                    else "消息已保存并将通知对方。",
                )
                await self.flush_request_notifications()
        except RequestError as exc:
            # Keep the exact bound input to let the sender correct a typo.
            await self.send(chat, "⚠ " + escape(str(exc)))
        return True

    async def _rq_refresh(self, rid):
        row = self._requests.get(rid)
        if not row:
            return True
        ok = True
        for card in self._rq_db.query("SELECT * FROM request_cards WHERE request_id=?", (rid,)):
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
                mid = await self._rq_detail(card["chat_id"], card["message_id"], member, rid)
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
            result = await self._edit(
                notice["tg_user_id"],
                notice["message_id"],
                f"工单 #{rid} · {row['status_label']}\n旧卡已停用，请用 /uploader 打开工作台。",
            )
            ok = bool(result) and ok
        return ok

    async def flush_request_notifications(self, limit=50):
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
                                chat, None, member, row["id"], prefix="新求片"
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
                                        "上片员询问（请求仍待处理）："
                                        if payload["staff"]
                                        else "用户补充 / 回复："
                                    )
                                    + "\n"
                                    + escape(payload["body"])
                                )
                            else:
                                if payload.get("correction"):
                                    body += "管理员已纠正工单状态：\n"
                                status = payload["status"]
                                body += (
                                    ACCEPT_NOTICE if status == "accepted" else STATUS_LABELS[status]
                                )
                                if status == "rejected" and payload.get("note"):
                                    body += "\n理由：" + escape(payload["note"])
                            mid = await self._rq_render(
                                chat,
                                None,
                                member,
                                body,
                                [[button("查看工单", f"view:{row['id']}")]],
                                {"rid": row["id"], "notice": True},
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
