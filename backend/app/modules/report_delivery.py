"""Private report delivery receipts. Never retain Telegram response bodies or tokens."""
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

import httpx

CALL_DELIVERY: ContextVar[dict[str, Any] | None] = ContextVar("call_delivery", default=None)
MAX_ATTEMPTS = 4
BACKOFF = (300, 900, 3600)


def failure(body: Any = None, exc: Exception | None = None) -> dict[str, Any]:
    """Use fixed, safe reasons, not external error strings containing credentials."""
    if exc is not None:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return {"state": "retry", "reason": "连接失败，稍后重试"}
        # A response may have been lost AFTER Telegram accepted the message.
        return {"state": "unknown", "reason": "发送结果不明，需核实后手动重试"}
    body = body if isinstance(body, dict) else {}
    code = body.get("error_code")
    description = str(body.get("description") or "").lower()
    if code == 429:
        try:
            delay = max(1, int((body.get("parameters") or {}).get("retry_after", 60)))
        except (ValueError, TypeError, OverflowError):
            delay = 60
        return {"state": "retry", "reason": "Telegram 限流", "retry_after": delay}
    if code == 403:
        reason = "用户已屏蔽机器人" if "blocked" in description else "无法向该用户发送消息"
        if "deactivated" in description:
            reason = "Telegram 账号已注销"
        return {"state": "failed", "reason": reason}
    if code == 401:
        return {"state": "failed", "reason": "机器人凭据不可用"}
    if code == 400:
        reason = "会话不存在或用户尚未启动机器人" if "chat not found" in description else "Telegram 拒绝消息内容"
        return {"state": "failed", "reason": reason, "content_error": "chat not found" not in description}
    if isinstance(code, int) and code >= 500:
        return {"state": "retry", "reason": "Telegram 服务暂不可用"}
    return {"state": "unknown", "reason": "未取得发送确认，需核实后手动重试"}


def summary(state: dict[str, Any]) -> dict[str, Any]:
    rows = list((state.get("recipients") or {}).values())
    sent = sum(r["state"] == "sent" for r in rows)
    failed = sum(r["state"] in {"failed", "unknown"} for r in rows)
    waiting = sum(r["state"] in {"pending", "retry", "sending"} for r in rows)
    if not rows and state.get("sent"):
        sent = len(state["sent"])
    return {"ok": not failed and not waiting, "结果": (
        "部分成功" if sent and (failed or waiting) else
        "待重试" if waiting else "失败" if failed else "成功"),
        "已送达": sent, "失败待处理": failed, "等待重试": waiting}


def public_status(state: dict[str, Any]) -> dict[str, Any]:
    if not state:
        return {}
    if state.get("version") != 1:
        return {"legacy": True, "sent": len(state.get("sent") or []),
                "note": "旧批次只保留成功记录，无法可靠还原失败对象；不会自动补发。"}
    rows = [{"user_id": uid, **{k: row.get(k) for k in
             ("username", "state", "reason", "attempts", "next_at", "updated_at")}}
            for uid, row in state["recipients"].items()]
    return {"batch": state["batch"], "created_at": state["created_at"],
            "summary": summary(state), "recipients": rows,
            "retryable": any(r["state"] in {"failed", "unknown", "retry"} for r in rows)}


def record_failure(row: dict[str, Any], result: dict[str, Any], now: float) -> None:
    row.update(state=result.get("state", "unknown"),
               reason=result.get("reason", "发送结果不明"), updated_at=int(now), next_at=0)
    if row["state"] == "retry":
        if row["attempts"] >= MAX_ATTEMPTS:
            row.update(state="failed", reason=row["reason"] + "；已达自动重试上限")
        else:
            row["next_at"] = int(now + max(
                BACKOFF[min(row["attempts"] - 1, len(BACKOFF) - 1)],
                result.get("retry_after", 0)))


def is_due(state: dict[str, Any], now: float) -> bool:
    if state.get("version") != 1:
        return False
    if now - state["created_at"] >= 86400:
        # One final no-send run closes outstanding rows instead of displaying
        # 'waiting' forever. Manual retry remains an explicit operator choice.
        return any(r["state"] in {"pending", "retry", "sending"}
                   for r in state["recipients"].values())
    if now < state.get("pause_until", 0):
        return False
    return any(row["state"] in {"pending", "retry", "sending"} and now >= row.get("next_at", 0)
               for row in state["recipients"].values())


def new_batch(members: list[dict[str, Any]], config: dict[str, Any], now: float) -> dict[str, Any]:
    return {"version": 1, "batch": time.strftime("%Y-%m-%d", time.localtime(now)),
            "created_at": int(now), "config": dict(config), "pause_until": 0,
            "recipients": {str(m["emby_user_id"]): {
                "username": str(m.get("username") or "会员"),
                "tg_user_id": str(m["tg_user_id"]), "state": "pending",
                "attempts": 0, "reason": "", "next_at": 0, "updated_at": int(now)}
                for m in members if m.get("emby_user_id") and m.get("tg_user_id")}}
