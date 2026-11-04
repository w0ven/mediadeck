"""Narrow caller-authenticated Web login proxy; no admin key forwarding."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl

from app.modules.playback import caller_session_profile

LOGIN_FIELDS = frozenset({"Username", "Pw", "Password", "PasswordMd5"})
_CANONICAL_FIELDS = {field.lower(): field for field in LOGIN_FIELDS}


def parse_login_payload(raw_body: bytes, content_type: str = "",
                        query: dict[str, str] | None = None) -> dict[str, Any]:
    """Decode an Emby login body from JSON, form-urlencoded, or query parameters.

    Emby Web's ``apiclient.js`` posts ``application/x-www-form-urlencoded``
    (or sends ``text/plain`` when ``reqformat=json`` is set), whereas native
    players post JSON and some clients (such as Yamby) pass credentials in the
    query string with an empty body.
    """
    payload: dict[str, Any] | None = None
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    text = raw_body.decode("utf-8", errors="strict") if raw_body else ""
    stripped = text.strip()
    if stripped:
        if media_type == "application/x-www-form-urlencoded":
            payload = dict(parse_qsl(text, keep_blank_values=True))
        elif media_type == "application/json" or stripped.startswith("{"):
            parsed = json.loads(stripped)
            if not isinstance(parsed, dict):
                raise ValueError("invalid authentication body")
            payload = parsed
        elif "=" in stripped:
            payload = dict(parse_qsl(text, keep_blank_values=True))
        else:
            raise ValueError("invalid authentication body")
    else:
        payload = {}

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        target = _CANONICAL_FIELDS.get(str(key).strip().lower())
        if target and (target not in normalized or key in LOGIN_FIELDS):
            normalized[target] = value
    if query:
        for key, value in query.items():
            target = _CANONICAL_FIELDS.get(str(key).strip().lower())
            if target and target not in normalized:
                normalized[target] = value
    if not stripped and not normalized:
        raise ValueError("invalid authentication body")
    return normalized


async def login_request(emby: Any, headers: Any, query: dict[str, str],
                        payload: dict[str, Any], user_id: str = "") -> tuple[int, dict[str, Any]]:
    base, _admin_headers, timeout, verify = emby._conn()
    profile = caller_session_profile(headers, query)
    # Rebuild ONLY non-secret client identification. Never forward a token:
    # an administrative token may cause Emby to skip the password check.
    from app.modules.playback import caller_device
    fields = {"Client": profile.get("Client", ""),
              "Version": profile.get("ApplicationVersion", ""),
              "Device": profile.get("DeviceName", ""),
              "DeviceId": caller_device(headers, query)}
    if any('"' in v or '\r' in v or '\n' in v for v in fields.values()):
        return 400, {"error": "invalid client identity"}
    auth = "MediaBrowser " + ", ".join(f'{k}="{v}"' for k, v in fields.items() if v)
    path = "/emby/Users/AuthenticateByName"
    if user_id:
        if not re.fullmatch(r"[a-zA-Z0-9-]+", user_id):
            return 400, {"error": "invalid user id"}
        path = f"/emby/Users/{user_id}/Authenticate"
    body = {k: v for k, v in payload.items() if k in LOGIN_FIELDS}
    async with emby._client(timeout, verify) as client:
        response = await client.post(base + path, headers={"X-Emby-Authorization": auth}, json=body)
    if not 200 <= response.status_code < 300:
        return response.status_code, {"error": "authentication rejected"}
    data = response.json()
    if not isinstance(data, dict):
        raise TypeError("invalid authentication response")
    return response.status_code, data
