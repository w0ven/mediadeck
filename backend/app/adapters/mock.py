"""Mock adapters: full panel functionality with zero real credentials."""
from __future__ import annotations

import random
import time
from typing import Any

from app.adapters.base import MemberPolicyResult


class MockEmby:
    def __init__(self) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime())
        self._users: dict[str, dict[str, Any]] = {
            "u1": {"Id": "u1", "Name": "demo-user-1", "Policy": {"IsDisabled": False},
                   "LastActivityDate": stamp},
            "u2": {"Id": "u2", "Name": "demo-user-2", "Policy": {"IsDisabled": True},
                   "LastActivityDate": stamp},
            "admin": {"Id": "admin", "Name": "demo-admin",
                      "Policy": {"IsDisabled": False, "IsAdministrator": True},
                      "LastActivityDate": stamp},
        }
        self._next = 3
        self._sessions: list[dict[str, Any]] = []
        self.stopped: list[tuple[str, str]] = []
        self.deleted_sessions: list[str] = []
        # username -> password; missing means any non-empty password is accepted
        # so existing tests that never set a password still work.
        self._passwords: dict[str, str] = {}

    async def system_info(self) -> dict[str, Any]:
        return {
            "ok": True,
            "server_name": "demo-emby",
            "version": "4.8.0.0",
            "operating_system": "Linux",
            "id": "mock-server",
        }

    async def list_users(self) -> list[dict[str, Any]]:
        return list(self._users.values())

    async def create_user(self, name: str) -> dict[str, Any]:
        uid = f"u{self._next}"
        self._next += 1
        user = {"Id": uid, "Name": name, "Policy": {"IsDisabled": False}}
        self._users[uid] = user
        return user

    async def delete_user(self, user_id: str) -> bool:
        return self._users.pop(user_id, None) is not None

    async def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        user["Policy"]["IsDisabled"] = disabled
        return True

    async def set_user_password(self, user_id: str, new_password: str) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        self._passwords[user.get("Name") or ""] = new_password
        return True

    async def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        """Return the user dict on success, None on bad credentials.

        Never raises on a wrong password: the caller maps None to a 422 so a
        public redeem form cannot distinguish 'no such user' from 'bad password'.
        """
        if not username or not password:
            return None
        user = next((u for u in self._users.values()
                     if (u.get("Name") or "").lower() == username.lower()), None)
        if not user:
            return None
        expected = self._passwords.get(user.get("Name") or "")
        if expected is None:
            return dict(user)
        if expected != password:
            return None
        return dict(user)

    async def apply_policy(self, user_id: str, policy_patch: dict[str, Any]) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        user["Policy"].update(policy_patch)
        return True

    async def apply_member_policy(self, user_id: str,
                                  policy_patch: dict[str, Any]) -> MemberPolicyResult:
        user = self._users.get(user_id)
        if not user or not isinstance(user.get('Policy'), dict):
            return {'status': 'failed'}
        if user['Policy'].get('IsAdministrator'):
            return {'status': 'skipped_admin'}
        ok = await self.apply_policy(user_id, policy_patch)
        return {'status': 'applied' if ok else 'failed'}

    async def verify_item_access(self, item_id: str, token: str) -> bool:
        # Mirrors the live adapter: only a non-empty token is ever accepted.
        return bool((token or "").strip()) and token != "invalid-token"

    async def user_for_token(self, token: str,
                             device_id: str = "") -> str | None:
        """Deterministic mock: "tok:<uid>" resolves to that user, anything
        else non-empty resolves to u1 so existing playback tests keep working.

        ``admin-key`` models the live ambiguity the real adapter has to break:
        a fleet-wide credential identifies nobody on its own, and only the
        request's ``DeviceId`` ("dev:<uid>") names the actual streamer.
        """
        token = (token or "").strip()
        if not token or token == "invalid-token":
            return None
        if token == "admin-key":
            device_id = (device_id or "").strip()
            if device_id.startswith("dev:"):
                uid = device_id[4:]
                return uid if uid in self._users else None
            return None
        if token.startswith("tok:"):
            uid = token[4:]
            return uid if uid in self._users else None
        return "u1"

    async def item_media_paths(self, item_id: str) -> dict[str, str]:
        if item_id == "unknown":
            return {}
        return {
            f"src-{item_id}": f"/media/Movies/Demo/{item_id}.mkv",
            f"src-{item_id}-alt": f"/media/Movies/Demo/{item_id}.alt.mkv",
        }

    async def libraries(self) -> list[dict[str, Any]]:
        return [
            {"id": "lib-movies", "name": "demo-movies", "type": "movies",
             "items": 120, "locations": 2},
            {"id": "lib-series", "name": "demo-series", "type": "tvshows",
             "items": 340, "locations": 3},
        ]

    async def active_sessions_raw(self) -> list[dict[str, Any]]:
        """Full session objects, shaped like Emby's, for usage accounting.

        Injectable so tests can drive the sampler deterministically instead of
        depending on random demo data.
        """
        return list(self._sessions)

    def set_sessions(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = list(sessions)

    async def stop_session(self, session_id: str, reason: str = "") -> bool:
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions if s.get("Id") != session_id]
        self.stopped.append((session_id, reason))
        return len(self._sessions) < before

    async def delete_session(self, session_id: str) -> bool:
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions if s.get("Id") != session_id]
        self.deleted_sessions.append(session_id)
        return len(self._sessions) < before

    async def sessions_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [s for s in self._sessions if s.get("UserId") == user_id]

    async def active_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "Id": "mock-session-1",
                "UserId": "u1",
                "UserName": "demo-user-1",
                "Client": "Demo Player",
                "PlayMethod": "DirectStream",
                "Item": "Demo Show S01E01",
                "ItemId": "mock-item-1",
                "ItemType": "Episode",
                "ProductionYear": 2024,
                "Genres": ["Demo"],
                "Overview": "Demo overview for the mock session.",
                "RunTimeTicks": 36_000_000_000,
                "PositionTicks": 9_000_000_000,
                "ProgressPercent": 25.0,
            }
        ]

    async def latest_items(self, limit: int = 12) -> list[dict[str, Any]]:
        items = [
            {"Id": "mock-item-1", "Name": "Demo Movie One", "Type": "Movie",
             "ProductionYear": 2024, "DateCreated": "2026-09-01T10:00:00Z"},
            {"Id": "mock-item-2", "Name": "Demo Series Two", "Type": "Series",
             "ProductionYear": 2023, "DateCreated": "2026-08-31T10:00:00Z"},
        ]
        return items[:max(1, limit)]

    async def request_lookup(self, media_type: str, tmdb_id: int, user_id: str | None = None) -> list[dict[str, Any]]:
        return []

    # -- intake observability ------------------------------------------------
    async def scheduled_tasks(self) -> list[dict[str, Any]]:
        return [
            {"Key": "ScanExternalTrackTask", "Name": "Scan External Tracks",
             "State": "Idle",
             "LastExecutionResult": {"Status": "Completed",
                                     "EndTimeUtc": "2026-01-01T00:00:00.0000000Z"}},
            {"Key": "RefreshLibrary", "Name": "Scan media library",
             "State": "Running", "CurrentProgressPercentage": 42.5,
             "LastExecutionResult": {"Status": "Completed",
                                     "EndTimeUtc": "2026-01-01T00:00:00.0000000Z"}},
        ]

    async def latest_created(self, limit: int = 1) -> dict[str, Any]:
        now = time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime())
        return {"TotalRecordCount": 1,
                "Items": [{"Id": "mock-item-1", "Name": "Demo Episode",
                           "Type": "Episode", "DateCreated": now}][:max(1, limit)]}

    async def server_log_tail(self, max_bytes: int = 512_000,
                              name: str = "embyserver.txt") -> str:
        line = ("2026-01-01 00:00:00.000 Info MediaProbeManager: ProcessRun "
                "'ffprobe' Execute: /bin/ffprobe -i file:\"/media/Demo/Show/a.mkv\" "
                "-threads 0")
        other = line.replace("/media/Demo/Show/a.mkv", "/media/Other/Film/b.mkv")
        return "\n".join([line] * 6 + [other] * 4)[-max_bytes:]


class MockProbe:
    """Simulates per-node /load probes with drifting load values."""

    def __init__(self) -> None:
        self._seed = time.monotonic()

    async def load(self, probe_url: str) -> dict[str, Any]:
        jitter = (hash(probe_url) % 100) / 10
        t = time.monotonic() - self._seed
        return {
            "ok": True,
            "active_streams": int(5 + jitter + 3 * abs(__import__("math").sin(t / 30))),
            "egress_mbps": round(50 + jitter * 20 + random.uniform(-5, 5), 1),
        }
