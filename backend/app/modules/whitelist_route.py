"""Restricted entry policy. Only authentication and bounded HLS metadata use Deck.

Video bytes still use nginx -> the existing node/Emby. Source assertions in HLS
URLs are minted from authenticated PlaybackInfo, not from a client's source ID.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urljoin, urlsplit, urlunsplit

from fastapi import HTTPException

from app.modules.entries import PlaybackEntry, identify_entry
from app.modules.groups import WHITELIST_GROUP_ID
from app.modules.playback import caller_token, is_transcode_request, match_pool
from app.modules.signing import user_tag, validate_arg_names, verify

NO_STORE = {"Cache-Control": "private, no-store"}
MEDIA = re.compile(r"^/(?:emby/)?(?:Videos|Audio)/([^/]+)/(.+)$", re.IGNORECASE)
ISSUER = re.compile(r"^(?:stream|original)(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)
PLAYBACK = re.compile(r"^(?:stream|original|master|main|manifest|live|hls[0-9]*|universal)(?:[./].*)?$", re.IGNORECASE)
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
CAP_ARG = "md_route"
CAP_TTL = 21600


def refuse(code: int = 403, message: str = "restricted route refused") -> None:
    raise HTTPException(code, message, headers=NO_STORE)


def query_value(query: dict[str, str], name: str) -> str:
    return next((value for key, value in query.items() if key.lower() == name.lower()), "")


def target(value: str) -> tuple[str, dict[str, str]]:
    """One interpretation for authorization, routing and the nginx upstream."""
    if (not value.startswith("/") or value.startswith("//") or len(value) > 65536
            or "#" in value or any(ord(c) < 32 or ord(c) >= 127 for c in value)):
        refuse()
    u = urlsplit(value)
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", u.path):
            refuse()
        path = unquote_to_bytes(u.path).decode("utf-8", "strict")
    except (ValueError, UnicodeError):
        refuse()
    if ("\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part in (".", "..") for part in path.split("/"))):
        refuse()
    pairs = parse_qsl(u.query, keep_blank_values=True)
    if len(pairs) > 64:
        refuse()
    seen: set[str] = set()
    for raw in u.query.split("&") if u.query else []:
        name = raw.partition("=")[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.lower() in seen:
            refuse()
        seen.add(name.lower())
    if len(pairs) != len(seen):
        refuse()
    return path, dict(pairs)


def public_bootstrap(path: str, method: str) -> bool:
    if method not in ("GET", "HEAD"):
        return False
    lower = path.lower()
    if lower in ("/", "/web/", "/web/index.html", "/system/info/public", "/emby/system/info/public"):
        return True
    return bool(re.fullmatch(r"/web/[A-Za-z0-9_./-]+\.(?:js|css|woff2?|ttf|svg|png|ico|html)", path, re.IGNORECASE))


class WhitelistRoute:
    def __init__(self, state: Any, registry_path: str = "") -> None:
        self.state = state
        self.registry_path = registry_path
        self._registry_identity: tuple[int, int, int] | None = None
        self._mobile: dict[str, set[str]] = {}

    def entry(self, headers: Any, *, required: bool = False) -> PlaybackEntry | None:
        entries = self.state.settings_service.integration_config()["external_entries"]
        result = identify_entry(headers, entries)
        if required and (result is None or not result.whitelist_only):
            refuse()
        return result

    def key(self, entry: PlaybackEntry) -> str:
        rows = self.state.settings_service.integration_config()["external_entries"]
        row = next((e for e in rows if e["id"] == entry.id
                    and e["origin"] == entry.origin and e.get("whitelist_only")), None)
        if not row or not row.get("proxy_key"):
            refuse()
        return str(row["proxy_key"])

    def member(self, uid: str) -> dict[str, Any]:
        member = self.state.members.get(uid)
        if (not member or member.get("group_id") != WHITELIST_GROUP_ID
                or member.get("state", member.get("status")) != "active"):
            refuse()
        return member

    async def user(self, headers: Any, query: dict[str, str]) -> str:
        for name in ("X-Emby-Token", "X-MediaBrowser-Token", "Authorization", "X-Emby-Authorization"):
            if hasattr(headers, "getlist") and len(headers.getlist(name)) > 1:
                refuse()
        token = caller_token(headers, query)
        if not token:
            refuse(401, "personal authentication required")
        # Conflicting credential interpretations must not authorize one token
        # while Emby consumes another token from a query/header alias.
        tokens = [str(headers.get(k) or "").strip() for k in ("X-Emby-Token", "X-MediaBrowser-Token")]
        tokens += [v for k, v in query.items() if k.lower() in
                   ("api_key", "apikey", "x-emby-token", "x-mediabrowser-token")]
        auth_values = [headers.get(k) or "" for k in ("Authorization", "X-Emby-Authorization")]
        auth_values += [v for k, v in query.items() if k.lower() in
                        ("x-emby-authorization", "x-mediabrowser-authorization")]
        for value in auth_values:
            found = re.findall(r'token\s*=\s*"?([^",\s]+)"?', str(value), re.IGNORECASE)
            if len(found) > 1:
                refuse()
            tokens.extend(found)
        if any(v and v != token for v in tokens):
            refuse()
        cfg = self.state.settings_service.emby_config()
        if cfg.get("api_key") and hmac.compare_digest(token.encode(), str(cfg["api_key"]).encode()):
            refuse()
        try:
            uid = await self.state.emby.personal_user_for_token(token)
        except Exception:  # noqa: BLE001 - never expose credential-bearing upstream errors
            refuse(503, "personal identity verification unavailable")
        if not uid:
            refuse()
        self.member(uid)
        return uid

    def mobile_registry(self) -> dict[str, set[str]]:
        if not self.registry_path:
            refuse(503, "source authority unavailable")
        try:
            p = Path(self.registry_path)
            stat = p.stat()
            identity = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if identity != self._registry_identity:
                data = json.loads(p.read_text())
                if not isinstance(data, dict):
                    raise ValueError("invalid source authority")
                mobile = {str(k): {str(i) for i in v["item_ids"]} for k, v in data.items()
                          if isinstance(v, dict) and isinstance(v.get("item_ids"), list)}
                if len(mobile) != len(data):
                    raise ValueError("invalid source authority")
                self._mobile, self._registry_identity = mobile, identity
            return self._mobile
        except (OSError, ValueError, TypeError):
            self._mobile, self._registry_identity = {}, None
            refuse(503, "source authority unavailable")

    def source_kind(self, item: str, source: dict[str, Any]) -> str:
        sid = str(source.get("Id") or "")
        mobile = self.mobile_registry()
        if sid in mobile:
            if item not in mobile[sid]:
                refuse()
            return "mobile"
        path = str(source.get("Path") or "")
        if (not sid or not path.startswith("/") or path.lower().endswith(".strm")
                or any(p in (".", "..") for p in path.split("/"))):
            refuse(503, "media source unavailable")
        if any(match_pool(path, node.pools) for node in self.state.settings_service.nodes()):
            return "gd"
        refuse(503, "media source unavailable")

    def mint(self, entry: PlaybackEntry, uid: str, item: str, source: str,
             play: str, kind: str, now: int | None = None) -> str:
        fields = ["mediadeck.whitelist-play", 1, entry.id, uid, item, source, play,
                  kind, (int(time.time()) if now is None else now) + CAP_TTL]
        if not all(ID.fullmatch(v) for v in fields[2:7]) or kind not in ("gd", "mobile"):
            refuse(503, "playback source identity unavailable")
        raw = json.dumps(fields, separators=(",", ":")).encode()
        payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        mac = hmac.new(self.key(entry).encode(), b"mediadeck.whitelist-play\0" + raw, hashlib.sha256).digest()
        return payload + "." + base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def claims(self, entry: PlaybackEntry, uid: str, path: str,
               query: dict[str, str]) -> dict[str, str]:
        value = query_value(query, CAP_ARG)
        try:
            if len(value) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}", value):
                raise ValueError
            payload, signature = value.split(".")
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            if base64.urlsafe_b64encode(raw).decode().rstrip("=") != payload:
                raise ValueError
            expected = hmac.new(self.key(entry).encode(), b"mediadeck.whitelist-play\0" + raw, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, base64.urlsafe_b64encode(expected).decode().rstrip("=")):
                raise ValueError
            fields = json.loads(raw)
            if (not isinstance(fields, list) or len(fields) != 9
                    or fields[:2] != ["mediadeck.whitelist-play", 1]
                    or not all(isinstance(v, str) and ID.fullmatch(v) for v in fields[2:7])
                    or fields[2] != entry.id or fields[3] != uid
                    or fields[7] not in ("gd", "mobile") or type(fields[8]) is not int
                    or not int(time.time()) < fields[8] <= int(time.time()) + CAP_TTL + 1):
                raise ValueError
            match = MEDIA.fullmatch(path)
            if not match or match[1] != fields[4]:
                raise ValueError
            for name, expected_value in (("MediaSourceId", fields[5]), ("PlaySessionId", fields[6])):
                value = query_value(query, name)
                if value != expected_value:
                    raise ValueError
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            refuse()
        return {"uid": fields[3], "item": fields[4], "source": fields[5], "play": fields[6], "kind": fields[7]}

    def decorate(self, entry: PlaybackEntry, uid: str, item: str,
                 data: dict[str, Any]) -> dict[str, Any]:
        output = json.loads(json.dumps(data))
        official = self.state.settings_service.integration_config()["emby_public_url"].rstrip("/")
        if not official:
            refuse(503, "original entrance unavailable")
        for source in output.get("MediaSources", []):
            kind = self.source_kind(item, source)
            for field in ("DirectStreamUrl", "TranscodingUrl"):
                value = source.get(field)
                if not value:
                    continue
                parsed = urlsplit(str(value))
                path, query = target(parsed.path + ("?" + parsed.query if parsed.query else ""))
                match = MEDIA.fullmatch(path)
                if not match or match[1] != item:
                    refuse(503, "media URL unavailable")
                if kind == "mobile":
                    source[field] = urlunsplit((*urlsplit(official)[:2], parsed.path, parsed.query, ""))
                elif is_transcode_request(path, query):
                    play = str(data.get("PlaySessionId") or query_value(query, "PlaySessionId"))
                    sid = str(source.get("Id") or "")
                    if query_value(query, "MediaSourceId") != sid or query_value(query, "PlaySessionId") != play:
                        refuse(503, "unbound media URL")
                    query[CAP_ARG] = self.mint(entry, uid, item, sid, play, kind)
                    source[field] = urlunsplit((*urlsplit(entry.origin)[:2], parsed.path, urlencode(query), ""))
                elif parsed.netloc:
                    source[field] = urlunsplit((*urlsplit(entry.origin)[:2], parsed.path, parsed.query, ""))
        return output

    def rewrite_playlist(self, entry: PlaybackEntry, uid: str, original: str,
                         text: str, token: str) -> str:
        path, query = target(original)
        claims = self.claims(entry, uid, path, query)
        if claims["kind"] != "gd" or not token or len(text.encode()) > 4 * 1024 * 1024:
            refuse()
        base = entry.origin + original
        official = urlsplit(self.state.settings_service.integration_config()["emby_public_url"])
        allowed_hosts = {urlsplit(entry.origin).netloc, official.netloc}
        def rewrite(value: str) -> str:
            u = urlsplit(urljoin(base, value))
            if u.scheme != "https" or u.netloc not in allowed_hosts or u.fragment:
                refuse(503, "playlist URI unavailable")
            child_path, child_query = target(u.path + ("?" + u.query if u.query else ""))
            match = MEDIA.fullmatch(child_path)
            if not match or match[1] != claims["item"]:
                refuse(503, "playlist scope unavailable")
            for name, wanted in (("MediaSourceId", claims["source"]), ("PlaySessionId", claims["play"])):
                existing = query_value(child_query, name)
                if existing and existing != wanted:
                    refuse()
                child_query = {k: v for k, v in child_query.items() if k.lower() != name.lower()}
                child_query[name] = wanted
            child_query = {k: v for k, v in child_query.items() if k.lower() not in
                           ("api_key", "apikey", CAP_ARG)}
            child_query["api_key"] = token
            child_query[CAP_ARG] = query_value(query, CAP_ARG)
            return urlunsplit((*urlsplit(entry.origin)[:2], u.path, urlencode(child_query), ""))
        lines = []
        for line in text.splitlines():
            if line and not line.startswith("#"):
                line = rewrite(line)
            elif "URI=\"" in line:
                line = re.sub(r'URI="([^"\r\n]+)"', lambda m: 'URI="' + rewrite(m[1]) + '"', line)
            lines.append(line)
        return "\n".join(lines) + "\n"

    def file_user(self, original: str) -> str:
        """Authenticate a finished node URL before allowing this reverse proxy."""
        path, query = target(original)
        match = re.fullmatch(r"/_n/([A-Za-z0-9._-]+)/s/(.+)", path)
        if not match:
            refuse()
        node = next((n for n in self.state.settings_service.nodes() if n.name == match[1]), None)
        if not node or not node.sign_secret:
            refuse()
        _, _, raw_query = original.partition("?")
        # The wrapper prefix is not signed. Preserve the decoded /s/... and
        # original query contract exactly as the node verifier does.
        served_path = path[len("/_n/" + node.name):]
        if not any(served_path.startswith(str(p.url_prefix).rstrip("/") + "/") for p in node.pools):
            refuse()
        try:
            validate_arg_names(node.sign_arg_digest, node.sign_arg_expires)
            if len(query) > 32 or any("%" in part.partition("=")[2] for part in raw_query.split("&")):
                raise ValueError
            digest = query[node.sign_arg_digest]
            expiry, rate, tag = query[node.sign_arg_expires], query["r"], query["u"]
            if (not re.fullmatch(r"0|[1-9][0-9]{0,18}", expiry)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,18}", rate)
                    or not re.fullmatch(r"[0-9a-f]{10}", tag)
                    or not verify(served_path, digest, int(expiry), node.sign_secret,
                                  rate_bps=int(rate), utag=tag)):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            refuse()
        matches = [m for m in self.state.members.list(limit=None)
                   if user_tag(str(m.get("emby_user_id") or "")) == tag]
        if len(matches) != 1:
            refuse()
        uid = str(matches[0]["emby_user_id"])
        self.member(uid)
        return uid
