"""Torrent client summaries for the intake page.

Only two numbers are wanted here: how much is still downloading, and how much
has finished but not yet moved on. The second one is what matters during an
incident — completed work that is not being consumed means the step *after*
the downloader has stalled, and that is invisible from the downloader's own UI.

Credentials never appear in this module. A client is constructed with an
already-resolved endpoint and an optional credential pair supplied by the
caller, which reads them from local configuration at startup; nothing is
logged, echoed or returned to the browser.
"""
from __future__ import annotations

from typing import Any

import httpx

# A summary is a nice-to-have on a page that must render during an incident.
# Waiting long for it would make the whole snapshot late.
TIMEOUT = 8.0
# Enough torrents to characterise a queue without pulling a huge payload from
# a client that has thousands of them.
MAX_TORRENTS = 2000


class QbittorrentClient:
    """Minimal read-only client: login if required, then one list call."""

    def __init__(self, name: str, base_url: str, username: str = "",
                 pw_value: str = "", verify_ssl: bool = True) -> None:
        self.name = name
        self._base = (base_url or "").rstrip("/")
        self._user = username or ""
        self._pw = pw_value or ""
        self._verify = verify_ssl

    async def _list(self, client: httpx.AsyncClient) -> Any:
        response = await client.get(
            f"{self._base}/api/v2/torrents/info",
            params={"limit": MAX_TORRENTS})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise TypeError("invalid downloader response: expected list")
        return data

    async def summary(self) -> dict[str, Any]:
        if not self._base:
            raise ValueError("downloader endpoint not configured")
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=self._verify) as client:
            try:
                data = await self._list(client)
            except httpx.HTTPStatusError as exc:
                # Many deployments bind the API to loopback with auth off, so
                # the login round trip is skipped until the client actually
                # refuses. Logging in unconditionally would fail on exactly
                # those hosts.
                if exc.response.status_code not in (401, 403):
                    raise
                await self._login(client)
                data = await self._list(client)
        return self.summarise(data)

    async def _login(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            f"{self._base}/api/v2/auth/login",
            data={"username": self._user, "password": self._pw},
            headers={"Referer": self._base})
        response.raise_for_status()
        if "Fails" in response.text:
            raise PermissionError("downloader rejected the credential")

    @staticmethod
    def summarise(torrents: Any) -> dict[str, Any]:
        """Counts and bytes, split by whether the payload is complete.

        "Complete" is progress >= 1 rather than a state name: state strings
        vary between client versions and seeding/paused/stalled all describe a
        finished download that something downstream still has to pick up.
        """
        if not isinstance(torrents, list):
            return {"total": 0, "downloading": 0, "completed": 0,
                    "completed_bytes": 0, "downloading_bytes": 0}
        total = downloading = completed = 0
        completed_bytes = downloading_bytes = 0
        for item in torrents:
            if not isinstance(item, dict):
                continue
            total += 1
            try:
                progress = float(item.get("progress") or 0.0)
            except (TypeError, ValueError):
                progress = 0.0
            try:
                size = max(0, int(item.get("total_size") or 0))
            except (TypeError, ValueError, OverflowError):
                size = 0
            if progress >= 1.0:
                completed += 1
                completed_bytes += size
            else:
                downloading += 1
                downloading_bytes += size
        return {
            "total": total,
            "downloading": downloading,
            "downloading_bytes": downloading_bytes,
            "completed": completed,
            "completed_bytes": completed_bytes,
        }


class MockDownloader:
    """Credential-free stand-in so the page is fully populated in mock mode."""

    def __init__(self, name: str = "mock-downloader") -> None:
        self.name = name

    async def summary(self) -> dict[str, Any]:
        return {"total": 12, "downloading": 9, "downloading_bytes": 42 * 2**30,
                "completed": 3, "completed_bytes": 7 * 2**30}
