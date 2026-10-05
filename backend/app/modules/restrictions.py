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

from app.modules.enforcement import BLOCKING_STATES, EnforcementService
from app.modules.member_ops import observe_one, record_remote
from app.modules.report_delivery import CALL_DELIVERY, failure, record_failure

RULES = {"concurrency": "超过同播限制", "web_login": "Emby 网页登录", "web_play": "Emby 网页观看"}
ACTIONS = {"pause": "阻止／停止本次播放", "disable": "禁用账号", "delete": "删除账号", "enable": "解除禁用"}
EVENT_RULES = {**RULES, "manual": "管理员手动操作"}
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
                 telegram: Any, telegram_config: Any, enforcement: Any = None) -> None:
        self.db, self.store, self.members = db, store, members
        self.emby, self.telegram, self.telegram_config = emby, telegram, telegram_config
        self.enforcement = enforcement or EnforcementService(db, members, emby)
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
                if value.get("action") in ("pause", "disable", "delete"):
                    default["action"] = value["action"]
        return result

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - set(RULES):
            raise ValueError("未知限制规则")
        cfg = self.config()
        for rule, value in payload.items():
            if (not isinstance(value, dict) or set(value) != {"enabled", "action"}
                    or not isinstance(value["enabled"], bool) or value["action"] not in ("pause", "disable", "delete")):
                raise ValueError("限制必须包含启用开关和合法处罚方式")
            cfg[rule] = dict(value)
        self.store.set_section("restrictions", cfg)
        return cfg

    async def user(self, uid: str, *, allow_partial: bool = False) -> dict[str, Any] | None:
        # list_users is a fresh authoritative read. Failure is not non-admin.
        users = await self.emby.list_users()
        rows = [u for u in users if str(u.get("Id") or "") == uid]
        if len(rows) != 1 or (not allow_partial and not isinstance(rows[0].get("Policy"), dict)):
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
        async with self._lock, self.enforcement._apply_lock:
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
            old = self.db.query("SELECT * FROM restriction_events WHERE user_id=? AND rule=? AND (session_id=? OR rule='web_login') AND created_at>=? AND id>COALESCE((SELECT MAX(id) FROM restriction_events WHERE user_id=? AND rule='manual' AND action='enable' AND status='done'),0) ORDER BY id DESC LIMIT 1", (uid, rule, sid, now - 300, uid))
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
                    if member and member.get("status") != "pending":
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

    def access_state(self, uid: str, user: dict[str, Any] | None, *,
                     available: bool = True) -> dict[str, Any]:
        member = self.members.get(uid) or {"emby_user_id": uid, "state": "active"}
        observed = observe_one(member, user, emby_available=available)
        # Suspension hides expiry/quota in effective_state; expose those too.
        base = {**member, "status": "active"}
        state, reason = self.members.effective_state(base, member.get("group"))
        limits = []
        if member.get("status") == "pending":
            limits.append("待开通")
        if state in ("expired", "exhausted"):
            limits.append(reason)
        policy = (user or {}).get("Policy") or {}
        if policy.get("EnableRemoteAccess") is False:
            limits.append("Emby 禁止远程访问")
        if policy.get("EnableMediaPlayback") is False:
            limits.append("Emby 禁止媒体播放")
        if policy.get("EnableAllFolders") is False and policy.get("EnabledFolders") == []:
            limits.append("无可用媒体库")
        observed["remaining_restrictions"] = limits
        observed["account_available"] = (observed["emby_disabled"] is False
                                         and member.get("state") not in BLOCKING_STATES and not limits)
        return observed

    def _manual_result(self, uid: str, action: str, user: dict[str, Any]) -> str:
        access = self.access_state(uid, user)
        if action == "disable":
            return "管理员手动禁用：账号已禁用"
        if access["account_available"]:
            return "管理员手动解除禁用：账号已可用（仍受原组和其他权限约束）"
        return "管理员手动解除禁用：账号仍不可用：" + "、".join(
            access["remaining_restrictions"] or [access.get("state_reason") or "其他权益限制"])

    async def manual(self, uid: str, action: str, *, actor: str,
                     actor_user_id: str = "", request_id: str = "",
                     authorize: Any = None) -> dict[str, Any]:
        """Explicit intent, never a toggle; serialize with sanctions/reconcile.

        Remote receipt precedes the local status commit. On failure no local
        suspension is cleared and a later reconcile cannot revive an old write.
        Only IsDisabled is patched: expiry, usage, roles and policy stay intact.
        A repeated request identity observes the old receipt, never replays it.
        """
        if action not in ("disable", "enable"):
            raise ValueError("必须明确选择 disable 或 enable")
        if request_id and (len(request_id) > 64 or not all(
                c.isascii() and (c.isalnum() or c in "-_:") for c in request_id)):
            raise ValueError("无效的操作标识")
        async with self._lock, self.enforcement._apply_lock:
            def authorized():
                return authorize is None or authorize()

            def response(ok, local_ok, remote_ok, text, user=None, *, retry=False, event=None):
                return {"ok": ok, "local_ok": local_ok, "remote_ok": remote_ok,
                        "retryable": retry, "error": "" if ok else text,
                        "result": text, "event_id": event,
                        "access": self.access_state(uid, user, available=user is not None)}

            if not authorized():
                return response(False, False, None, "管理员身份或权限已变化，未执行操作")
            try:
                user = await self.user(uid, allow_partial=True)
            except Exception:  # noqa: BLE001 - identity must be affirmative
                return response(False, False, None, "无法确认 Emby 账号及管理员身份；未执行，可重试", retry=True)
            if not user:
                return {**response(False, False, None, "Emby 账号不存在，未执行操作"),
                        "skipped": "emby_user_missing"}
            if not isinstance(user.get("Policy"), dict):
                return response(False, False, None, "无法确认 Emby 管理员身份；未执行，可重试", retry=True)
            member = self.members.get(uid) or {}
            if (not authorized() or uid == actor_user_id or self.exempt(uid, user)
                    or str(user.get("Name") or "").casefold() == actor.casefold()
                    or (action == "disable" and user["Policy"].get("IsAdministrator") is not False)):
                return response(False, False, None, "管理员账号、操作人自身或权限不明，未执行操作", user)
            if request_id:
                prior = self.db.one("SELECT * FROM restriction_events WHERE rule='manual' AND session_id=? ORDER BY id DESC LIMIT 1", (request_id,))
                if prior:
                    if prior["user_id"] != uid or prior["action"] != action:
                        return response(False, False, None, "操作标识与原目标或动作不符，未执行", user)
                    access = self.access_state(uid, user)
                    remote_label = ("未知" if access["emby_disabled"] is None else
                                    "已禁用" if access["emby_disabled"] else "未禁用")
                    text = ("原管理员手动操作回执：" + ("已完成" if prior["status"] == "done" else "未确认成功")
                            + "（未重复执行）；当前本地封禁："
                            + ("已封禁" if member.get("status") == "suspended" else "未封禁")
                            + "；Emby：" + remote_label + "；剩余限制："
                            + "、".join(access["remaining_restrictions"] or ["无额外权益限制"]))
                    return response(prior["status"] == "done", prior["status"] == "done",
                                    True if prior["status"] == "done" else None,
                                    text, user, retry=prior["status"] != "done", event=prior["id"])
            prospective = dict(member)
            if action == "disable" and member.get("status") != "pending":
                prospective["status"] = "suspended"
            elif member.get("status") == "suspended":
                prospective["status"] = "active"
            state, _ = self.members.effective_state(prospective, member.get("group"))
            disabled = action == "disable" or state in BLOCKING_STATES
            local_change = bool(member and prospective.get("status") != member.get("status"))
            # An identical action without a key is a no-op, not another notice.
            old = self.db.one("SELECT * FROM restriction_events WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
            if (not local_change and user["Policy"].get("IsDisabled") is disabled
                    and old and old["rule"] == "manual" and old["action"] == action
                    and old["status"] == "done"):
                text = "当前已是目标状态；" + self._manual_result(uid, action, user)
                if action == "disable":
                    try:
                        await self.enforcement.terminate_users({uid}, "管理员手动禁用", strict=True)
                    except Exception:  # noqa: BLE001 - retry stop without repeating successful notices
                        text += "；会话终止未确认，可重试禁用"
                receipt_id = old["id"]
                if request_id:
                    now = int(time.time())
                    with self.db.write() as conn:
                        receipt_id = conn.execute("INSERT INTO restriction_events(user_id,username,rule,action,session_id,status,result,evidence_json,created_at,updated_at) VALUES(?,?,'manual',?,?,'done',?,?,?,?)",
                                                  (uid, str(user.get("Name") or uid), action, request_id, text, json.dumps({"actor": actor, "no_change": True}), now, now)).lastrowid
                return response(True, True, True, text, user, event=receipt_id)
            now = int(time.time())
            with self.db.write() as conn:
                event_id = conn.execute("INSERT INTO restriction_events(user_id,username,tg_user_id,rule,action,session_id,evidence_json,created_at,updated_at) VALUES(?,?,?,'manual',?,?,?,?,?)",
                                        (uid, str(user.get("Name") or member.get("username") or uid), str(member.get("tg_user_id") or ""), action, request_id, json.dumps({"actor": actor}), now, now)).lastrowid
                targets = [("private", str(member.get("tg_user_id") or ""))]
                targets += [("group:" + str(chat), str(chat)) for chat in self.telegram_config().get("group_interaction_chats", [])]
                if len(targets) == 1:
                    targets.append(("group:", ""))
                for kind, chat in targets:
                    conn.execute("INSERT OR IGNORE INTO restriction_notices(event_id,kind,chat_id,state,error) VALUES(?,?,?,?,?)",
                                 (event_id, kind, chat, "pending" if chat else "skipped", "" if chat else ("未绑定 Telegram" if kind == "private" else "未配置互动群")))
            status, text, local_ok, remote_ok = "failed", "Emby 操作未确认；未改本地状态，可重试", False, False
            try:
                # Last synchronous permission/target check immediately before
                # adapter's fresh policy read and protected remote write.
                current = self.members.get(uid) or {}
                if not authorized() or "admin" in (current.get("roles") or []):
                    text = "管理员身份或权限已变化，未执行操作"
                else:
                    def write_guard():
                        fresh = self.members.get(uid) or {}
                        if (not authorized() or "admin" in (fresh.get("roles") or [])
                                or bool(fresh) != bool(member)
                                or fresh.get("status") != member.get("status")):
                            return False
                        projected = {**fresh, "status": prospective.get("status", "active")}
                        fresh_state, _ = self.members.effective_state(projected, fresh.get("group"))
                        return (action == "disable" or fresh_state in BLOCKING_STATES) == disabled

                    outcome = await self.emby.apply_member_policy(uid, {"IsDisabled": disabled},
                                                                  authorize=write_guard)
                    if outcome.get("status") == "skipped_authority":
                        text = "管理员权限或目标权益已变化，未执行操作，请重新确认"
                    elif outcome.get("status") == "skipped_admin":
                        text = "目标已变为 Emby 管理员，未执行操作"
                    elif outcome.get("status") == "applied":
                        if local_change:
                            self.members.set_status(uid, prospective["status"], actor=actor)
                        local_ok, remote_ok = True, True
                        user = await self.user(uid)
                        if not user or user["Policy"].get("IsDisabled") is not disabled:
                            raise RuntimeError("remote verification unavailable")
                        status = "done"
                        text = self._manual_result(uid, action, user)
                        if action == "disable":
                            try:
                                await self.enforcement.terminate_users({uid}, "管理员手动禁用", strict=True)
                            except Exception:  # noqa: BLE001 - policy success is independent
                                text += "；会话终止未确认，可重试禁用"
            except Exception:  # noqa: BLE001 - do not expose transport secrets
                status, text, remote_ok = "unknown", "Emby 结果未确认；请刷新核实真实状态后重试", None
                user = None
            if status == "failed":
                try:
                    user = await self.user(uid)
                except Exception:  # noqa: BLE001 - failed writes need a fresh observation
                    user = None
            self.db.execute("UPDATE restriction_events SET status=?,result=?,updated_at=? WHERE id=?", (status, text, int(time.time()), event_id))
            record_remote(self.members, uid, "manual." + action, ok=status == "done", error="" if status == "done" else text, actor=actor)
            self.members.audit(actor, "restriction.manual." + action, uid,
                               f"管理员手动操作 event={event_id} status={status} {text}", ok=status == "done")
            return response(status == "done", local_ok, remote_ok, text, user,
                            retry=status != "done", event=event_id)

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
                        f"原因：{EVENT_RULES.get(row['rule'], row['rule'])}\n"
                        f"{'操作' if row['rule'] == 'manual' else '处罚'}：{ACTIONS.get(row['action'], row['action'])}\n"
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
