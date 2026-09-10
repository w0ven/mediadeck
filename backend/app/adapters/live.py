"""Live adapters.

Connection details are resolved *per call* from the runtime settings store, so
an operator can point the panel at a different Emby server from the UI and have
it take effect immediately — no restart, no shell access.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

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

    def __init__(self, config_provider: ConfigProvider) -> None:
        self._config = config_provider

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
                                  policy_patch: dict[str, Any]) -> MemberPolicyResult:
        return await self._apply_policy(user_id, policy_patch, protect_admin=True)

    async def _apply_policy(self, user_id: str, policy_patch: dict[str, Any], *,
                            protect_admin: bool) -> MemberPolicyResult:
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

        Anything still ambiguous returns None and the caller signs uncapped:
        guessing would apply one member's cap to another member's stream.
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

        # Exact match first: it is the only branch that stays correct when the
        # credential can see more than its own sessions.
        if device_id:
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                if str(session.get("DeviceId") or "") == device_id:
                    uid = session.get("UserId")
                    if uid:
                        return str(uid)

        uids = {
            str(session["UserId"]) for session in sessions
            if isinstance(session, dict) and session.get("UserId")
        }
        if len(uids) == 1:
            return uids.pop()
        return None

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
            return list(r.json() or [])

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
            with contextlib.suppress(httpx.HTTPError):
                await client.delete(
                    f"{base}/emby/Sessions/{session_id}", headers=headers)
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

    async def latest_items(self, limit: int = 12) -> list[dict[str, Any]]:
        """Most recently added movies and series, for the dashboard wall.

        Deliberately asks for whole titles rather than episodes: a series that
        just gained twelve episodes would otherwise fill the entire wall with
        one show's artwork and bury everything else added that day.
        """
        base, headers, timeout, verify = self._conn()
        params = {
            "Recursive": "true",
            "Limit": str(max(1, min(limit, 60))),
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "IncludeItemTypes": "Movie,Series",
            "Fields": "ProductionYear,DateCreated",
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
                if not (item.get("ImageTags") or {}).get("Primary"):
                    continue
                out.append({
                    "Id": item.get("Id"),
                    "Name": item.get("Name"),
                    "Type": item.get("Type"),
                    "ProductionYear": item.get("ProductionYear"),
                    "DateCreated": item.get("DateCreated"),
                })
            return out

    async def item_primary_image(self, item_id: str) -> bytes | None:
        """Primary poster bytes for a ranking card. Best-effort, never raises."""
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
                for candidate in (poster_id, item_id):
                    r = await client.get(
                        f"{base}/emby/Items/{candidate}/Images/Primary",
                        headers=headers, params={"maxWidth": "420", "quality": "80"})
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
