"""Operator-configured, authenticated-session client matching (not User-Agent)."""
from __future__ import annotations

import re
from typing import Any

DEFAULT = {"enabled": False, "blocked_patterns": [], "exempt_groups": []}


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw) != set(DEFAULT) or not isinstance(raw.get("enabled"), bool):
        raise ValueError("会话客户端规则须包含 enabled、blocked_patterns、exempt_groups")
    patterns, groups = raw["blocked_patterns"], raw["exempt_groups"]
    if (not isinstance(patterns, list) or len(patterns) > 64
            or not isinstance(groups, list) or len(groups) > 32):
        raise ValueError("会话客户端规则列表无效")
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
            raise ValueError("客户端匹配式无效")
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error:
            raise ValueError("客户端匹配式无效") from None
    if any(not isinstance(g, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", g) for g in groups):
        raise ValueError("豁免组无效")
    return {"enabled": raw["enabled"], "blocked_patterns": list(patterns), "exempt_groups": list(groups)}


def matches(session: dict[str, Any], config: dict[str, Any]) -> bool:
    fields = [str(session.get(k) or "") for k in ("Client", "DeviceName", "ApplicationVersion")]
    return any(re.search(p, field, re.IGNORECASE)
               for p in config["blocked_patterns"] for field in fields if field)
