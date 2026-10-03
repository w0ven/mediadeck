"""Narrow caller-authenticated Web login proxy; no admin key forwarding."""
from __future__ import annotations

import re
from typing import Any

from app.modules.playback import caller_session_profile


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
    body = {k: v for k, v in payload.items() if k in {"Username", "Pw", "Password", "PasswordMd5"}}
    async with emby._client(timeout, verify) as client:
        response = await client.post(base + path, headers={"X-Emby-Authorization": auth}, json=body)
    if not 200 <= response.status_code < 300:
        return response.status_code, {"error": "authentication rejected"}
    data = response.json()
    if not isinstance(data, dict):
        raise TypeError("invalid authentication response")
    return response.status_code, data
