"""Confirmed access violations, bounded sanctions and durable delivery receipts.

No sanctions are inferred from a 403, User-Agent or a stale polling snapshot.
The caller supplies authenticated identity and a fresh confirmation callback.
"""
from __future__ import annotations

import asyncio
import html
import json
import time
from typing import Any

from app.modules.report_delivery import CALL_DELIVERY, failure, record_failure

RULES = {"concurrency": "超过同播限制", "web_login": "Emby 网页登录", "web_play": "Emby 网页观看"}
ACTIONS = {"pause": "阻止／停止本次播放", "disable": "禁用账号", "delete": "删除账号"}
DEFAULTS = {
    "concurrency": {"enabled": True, "action": "pause"},
    "web_login": {"enabled": False, "action": "disable"},
    "web_play": {"enabled": False, "action": "disable"},
}


def is_web_client(session: dict[str, Any]) -> bool:
    # The server's authenticated session identity, NEVER browser User-Agent.
    return str(session.get("Client") or "").strip().casefold() in {
        "emby web", "emby web mobile", "emby web client"}


class RestrictionService:
    def __init__(self, db: Any, store: Any, members: Any, emby: Any,
                 telegram: Any, telegram_config: Any) -> None:
        self.db, self.store, self.members = db, store, members
        self.emby, self.telegram, self.telegram_config = emby, telegram, telegram_config
        self._lock = asyncio.Lock()
        self._notice_lock = asyncio.Lock()
        # Never repeat a possibly completed sanction/send after a crash.
        db.execute("UPDATE restriction_events SET status='unknown', result='处理被中断，需核实；未自动重复处罚' WHERE status='pending'")
        db.execute("UPDATE restriction_notices SET state='unknown', error='发送被中断，需核实；未自动补发' WHERE state='sending'")

    def config(self) -> dict[str, Any]:
        raw = self.store.section("restrictions")
        result = {k: dict(v) for k, v in DEFAULTS.items()}
        for rule, default in result.items():
            value = raw.get(rule)
            if isinstance(value, dict):
                default["enabled"] = value.get("enabled") is True
                if value.get("action") in ACTIONS:
                    default["action"] = value["action"]
        return result

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - set(RULES):
            raise ValueError("未知限制规则")
        cfg = self.config()
        for rule, value in payload.items():
            if (not isinstance(value, dict) or set(value) != {"enabled", "action"}
                    or not isinstance(value["enabled"], bool) or value["action"] not in ACTIONS):
                raise ValueError("限制必须包含启用开关和合法处罚方式")
            cfg[rule] = dict(value)
        self.store.set_section("restrictions", cfg)
        return cfg

    async def user(self, uid: str) -> dict[str, Any] | None:
        # list_users is a fresh authoritative read. Failure is not non-admin.
        users = await self.emby.list_users()
        rows = [u for u in users if str(u.get("Id") or "") == uid]
        if len(rows) != 1 or not isinstance(rows[0].get("Policy"), dict):
            return None
        return rows[0]

    def exempt(self, uid: str, user: dict[str, Any]) -> bool:
        member = self.members.get(uid) or {}
        return bool((user.get("Policy") or {}).get("IsAdministrator")
                    or "admin" in (member.get("roles") or []))

    async def apply(self, rule: str, uid: str, confirm: Any) -> dict[str, Any] | None:
        """Recheck under the sanction lock; None means no confirmed violation.

        confirm returns only non-secret evidence with session_id, playing and
        optionally cap/live_count/pending_count. It must re-evaluate current
        activity, not simply return the initial rejection.
        """
        async with self._lock:
            cfg = self.config().get(rule, {})
            if not cfg.get("enabled"):
                return None
            user = await self.user(uid)
            if not user or self.exempt(uid, user):
                return None
            evidence = await confirm()
            if not evidence:
                return None
            # Keep receipts useful without copying request credentials or IPs.
            safe = {k: evidence[k] for k in ("session_id", "playing", "cap", "live_count", "pending_count") if k in evidence}
            sid = str(safe.get("session_id") or "")
            if not sid:
                return None
            now = int(time.time())
            old = self.db.query("SELECT * FROM restriction_events WHERE user_id=? AND rule=? AND (session_id=? OR rule='web_login') AND created_at>=? ORDER BY id DESC LIMIT 1", (uid, rule, sid, now - 300))
            if old:
                return old[0]
            member = self.members.get(uid) or {}
            action = cfg["action"]
            with self.db.write() as conn:
                cursor = conn.execute("INSERT INTO restriction_events(user_id,username,tg_user_id,rule,action,session_id,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                      (uid, str(user.get("Name") or member.get("username") or uid), str(member.get("tg_user_id") or ""), rule, action, sid, json.dumps(safe), now, now))
                event_id = cursor.lastrowid
                tg = self.telegram_config()
                targets = [("private", str(member.get("tg_user_id") or ""))]
                targets += [("group:" + str(chat), str(chat)) for chat in tg.get("group_interaction_chats", [])]
                if len(targets) == 1:
                    targets.append(("group:", ""))
                for kind, chat in targets:
                    conn.execute("INSERT OR IGNORE INTO restriction_notices(event_id,kind,chat_id,state,error) VALUES(?,?,?,?,?)",
                                 (event_id, kind, chat, "pending" if chat else "skipped", "" if chat else ("未绑定 Telegram" if kind == "private" else "未配置互动群")))
            status, result = "done", "已拒绝本次请求"
            try:
                if action == "disable":
                    # Preserve a suspension locally so reconciliation cannot undo it.
                    if member:
                        self.members.set_status(uid, "suspended", actor="restriction")
                    outcome = await self.emby.apply_member_policy(uid, {"IsDisabled": True})
                    if outcome.get("status") != "applied":
                        if outcome.get("status") == "skipped_admin" and member:
                            self.members.set_status(uid, member["status"], actor="restriction")
                        status, result = "failed", "禁用未确认成功；本次请求已拒绝，需管理员核实"
                    else:
                        result = "账号已禁用"
                elif action == "delete":
                    # Guard again immediately before the destructive endpoint.
                    current = await self.user(uid)
                    if not current or self.exempt(uid, current):
                        status, result = "failed", "账号权限已变化，未执行删除"
                    elif await self.emby.delete_user(uid):
                        if member:
                            self.members.delete(uid, actor="restriction", cascade=False)
                        result = "账号已删除（不连带删除其他账号）"
                    else:
                        status, result = "failed", "删除未确认成功；本次请求已拒绝，需管理员核实"
                if safe.get("playing") and status == "done":
                    accepted = await self.emby.stop_session(sid)
                    result += "；已发送该会话停止指令（客户端执行待确认）" if accepted else "；停止指令未获确认"
                elif action == "pause":
                    result = ("已拒绝本次登录" if rule == "web_login" else
                              "已拒绝本次网页起播" if rule == "web_play" else
                              "已拒绝超出的本次起播；已有合规播放不动")
            except Exception:  # noqa: BLE001 - unknown transport outcome must not cause replay
                status, result = "unknown", "处罚执行结果不明；本次请求已拒绝，需管理员核实"
            self.db.execute("UPDATE restriction_events SET status=?,result=?,updated_at=? WHERE id=?", (status, result, int(time.time()), event_id))
            self.members.audit("restriction", "restriction." + rule, uid, f"event={event_id} action={action} status={status}", ok=status == "done")
            return self.db.query("SELECT * FROM restriction_events WHERE id=?", (event_id,))[0]

    async def sampled_concurrency(self, uid: str, sid: str) -> bool:
        # The sampler is only a fallback. Never punish using its earlier snapshot.
        from app.modules.streams import overflow_session_ids
        async def confirm():
            member = self.members.get(uid) or {}
            cap = int(member.get("max_streams") or 0)
            rows = await self.emby.active_sessions_raw()
            if sid not in overflow_session_ids(rows, uid, cap):
                return None
            return {"session_id": sid, "playing": True, "cap": cap,
                    "live_count": sum(bool(s.get("NowPlayingItem")) for s in rows if str(s.get("UserId")) == uid)}
        event = await self.apply("concurrency", uid, confirm)
        return bool(event and event["status"] == "done")

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id,user_id,username,rule,action,status,result,created_at FROM restriction_events ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["notices"] = self.db.query("SELECT kind,state,attempts,error FROM restriction_notices WHERE event_id=? ORDER BY id", (row["id"],))
        return rows

    async def deliver(self) -> None:
        async with self._notice_lock:
            rows = self.db.query("SELECT n.*,e.username,e.rule,e.action,e.status,e.result FROM restriction_notices n JOIN restriction_events e ON e.id=n.event_id WHERE n.state IN ('pending','retry') AND n.next_at<=? AND e.status<>'pending' ORDER BY n.id LIMIT 20", (int(time.time()),))
            for row in rows:
                self.db.execute("UPDATE restriction_notices SET state='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
                row["attempts"] += 1
                text = (f"<b>账号限制处理 #{row['event_id']}</b>\n"
                        f"用户：{html.escape(row['username'])}\n"
                        f"原因：{RULES[row['rule']]}\n处罚：{ACTIONS[row['action']]}\n"
                        f"结果：{html.escape(row['result'])}")
                marker = CALL_DELIVERY.set(None)
                try:
                    message = await self.telegram.send_message(row["chat_id"], text)
                    outcome = {"state": "sent", "reason": ""} if message else (CALL_DELIVERY.get() or failure())
                except Exception as exc:  # noqa: BLE001 - classify delivery, never leak raw error
                    outcome = failure(exc=exc)
                finally:
                    CALL_DELIVERY.reset(marker)
                record_failure(row, outcome, time.time())
                self.db.execute("UPDATE restriction_notices SET state=?,next_at=?,error=? WHERE id=?", (row["state"], row.get("next_at", 0), row.get("reason", ""), row["id"]))
