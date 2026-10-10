"""Live adapters.

Connection details are resolved *per call* from the runtime settings store, so
an operator can point the panel at a different Emby server from the UI and have
it take effect immediately — no restart, no shell access.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.base import MemberPolicyResult
from app.core.errors import EmbyNotConfigured, UpstreamError

ConfigProvider = Callable[[], dict[str, Any]]


def normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


async def probe_emby(
    url: str, api_key: str, timeout: float = 15.0, verify_ssl: bool = True
) -> dict[str, Any]:
    """Validate a candidate Emby connection without persisting it.

    Used by the settings UI "test connection" button, so the operator gets a
    concrete answer before saving credentials.
    """
    base = normalize_base_url(url)
    if not base:
        raise EmbyNotConfigured("请填写 Emby 地址")
    if not api_key.strip():
        raise EmbyNotConfigured("请填写 Emby API Key")
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify_ssl) as client:
            response = await client.get(
                f"{base}/emby/System/Info", headers={"X-Emby-Token": api_key.strip()}
            )
    except httpx.HTTPError as exc:
        raise UpstreamError(f"无法连接: {exc.__class__.__name__}") from None
    if response.status_code in (401, 403):
        raise UpstreamError("API Key 无效或权限不足")
    if response.status_code != 200:
        raise UpstreamError(f"Emby 返回 HTTP {response.status_code}")
    try:
        info = response.json()
    except ValueError:
        raise UpstreamError("返回内容不是有效 JSON，请确认地址指向 Emby 服务") from None
    if not isinstance(info, dict):
        raise UpstreamError("返回内容不是有效 Emby 信息，请确认地址指向 Emby 服务")
    return {
        "ok": True,
        "server_name": info.get("ServerName"),
        "version": info.get("Version"),
        "operating_system": info.get("OperatingSystem"),
        "id": info.get("Id"),
    }


class LiveEmby:
    """Emby adapter bound to a settings provider rather than frozen env vars."""

    def __init__(self, config_provider: ConfigProvider, *,
                 identity_data_dir: str = "", identity_url: str = "") -> None:
        self._config = config_provider
        self._identity_data_dir = identity_data_dir
        self._identity_url = normalize_base_url(identity_url)

    # -- connection ----------------------------------------------------------
    def _conn(self) -> tuple[str, dict[str, str], float, bool]:
        cfg = self._config() or {}
        base = normalize_base_url(cfg.get("url", ""))
        api_key = (cfg.get("api_key") or "").strip()
        if not cfg.get("enabled"):
            raise EmbyNotConfigured("Emby 集成未启用，请在「系统设置」中连接 Emby")
        if not base or not api_key:
            raise EmbyNotConfigured("Emby 尚未配置，请在「系统设置」中填写地址和 API Key")
        timeout = float(cfg.get("timeout_seconds") or 15)
        verify = bool(cfg.get("verify_ssl", True))
        return base, {"X-Emby-Token": api_key}, timeout, verify

    def _client(self, timeout: float, verify: bool) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, verify=verify)

    @staticmethod
    def _check(response: httpx.Response) -> httpx.Response:
        if response.status_code in (401, 403):
            raise UpstreamError("Emby 拒绝了请求：API Key 无效或权限不足")
        if response.status_code >= 500:
            raise UpstreamError(f"Emby 服务异常 (HTTP {response.status_code})")
        return response

    async def system_info(self) -> dict[str, Any]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            try:
                r = self._check(await client.get(f"{base}/emby/System/Info", headers=headers))
            except httpx.HTTPError as exc:
                raise UpstreamError(f"无法连接 Emby: {exc.__class__.__name__}") from None
            r.raise_for_status()
            info = r.json()
        return {
            "ok": True,
            "server_name": info.get("ServerName"),
            "version": info.get("Version"),
            "operating_system": info.get("OperatingSystem"),
            "id": info.get("Id"),
        }

    # -- users ---------------------------------------------------------------
    async def list_users(self) -> list[dict[str, Any]]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(f"{base}/emby/Users", headers=headers))
            r.raise_for_status()
            return r.json()

    async def create_user(self, name: str) -> dict[str, Any]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.post(f"{base}/emby/Users/New", headers=headers, json={"Name": name})
            )
            r.raise_for_status()
            return r.json()

    async def delete_user(self, user_id: str) -> bool:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.post(f"{base}/emby/Users/{user_id}/Delete", headers=headers)
            )
            return r.status_code in (200, 204)

    async def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        return await self.apply_policy(user_id, {"IsDisabled": disabled})

    async def set_user_password(self, user_id: str, new_password: str) -> bool:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.post(
                    f"{base}/emby/Users/{user_id}/Password",
                    headers=headers,
                    json={"Id": user_id, "NewPw": new_password, "ResetPassword": False},
                )
            )
            return r.status_code in (200, 204)

    async def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        """Validate Emby credentials for the public redeem form.

        Uses AuthenticateByName so a wrong password is just a 401, never an
        exception that would leak whether the username exists. The admin API
        key is deliberately omitted: sending it can make Emby skip the password
        check and accept any username.
        """
        if not (username or "").strip() or not password:
            return None
        base, _headers, timeout, verify = self._conn()
        client_auth = (
            'MediaBrowser Client="mediadeck", Device="redeem", '
            'DeviceId="mediadeck-redeem", Version="0.1.0"'
        )
        try:
            async with self._client(timeout, verify) as client:
                r = await client.post(
                    f"{base}/emby/Users/AuthenticateByName",
                    headers={"X-Emby-Authorization": client_auth},
                    json={"Username": username.strip(), "Pw": password},
                )
        except httpx.HTTPError:
            return None
        if r.status_code in (401, 403):
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json() or {}
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        user = data.get("User") or {}
        if not isinstance(user, dict) or not user.get("Id"):
            return None
        return user

    async def apply_policy(self, user_id: str, policy_patch: dict[str, Any]) -> bool:
        # Explicit Web/operator edits retain their existing semantics.
        result = await self._apply_policy(user_id, policy_patch, protect_admin=False)
        return result['status'] == 'applied'

    async def apply_member_policy(self, user_id: str,
                                  policy_patch: dict[str, Any], *,
                                  authorize: Any = None) -> MemberPolicyResult:
        return await self._apply_policy(user_id, policy_patch, protect_admin=True,
                                        authorize=authorize)

    async def _apply_policy(self, user_id: str, policy_patch: dict[str, Any], *,
                            protect_admin: bool, authorize: Any = None) -> MemberPolicyResult:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(f"{base}/emby/Users/{user_id}", headers=headers))
            if r.status_code != 200:
                return {'status': 'failed'}
            body = r.json()
            if not isinstance(body, dict):
                return {'status': 'failed'}
            if protect_admin and not isinstance(body.get('Policy'), dict):
                return {'status': 'failed'}
            policy = dict(body.get('Policy') or {})
            if protect_admin and policy.get('IsAdministrator'):
                return {'status': 'skipped_admin'}
            if authorize is not None:
                if str(body.get('Id') or '') != str(user_id):
                    return {'status': 'failed'}
                if not authorize():
                    return {'status': 'skipped_authority'}
                if policy_patch.get('IsDisabled') is True and policy.get('IsAdministrator') is not False:
                    return {'status': 'failed'}
            policy.update(policy_patch)
            pr = self._check(
                await client.post(
                    f"{base}/emby/Users/{user_id}/Policy", headers=headers, json=policy
                )
            )
            return {'status': 'applied' if pr.status_code in (200, 204) else 'failed'}

    # -- playback ------------------------------------------------------------
    async def verify_item_access(self, item_id: str, token: str) -> bool:
        """Does the *caller's* own Emby credential grant access to this item?

        The panel sits on the playback path, so it must not become a way around
        Emby's own authentication: without this check anyone could guess an
        item id and be handed a signed media URL with no login at all.

        Asking Emby with the caller's token also covers per-user library
        permissions -- a user who cannot see a library gets no item back, so
        they cannot obtain a link to it.
        """
        base, _, timeout, verify = self._conn()
        token = (token or "").strip()
        if not token:
            return False
        try:
            async with self._client(timeout, verify) as client:
                r = await client.get(
                    f"{base}/emby/Items",
                    headers={"X-Emby-Token": token},
                    params={"Ids": item_id, "Limit": "1"},
                )
        except httpx.HTTPError:
            return False
        if r.status_code != 200:
            return False
        try:
            data = r.json()
        except ValueError:
            return False
        items = data.get("Items") if isinstance(data, dict) else None
        return isinstance(items, list) and any(
            isinstance(item, dict) and str(item.get("Id") or "") == str(item_id)
            for item in items
        )

    async def user_for_token(self, token: str,
                             device_id: str = "") -> str | None:
        """Resolve a caller's playback credential to their Emby user id.

        Needed to look up the member's bandwidth cap when signing a node URL.
        A wrong answer here is not cosmetic: an unresolved caller is signed
        ``r=0`` with an empty user tag, which silently disables *both*
        per-user rate limiting and per-user speed attribution.

        Emby is not Jellyfin, and two plausible-looking lookups do not work:

        * ``/emby/Users/Me`` does not exist on Emby. It routes into the
          by-id handler, which tries to parse ``"Me"`` as a Guid and answers
          **500 Unrecognized Guid format** for every token.
        * ``/emby/Sessions`` never populates ``AccessToken``, so matching a
          session by token can never succeed either.

        What does work is ``/emby/Sessions?api_key=<token>``, because Emby
        scopes that response to the credential presented:

        * a *user* token sees only its own sessions -> exactly one distinct
          ``UserId``, which is its owner;
        * an *admin* api_key sees the whole fleet, so the owner is ambiguous
          from the token alone and must be identified by the ``DeviceId``
          the playback request carried.

        Anything still ambiguous returns None; playback admission refuses an
        unresolved caller. Guessing would apply one member's cap or punishment
        to another member's stream. DeviceId itself is not a secret credential.
        """
        token = (token or "").strip()
        if not token:
            return None
        device_id = (device_id or "").strip()
        base, _, timeout, verify = self._conn()
        try:
            async with self._client(timeout, verify) as client:
                s = await client.get(f"{base}/emby/Sessions",
                                     params={"api_key": token})
                if s.status_code != 200:
                    return None
                try:
                    sessions = s.json() or []
                except ValueError:
                    return None
        except httpx.HTTPError:
            return None
        if not isinstance(sessions, list):
            return None

        # A shared credential may see several users with the same DeviceId.
        # List order cannot authenticate one of them; every exact match must
        # agree on one owner. Same-owner duplicate sessions remain resolvable.
        if device_id:
            matches = [session for session in sessions if isinstance(session, dict)
                       and str(session.get("DeviceId") or "") == device_id]
            if matches:
                owners = {str(session["UserId"]) for session in matches if session.get("UserId")}
                if len(owners) == 1 and all(session.get("UserId") for session in matches):
                    return owners.pop()
                return None

        uids = {
            str(session["UserId"]) for session in sessions
            if isinstance(session, dict) and session.get("UserId")
        }
        if len(uids) == 1:
            return uids.pop()
        return None

    def _local_personal_owner(self, token: str) -> str | None:
        """Read Emby's current credential authority; never infer from Sessions.

        Tokens_2.UserId is an internal integer, not the public user GUID.
        Both databases are operator-configured and opened read-only. No owner
        cache: revocation/member changes must be effective on the next call.
        """
        root = Path(self._identity_data_dir).expanduser().resolve()
        try:
            with contextlib.closing(sqlite3.connect(
                    (root / "authentication.db").as_uri() + "?mode=ro",
                    uri=True, timeout=1)) as auth:
                auth.execute("PRAGMA query_only=ON")
                rows = auth.execute(
                    "SELECT UserId, IsActive FROM Tokens_2 WHERE AccessToken=? LIMIT 2",
                    (token,)).fetchall()
            if (len(rows) != 1 or rows[0][1] != 1
                    or type(rows[0][0]) is not int or rows[0][0] <= 0):
                return None
            owner = rows[0][0]
            with contextlib.closing(sqlite3.connect(
                    (root / "users.db").as_uri() + "?mode=ro",
                    uri=True, timeout=1)) as users:
                users.execute("PRAGMA query_only=ON")
                rows = users.execute(
                    "SELECT guid FROM LocalUsersv2 WHERE Id=? LIMIT 2", (owner,)).fetchall()
                if len(rows) != 1:
                    return None
                raw_guid = rows[0][0]
                if isinstance(raw_guid, bytes) and len(raw_guid) == 16:
                    # Emby persists System.Guid.ToByteArray() (.NET endian).
                    identity = uuid.UUID(bytes_le=raw_guid)
                elif (isinstance(raw_guid, str) and re.fullmatch(
                        r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", raw_guid)):
                    identity = uuid.UUID(raw_guid)
                else:
                    return None
                guid = identity.hex
                if identity.int == 0:
                    return None
                matches = users.execute(
                    "SELECT Id FROM LocalUsersv2 WHERE guid=? OR "
                    "lower(replace(CAST(guid AS TEXT), '-', ''))=? LIMIT 2",
                    (identity.bytes_le, guid)).fetchall()
                return guid if matches == [(owner,)] else None
        except (sqlite3.Error, OSError, ValueError):
            # Neither paths nor credential-bearing sqlite errors leave here.
            raise RuntimeError("local personal identity authority unavailable") from None

    async def personal_user_for_token(self, token: str) -> str | None:
        """Restricted routes never select an owner by a caller's DeviceId.

        Auth/Keys is an Emby API-key authority: ordinary personal credentials
        get 403; management credentials can list keys. Refuse any matching API
        key even when only one user's sessions happen to exist. A configured
        local authority binds active personal tokens to their exact owner,
        including admins who can see global Sessions; no role exemption.
        Without it the existing scoped-session check remains fail-closed.
        """
        token = (token or "").strip()
        if not token or len(token) > 2048:
            return None
        base, _, timeout, verify = self._conn()
        headers = {"X-Emby-Token": token}
        async with self._client(timeout, verify) as client:
            key_reply = await client.get(f"{base}/emby/Auth/Keys", headers=headers)
            if key_reply.status_code == 401:
                return None
            if key_reply.status_code == 200:
                keys = key_reply.json()
                if not isinstance(keys, dict) or not isinstance(keys.get("Items"), list):
                    raise RuntimeError("credential authority unavailable")
                if any(isinstance(k, dict) and str(k.get("AccessToken") or "") == token
                       for k in keys["Items"]):
                    return None
            elif key_reply.status_code != 403:
                raise RuntimeError("credential authority unavailable")
            if self._identity_data_dir or self._identity_url:
                if (not self._identity_data_dir or not self._identity_url
                        or normalize_base_url(base) != self._identity_url):
                    raise RuntimeError("local personal identity authority URL mismatch")
                return await asyncio.to_thread(self._local_personal_owner, token)
            reply = await client.get(f"{base}/emby/Sessions", headers=headers)
            if reply.status_code in (401, 403):
                return None
            if reply.status_code != 200:
                raise RuntimeError("credential identity unavailable")
            sessions = reply.json()
        if not isinstance(sessions, list):
            raise TypeError("credential identity unavailable")
        if any(not isinstance(s, dict) for s in sessions):
            return None
        # Emby includes unauthenticated discovery sessions without UserId.
        # They identify nobody; never use their DeviceId to choose an owner.
        owners = {str(s["UserId"]) for s in sessions if s.get("UserId")}
        return owners.pop() if len(owners) == 1 else None

    async def item_media_paths(self, item_id: str) -> dict[str, str]:
        """Map MediaSourceId -> on-disk file path for one item.

        This is what lets playback interception key affinity on the actual
        file rather than on the request URL, so every client watching the
        same title converges on the same node.
        """
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(
                f"{base}/emby/Items",
                headers=headers,
                params={"Ids": item_id, "Fields": "MediaSources,Path", "Recursive": "true"},
            ))
            r.raise_for_status()
            items = (r.json() or {}).get("Items") or []
            # Items exposes only the default file for grouped editions on Emby.
            # PlaybackInfo lists the exact source IDs offered to the player.
            # This is metadata-only GET: do not issue a playback session here.
            try:
                playback = self._check(await client.get(
                    f"{base}/emby/Items/{quote(str(item_id), safe='')}/PlaybackInfo",
                    headers=headers,
                ))
                playback.raise_for_status()
                payload = playback.json()
                editions = payload.get("MediaSources") if isinstance(payload, dict) else []
                if not isinstance(editions, list):
                    editions = []
            except (httpx.HTTPError, UpstreamError, ValueError):
                # Preserve existing primary-file routing if edition lookup is
                # unavailable. Unknown explicit sources still fail closed.
                editions = []
        out: dict[str, str] = {}
        for item in items:
            for source in item.get("MediaSources") or []:
                path = source.get("Path")
                source_id = source.get("Id")
                # Remote/streamed sources have no local file to hand to a node.
                if path and source_id and not str(path).lower().startswith("http"):
                    out[str(source_id)] = str(path)
            if not out and item.get("Path"):
                out[str(item_id)] = str(item["Path"])
        # Append grouped editions without changing the default-first ordering
        # used by clients which omit MediaSourceId. Never invent ID aliases or
        # hand a remote URL to a local-file playback node.
        for source in editions:
            if not isinstance(source, dict):
                continue
            path = source.get("Path")
            source_id = source.get("Id")
            if path and source_id and not str(path).lower().startswith("http"):
                out[str(source_id)] = str(path)
        return out

    # -- library -------------------------------------------------------------
    async def libraries(self) -> list[dict[str, Any]]:
        base, headers, timeout, verify = self._conn()
        async with self._client(max(timeout, 30), verify) as client:
            r = self._check(
                await client.get(f"{base}/emby/Library/VirtualFolders", headers=headers)
            )
            r.raise_for_status()
            out = []
            for folder in r.json():
                item_id = folder.get("ItemId") or folder.get("Id")
                count = None
                if item_id:
                    cr = await client.get(
                        f"{base}/emby/Items",
                        headers=headers,
                        params={
                            "ParentId": item_id,
                            "Recursive": "true",
                            "IncludeItemTypes": "Movie,Series",
                            "Limit": "0",
                        },
                    )
                    if cr.status_code == 200:
                        count = cr.json().get("TotalRecordCount")
                out.append({
                    "id": str(item_id or folder.get("Name") or ""),
                    "name": folder.get("Name"),
                    "type": folder.get("CollectionType") or "mixed",
                    "items": count,
                    "locations": len(folder.get("Locations") or []),
                })
            return out

    async def active_sessions_raw(self) -> list[dict[str, Any]]:
        """Full session objects, unfiltered.

        Usage accounting needs fields the dashboard view drops (UserId,
        DeviceId, PlayState, TranscodingInfo), and device tracking needs to see
        idle sessions too, so it cannot reuse active_sessions().
        """
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(f"{base}/emby/Sessions", headers=headers))
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or any(not isinstance(s, dict) for s in data):
                raise ValueError("invalid Emby session response")
            return data

    async def playback_info(self, item: str, method: str, headers: dict[str, str],
                            query: dict[str, str], payload: dict[str, Any] | None
                            ) -> tuple[int, dict[str, Any]]:
        base, _, timeout, verify = self._conn()
        # Caller headers only: never substitute the configured admin API key.
        forwarded = {k: v for k, v in headers.items() if k.lower() not in
                     {"host", "content-length", "connection", "transfer-encoding", "accept-encoding",
                      "x-mediadeck-entry", "x-mediadeck-entry-key", "x-original-uri", "x-original-method"}}
        async with self._client(timeout, verify) as client:
            r = await client.request(method, f"{base}/emby/Items/{quote(item, safe='')}/PlaybackInfo",
                                     headers=forwarded, params=query, json=payload)
            # HEAD has no representation body, including successful responses.
            # Preserve its status without inventing a PlaySessionId or parsing
            # empty JSON. The admission layer still guards this entrance.
            if method.upper() == "HEAD":
                return r.status_code, {}
            try:
                data = r.json()
            except ValueError:
                if r.is_success:
                    raise
                data = {"Message": "PlaybackInfo unavailable"}
            if not isinstance(data, dict):
                raise TypeError("invalid PlaybackInfo response")
            return r.status_code, data

    async def short_catalogue_info(self, item: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        """Real caller-scoped catalogue only; never open an unsigned STRM on ca1."""
        base, _, timeout, verify = self._conn()
        forwarded = {k: v for k, v in headers.items() if k.lower() not in
                     {"host", "content-length", "connection", "transfer-encoding", "accept-encoding",
                      "x-mediadeck-entry", "x-mediadeck-entry-key", "x-original-uri", "x-original-method"}}
        async with self._client(timeout, verify) as client:
            reply = await client.get(f"{base}/emby/Items", headers=forwarded,
                                     params={"Ids": item, "Limit": "1", "Fields": "MediaSources,MediaStreams,Path"})
        if reply.status_code != 200:
            return reply.status_code, {}
        document = reply.json()
        items = document.get("Items") if isinstance(document, dict) else None
        rows = [r for r in items or [] if isinstance(r, dict) and str(r.get("Id") or "") == item]
        if len(rows) != 1 or not isinstance(rows[0].get("MediaSources"), list) or not rows[0]["MediaSources"]:
            raise ValueError("short catalogue source unavailable")
        return 200, {"MediaSources": rows[0]["MediaSources"]}

    async def media_sources_for_token(self, item: str, token: str) -> list[dict[str, Any]]:
        """Read source identity with the caller's credential, without a play lease."""
        base, _, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            reply = await client.get(f"{base}/emby/Items/{quote(item, safe='')}/PlaybackInfo",
                                     headers={"X-Emby-Token": token})
        if reply.status_code != 200:
            raise RuntimeError("media source verification unavailable")
        data = reply.json()
        sources = data.get("MediaSources") if isinstance(data, dict) else None
        if not isinstance(sources, list) or not sources:
            raise RuntimeError("media source verification unavailable")
        return sources

    async def playback_manifest(self, path: str, headers: Any,
                                query: dict[str, str]) -> tuple[int, str]:
        """Bounded manifest metadata only; never proxy a video representation."""
        from app.modules.playback import caller_device, caller_token
        if not path.lower().endswith(".m3u8"):
            raise ValueError("unsupported manifest path")
        token = caller_token(headers, query)
        if not token:
            raise ValueError("manifest credential required")
        base, _, timeout, verify = self._conn()
        own_headers = {"X-Emby-Token": token,
                       "X-Emby-Device-Id": caller_device(headers, query)}
        own_query = {k: v for k, v in query.items() if k.lower() not in
                     {"md_route", "api_key", "apikey", "x-emby-token", "x-mediabrowser-token"}}
        url_path = path if path.lower().startswith("/emby/") else "/emby" + path
        body = bytearray()
        async with (
            self._client(max(timeout, 90), verify) as client,
            client.stream("GET", base + url_path, headers=own_headers, params=own_query) as reply,
        ):
            if reply.status_code != 200:
                return reply.status_code, ""
            async for chunk in reply.aiter_bytes():
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError("manifest too large")
        text = body.decode("utf-8", "strict")
        if not text.startswith("#EXTM3U"):
            raise ValueError("invalid manifest")
        return 200, text

    async def report_playback(self, event: str, headers: dict[str, str],
                              query: dict[str, str], payload: dict[str, Any]) -> int:
        base, _, timeout, verify = self._conn()
        suffix = {"started": "", "progress": "/Progress"}[event]
        forwarded = {k: v for k, v in headers.items() if k.lower() not in
                     {"host", "content-length", "connection", "transfer-encoding", "accept-encoding",
                      "x-mediadeck-entry", "x-mediadeck-entry-key", "x-original-uri", "x-original-method"}}
        async with self._client(timeout, verify) as client:
            r = await client.post(f"{base}/emby/Sessions/Playing{suffix}",
                                  headers=forwarded, params=query, json=payload)
            return r.status_code

    async def report_stopped(self, token: str, device: str, payload: dict[str, Any]) -> int:
        base, _, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = await client.post(f"{base}/emby/Sessions/Playing/Stopped",
                                  headers={"X-Emby-Token": token, "X-Emby-Device-Id": device},
                                  json=payload)
            return r.status_code

    async def stop_session(self, session_id: str, reason: str = "") -> bool:
        """End a playback session.

        Disabling an account does not interrupt a stream that already started,
        so a member who exhausts their quota mid-film would otherwise watch it
        to the end. The message is best-effort: not every client renders it.
        """
        if not session_id:
            return False
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            if reason:
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(
                        f"{base}/emby/Sessions/{session_id}/Message",
                        headers=headers,
                        json={"Text": reason, "Header": "mediadeck", "TimeoutMs": 8000},
                    )
            r = await client.post(
                f"{base}/emby/Sessions/{session_id}/Playing/Stop", headers=headers)
            stopped = r.status_code in (200, 204)
            # HTTP acceptance is not proof the client stopped. Do not delete
            # the session: that would hide a still-playing client from admission.
            return stopped

    async def delete_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = await client.delete(
                f"{base}/emby/Sessions/{session_id}", headers=headers)
            return r.status_code in (200, 204)

    async def sessions_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [s for s in await self.active_sessions_raw()
                if s.get("UserId") == user_id]

    async def active_sessions(self) -> list[dict[str, Any]]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(f"{base}/emby/Sessions", headers=headers))
            r.raise_for_status()
            out = []
            for session in r.json():
                item = session.get("NowPlayingItem")
                if not item:
                    continue
                play_state = session.get("PlayState") or {}
                # Position and runtime travel together or not at all: a percentage
                # derived from a missing runtime would render a full bar for a
                # session that just started.
                runtime = item.get("RunTimeTicks") or 0
                position = play_state.get("PositionTicks") or 0
                percent = round(position * 100 / runtime, 1) if runtime > 0 else None
                out.append({
                    "Id": session.get("Id"),
                    "UserId": session.get("UserId"),
                    "UserName": session.get("UserName"),
                    "Client": session.get("Client"),
                    "DeviceName": session.get("DeviceName"),
                    "PlayMethod": play_state.get("PlayMethod"),
                    "Item": item.get("Name"),
                    "SeriesName": item.get("SeriesName"),
                    "Paused": bool(play_state.get("IsPaused")),
                    # Artwork and progress. ItemId is what lets the panel address
                    # the cached-image route; without it the UI can only print
                    # a title where the poster should be.
                    "ItemId": item.get("Id"),
                    "ItemType": item.get("Type"),
                    "PosterItemId": (item.get('SeriesId') if item.get('Type') == 'Episode'
                                     and item.get('SeriesId') and item.get('SeriesPrimaryImageTag') else
                                     item.get('ParentPrimaryImageItemId') or item.get('Id')),
                    "PosterImageTag": (item.get('SeriesPrimaryImageTag') if item.get('Type') == 'Episode'
                                       and item.get('SeriesId') and item.get('SeriesPrimaryImageTag') else
                                       item.get('ParentPrimaryImageTag') or (item.get('ImageTags') or {}).get('Primary')),
                    "PosterKind": ('series' if item.get('Type') == 'Episode'
                                   and item.get('SeriesId') and item.get('SeriesPrimaryImageTag')
                                   else 'item'),
                    "PosterAspectRatio": (None if item.get('Type') == 'Episode'
                                          and item.get('SeriesId') and item.get('SeriesPrimaryImageTag')
                                          else item.get('PrimaryImageAspectRatio')),
                    "ProductionYear": item.get("ProductionYear"),
                    "Genres": (item.get("Genres") or [])[:2],
                    "Overview": item.get("Overview") or "",
                    "RunTimeTicks": runtime,
                    "PositionTicks": position,
                    "ProgressPercent": percent,
                })
            return out

    async def latest_items(self, limit: int = 12, *, watch: bool = False) -> list[dict[str, Any]]:
        """Most recently added movies and series, for the dashboard wall.

        Deliberately asks for whole titles rather than episodes: a series that
        just gained twelve episodes would otherwise fill the entire wall with
        one show's artwork and bury everything else added that day.

        ``watch=True`` is the request-library poller: keep items without
        artwork, include ProviderIds so callers can reverse-match TMDB ids,
        and allow a larger window. Dashboard callers stay on the default path.
        """
        base, headers, timeout, verify = self._conn()
        fields = "ProductionYear,DateCreated"
        if watch:
            fields += ",ProviderIds"
        params = {
            "Recursive": "true",
            "Limit": str(max(1, min(limit, 100 if watch else 60))),
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "IncludeItemTypes": "Movie,Series",
            "Fields": fields,
            "ImageTypeLimit": "1",
            "EnableImageTypes": "Primary",
        }
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.get(f"{base}/emby/Items", headers=headers, params=params))
            r.raise_for_status()
            out = []
            for item in (r.json().get("Items") or []):
                # An entry with no Primary tag has no artwork to show; keeping it
                # would punch a grey hole in an otherwise dense grid.
                if not watch and not (item.get("ImageTags") or {}).get("Primary"):
                    continue
                row = {
                    "Id": item.get("Id"),
                    "Name": item.get("Name"),
                    "Type": item.get("Type"),
                    "ProductionYear": item.get("ProductionYear"),
                    "DateCreated": item.get("DateCreated"),
                }
                providers = item.get("ProviderIds") or {}
                raw = providers.get("Tmdb", providers.get("tmdb"))
                try:
                    tmdb_id = int(raw)
                except (TypeError, ValueError):
                    tmdb_id = None
                if tmdb_id and tmdb_id > 0:
                    row["tmdb_id"] = tmdb_id
                if watch:
                    row["ProviderIds"] = providers
                out.append(row)
            return out

    async def request_lookup(self, media_type: str, tmdb_id: int, user_id: str | None = None) -> list[dict[str, Any]]:
        """Read-only advisory lookup, scoped to the requester's accessible library."""
        from urllib.parse import quote
        base, headers, timeout, verify = self._conn()
        params = {'Recursive': 'true', 'IncludeItemTypes': 'Movie' if media_type == 'movie' else 'Series',
                  'AnyProviderIdEquals': f'tmdb.{int(tmdb_id)}', 'Fields': 'ProviderIds', 'Limit': '20'}
        if user_id:
            params['UserId'] = str(user_id)
        async with self._client(timeout, verify) as client:
            response = self._check(await client.get(f'{base}/emby/Items', headers=headers, params=params))
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                raise UpstreamError('媒体库返回了不完整的查询结果')
            out = []
            for item in data['Items']:
                provider_ids = item.get('ProviderIds') or {}
                provider_id = provider_ids.get('Tmdb', provider_ids.get('tmdb'))
                if provider_id is None:
                    raise UpstreamError('媒体库结果缺少 TMDB 标识，无法确认已有内容')
                if str(provider_id) != str(tmdb_id):
                    continue
                row = {'id': str(item['Id']), 'name': str(item.get('Name') or ''),
                       'url': f"{base}/web/index.html#!/item?id={quote(str(item['Id']), safe='')}", 'episodes': []}
                if media_type == 'tv':
                    ep_params = {'ParentId': str(item['Id']), 'Recursive': 'true',
                                 'IncludeItemTypes': 'Episode', 'Fields': 'ParentIndexNumber,IndexNumber', 'Limit': '500'}
                    if user_id:
                        ep_params['UserId'] = str(user_id)
                    ep = self._check(await client.get(f'{base}/emby/Items', headers=headers, params=ep_params))
                    ep.raise_for_status()
                    data = ep.json()
                    if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                        raise UpstreamError('剧集查询未返回完整结果')
                    row['episodes'] = [{'season': e.get('ParentIndexNumber'), 'episode': e.get('IndexNumber')}
                                       for e in data['Items']]
                    row['partial'] = int(data.get('TotalRecordCount') or 0) > len(row['episodes'])
                out.append(row)
        return out

    async def item_primary_image(self, item_id: str) -> bytes | None:
        """Primary poster bytes for a ranking card. Best-effort, never raises."""
        return await self._ranking_image(item_id, 'Primary', 600)

    async def item_backdrop_image(self, item_id: str) -> bytes | None:
        """Optional real title artwork for the ranking header."""
        return await self._ranking_image(item_id, 'Backdrop/0', 1200)

    async def _ranking_image(self, item_id: str, kind: str, width: int) -> bytes | None:
        item_id = str(item_id or "").strip()
        if not item_id:
            return None
        base, headers, timeout, verify = self._conn()
        try:
            async with self._client(min(timeout, 8), verify) as client:
                info = await client.get(
                    f"{base}/emby/Items", headers=headers,
                    params={"Ids": item_id, "Limit": "1", "Fields": "SeriesId"})
                poster_id = item_id
                if info.status_code == 200:
                    items = (info.json() or {}).get("Items") or []
                    if items:
                        poster_id = str(items[0].get("SeriesId") or items[0].get("Id") or item_id)
                for candidate in dict.fromkeys((poster_id, item_id)):
                    r = await client.get(
                        f"{base}/emby/Items/{candidate}/Images/{kind}",
                        headers=headers, params={"maxWidth": str(width), "quality": "85"})
                    ctype = str(r.headers.get("content-type") or "")
                    if r.status_code == 200 and r.content and ctype.startswith("image/"):
                        return r.content
        except (httpx.HTTPError, ValueError, EmbyNotConfigured):
            return None
        return None


    # -- intake observability ------------------------------------------------
    # Three read-only calls behind the intake page. They are separate from the
    # dashboard's calls because they run on a slow timer and must stay cheap:
    # this page is opened when the server is already unwell, and a diagnostic
    # that adds load is a diagnostic that cannot be used.
    async def scheduled_tasks(self) -> list[dict[str, Any]]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.get(f"{base}/emby/ScheduledTasks", headers=headers))
            r.raise_for_status()
            data = r.json()
        return data if isinstance(data, list) else []

    async def latest_created(self, limit: int = 1) -> dict[str, Any]:
        """Newest episodes/movies by creation time.

        Episodes are included here, unlike the dashboard wall: the question is
        "did anything at all land recently", and a series that gained one
        episode is exactly the evidence wanted.
        """
        base, headers, timeout, verify = self._conn()
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Episode,Movie",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Limit": str(max(1, min(limit, 20))),
            "Fields": "DateCreated",
        }
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.get(f"{base}/emby/Items", headers=headers, params=params))
            r.raise_for_status()
            data = r.json()
        return data if isinstance(data, dict) else {}

    async def server_log_tail(self, max_bytes: int = 512_000,
                              name: str = "embyserver.txt") -> str:
        """Tail of the server log.

        The endpoint ignores Range and always returns the whole file (verified
        against the deployed server), so the tail is taken client-side. The
        response is streamed and the last ``max_bytes`` kept, which bounds
        memory even when the log is hundreds of megabytes.
        """
        base, headers, timeout, verify = self._conn()
        chunks: list[bytes] = []
        held = 0
        async with self._client(timeout, verify) as client, client.stream(
            "GET", f"{base}/emby/System/Logs/Log",
            headers=headers, params={"name": name},
        ) as response:
            self._check(response)
            response.raise_for_status()
            async for chunk in response.aiter_bytes(65536):
                chunks.append(chunk)
                held += len(chunk)
                while held - len(chunks[0]) >= max_bytes and len(chunks) > 1:
                    held -= len(chunks.pop(0))
        blob = b"".join(chunks)[-max_bytes:]
        text = blob.decode("utf-8", errors="replace")
        # Drop a partial first line so a truncated record cannot be parsed.
        return text.split("\n", 1)[1] if "\n" in text else text


class LiveProbe:
    async def load(self, probe_url: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(probe_url)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict):
                    raise TypeError("invalid probe document")
                speeds = data.get("user_speeds")
                return {
                    "ok": bool(data.get('ok', True)),
                    "active_streams": int(data.get("active_streams", 0)),
                    "egress_mbps": data.get("egress_mbps"),
                    "egress_sampled_at": data.get('egress_sampled_at'),
                    "egress_window_seconds": data.get('egress_window_seconds'),
                    "egress_ok": data.get('egress_ok', data.get('egress_mbps') is not None),
                    "user_speeds": speeds if isinstance(speeds, dict) else {},
                    "user_speeds_ok": data.get('user_speeds_ok', isinstance(speeds, dict)),
                    "user_speeds_source": data.get('user_speeds_source') or 'legacy_socket',
                    "user_speeds_sampled_at": data.get('user_speeds_sampled_at'),
                    "user_speeds_window_seconds": data.get('user_speeds_window_seconds'),
                }
        except (httpx.HTTPError, ValueError, TypeError, OverflowError, KeyError):
            return {"ok": False, "active_streams": 0, "egress_mbps": 0.0,
                    "user_speeds": {}}
