"""Registered short-drama metadata and bounded original v2 node capabilities.

This is not an admission system. Only the original playback handlers call it,
AFTER personal authentication and StreamAdmission. No video is proxied here.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from app.modules.signing import sign_url, verify

MAX_WINDOW = 7200
PAUSE_MARGIN = 900
CLOCK_MARGIN = 60
BOUND_MEDIA = re.compile(r"^/s/short/hongguo/([0-9]{5,24})/([0-9]{5,24})/([0-9a-f]{32})/(index\.m3u8|segment-[0-9]{6}\.ts)$")
MEDIA = re.compile(r"^/s/short/hongguo/([0-9]{5,24})/([0-9]{5,24})/index\.m3u8$")


class ShortDramaSources:
    def __init__(self, registry_path: str = "") -> None:
        self.path = registry_path
        self.identity: tuple[int, int, int] | None = None
        self.items: dict[str, dict[str, Any]] = {}
        self.admission = None
        self._metadata: dict[str, tuple[float, float]] = {}

    def registry(self) -> dict[str, dict[str, Any]]:
        if not self.path:
            return {}
        try:
            p = Path(self.path)
            st = p.stat()
            identity = (st.st_ino, st.st_mtime_ns, st.st_size)
            if identity != self.identity:
                # Bound the current 9,174-series catalogue without removing
                # fail-closed shape/source checks or original lease semantics.
                if st.st_size > 512 * 1024 * 1024:
                    raise ValueError
                document = json.loads(p.read_text())
                items = document["items"]
                if document.get("version") != 1 or not isinstance(items, dict) or len(items) > 1_000_000:
                    raise ValueError
                for item, row in items.items():
                    if (not re.fullmatch(r"[A-Za-z0-9_-]+", item) or not isinstance(row, dict)
                            or row.get("node") != "nc1" or not MEDIA.fullmatch(row.get("media_path", ""))
                            or not isinstance(row.get("source_ids"), list) or not row["source_ids"]
                            or not isinstance(row.get("paths"), list) or not row["paths"]):
                        raise ValueError
                self.items, self.identity = items, identity
            return self.items
        except (OSError, KeyError, ValueError, TypeError):
            self.items, self.identity = {}, None
            # A broken short registry cannot alter old film routing. Shorts
            # have unsigned STRM URLs; without registration they cannot obtain
            # a node capability or bypass the original unknown-source guard.
            return {}

    def item(self, item: str, source_id: str = "") -> dict[str, Any] | None:
        row = self.registry().get(str(item))
        if row is not None and source_id and source_id not in row["source_ids"]:
            raise HTTPException(403, "unregistered short source")
        return row

    def source(self, item: str, source: dict[str, Any]) -> dict[str, Any] | None:
        row = self.item(item, str(source.get("Id") or ""))
        if row is not None and (not source.get("Id") or str(source.get("Path") or "") not in row["paths"]):
            raise HTTPException(503, "short source identity changed")
        return row

    async def describe(self, node: Any, row: dict[str, Any], rate: int, tag: str) -> float:
        cached = self._metadata.get(row["media_path"])
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        address = sign_url(node.base_url, row["media_path"].replace("index.m3u8", "metadata.json"), node.sign_secret, 240,
                           arg_digest=node.sign_arg_digest, arg_expires=node.sign_arg_expires, rate_bps=rate, utag=tag)
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
            reply = await client.head(address)
        if reply.status_code != 200 or reply.headers.get("X-Juku-Codecs") != "h264,aac":
            raise HTTPException(503, "short metadata unavailable")
        duration = float(reply.headers.get("X-Juku-Duration", "0"))
        if not math.isfinite(duration) or not 0 < duration <= MAX_WINDOW - PAUSE_MARGIN - CLOCK_MARGIN:
            raise HTTPException(503, "short duration unavailable or outside bounded window")
        self._metadata[row["media_path"]] = (time.time(), duration)
        return duration

    async def issue_url(self, node: Any, row: dict[str, Any], item: str, source: str,
                        play: str, rate: int, tag: str, new: bool) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", play):
            raise HTTPException(403, "short play binding required")
        path = row["media_path"].replace("/index.m3u8", "/" + play + "/index.m3u8")
        now = int(time.time())
        if new:
            duration = await self.describe(node, row, rate, tag)
            expiry = int(time.time()) + math.ceil(duration) + PAUSE_MARGIN + CLOCK_MARGIN
        else:
            lease = self.admission.short_grant(play, item, source, tag) if self.admission else None
            if not lease or lease["short_path"] != path:
                raise HTTPException(403, "short play binding unavailable")
            expiry = int(lease["short_expiry"])
        if expiry <= now:
            raise HTTPException(403, "short play expired")
        # Raw Videos redirects reuse the original PI deadline, never renew it.
        return sign_url(node.base_url, path, node.sign_secret, 60,
                        now=expiry - 60, arg_digest=node.sign_arg_digest,
                        arg_expires=node.sign_arg_expires, rate_bps=rate, utag=tag)

    async def validate_session(self, router: Any, query: dict[str, str]) -> bool:
        path = query.get("p", "")
        match = BOUND_MEDIA.fullmatch(path)
        if not match or len(query) != 5:
            return False
        canonical = path.rsplit("/", 1)[0] + "/index.m3u8"
        original = f"/s/short/hongguo/{match[1]}/{match[2]}/index.m3u8"
        pair = next(((item, row) for item, row in self.registry().items() if row["media_path"] == original), None)
        if not pair or not self.admission:
            return False
        item, row = pair
        chosen = router._scheduler.pick(context=original, predicate=lambda s: s.node.name == row["node"])
        if chosen is None:
            return False
        node = chosen.node
        try:
            expiry = int(query[node.sign_arg_expires]); rate = int(query["r"]); tag = query["u"]
            if expiry > time.time() + MAX_WINDOW + CLOCK_MARGIN:
                return False
            if not verify(path, query[node.sign_arg_digest], expiry, node.sign_secret, rate_bps=rate, utag=tag):
                return False
            return await self.admission.check_short(match[3], item, canonical, expiry, tag)
        except (KeyError, ValueError, TypeError):
            return False

    @staticmethod
    def supported(source: dict[str, Any], payload: dict[str, Any] | None,
                  query: dict[str, str]) -> bool:
        def explicitly_off(value: Any) -> bool:
            return value is False or str(value).lower() in ("false", "0")
        if explicitly_off(query.get("EnableDirectStream", "")) or explicitly_off((payload or {}).get("EnableDirectStream", "")):
            return False
        profile = (payload or {}).get("DeviceProfile")
        if isinstance(profile, dict):
            for p in profile.get("DirectPlayProfiles", []) + profile.get("TranscodingProfiles", []):
                if not isinstance(p, dict) or str(p.get("Type", "Video")).lower() != "video":
                    continue
                containers = str(p.get("Container", "")).lower().split(",")
                if str(p.get("Protocol", "")).lower() != "hls" and not any(c in ("hls", "m3u8", "ts", "mpegts") for c in containers):
                    continue
                video = str(p.get("VideoCodec", "h264")).lower().split(",")
                audio = str(p.get("AudioCodec", "aac")).lower().split(",")
                if "h264" in video and "aac" in audio:
                    return True
            return False
        # Missing device evidence is unknown, not a false compatibility verdict.
        # Offer the real HLS source, with no claim that ca1 can transcode it.
        return True

    async def decorate_info(self, router: Any, item: str, data: dict[str, Any],
                            payload: dict[str, Any] | None, query: dict[str, str],
                            token: str, device: str, only_node: str = "",
                            session: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.item(item) is None:
            return data
        output = json.loads(json.dumps(data))
        sources = output.get("MediaSources")
        if not isinstance(sources, list) or not sources:
            raise HTTPException(503, "short media metadata unavailable")
        for source in sources:
            row = self.source(item, source)
            if row is None:
                raise HTTPException(503, "short media source unavailable")
            effective = payload
            if not isinstance((payload or {}).get("DeviceProfile"), dict):
                stored = (session or {}).get("DeviceProfile") or ((session or {}).get("Capabilities") or {}).get("DeviceProfile")
                if isinstance(stored, dict):
                    effective = {**(payload or {}), "DeviceProfile": stored}
                playable = (session or {}).get("PlayableMediaTypes") or []
                if playable and "Video" not in playable:
                    raise HTTPException(409, "device cannot play video")
            if not self.supported(source, effective, query):
                raise HTTPException(409, "short dramas require direct HLS H264/AAC; ca1 transcoding is not supported")
            decision = await router.route(item, f"emby/Videos/{item}/stream.m3u8",
                                          {"Static": "true", "MediaSourceId": str(source["Id"]), "PlaySessionId": str(output["PlaySessionId"]) },
                                          caller_token=token, caller_device=device, require_auth=True,
                                          only_node=only_node, short_issue=True)
            if not decision.redirected or decision.reason != "short-direct":
                raise HTTPException(503, "short media node unavailable")
            source.update({"Path": decision.target, "DirectStreamUrl": decision.target,
                           "Container": "m3u8", "Protocol": "Http", "SupportsDirectPlay": False,
                           "SupportsDirectStream": True, "SupportsTranscoding": False,
                           "RequiresOpening": False, "RequiresClosing": False,
                           "IsRemote": True, "IsInfiniteStream": False})
            source.pop("TranscodingUrl", None)
            duration = self._metadata[row["media_path"]][1]
            source["RunTimeTicks"] = int(round(duration * 10_000_000))  # noqa: RUF046 - preserve source behavior
            source["MediaStreams"] = [{"Index": 0, "Type": "Video", "Codec": "h264", "IsExternal": False},
                                      {"Index": 1, "Type": "Audio", "Codec": "aac", "IsExternal": False}]
        return output
