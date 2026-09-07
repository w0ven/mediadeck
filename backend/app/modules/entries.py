"""Registered playback entry assertions, independent of forwarded host headers."""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.errors import ConfigError

ENTRY_HEADER = "X-Mediadeck-Entry"
KEY_HEADER = "X-Mediadeck-Entry-Key"
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z")
NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}\Z")
LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_ENTRIES = 64


def https_origin(value: Any) -> str:
    """A literal HTTPS DNS origin, safe in both Location and proxy config.

    No userinfo, paths, wildcards, query, fragment, escapes or control bytes.
    IDNs are stored in their ASCII form. Default ports are canonicalised so
    duplicate entries cannot acquire two identities.
    """
    try:
        if not isinstance(value, str) or any(c.isspace() for c in value):
            raise ValueError
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        if (parsed.scheme != "https" or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(c in value for c in "\\%?#")
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or len(host) > 253 or "." not in host
                or not all(LABEL_RE.fullmatch(part) for part in host.split("."))):
            raise ValueError
        port = parsed.port
        if port == 0 or parsed.netloc.endswith(":"):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ConfigError("入口必须是 HTTPS 域名，可带端口，但不能包含路径或认证信息") from None
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def validate_entries(raw: Any, current: list[dict[str, Any]],
                     official_url: str = "",
                     node_names: set[str] | None = None) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > MAX_ENTRIES:
        raise ConfigError(f"外部入口必须是列表，最多 {MAX_ENTRIES} 个")
    previous = {entry["id"]: entry for entry in current}
    ids: set[str] = set()
    origins: set[str] = set()
    try:
        official = https_origin(official_url)
    except ConfigError:
        official = ""
    result = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError("外部入口格式错误")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not ID_RE.fullmatch(entry_id):
            raise ConfigError("入口 ID 只允许字母、数字、下划线和连字符（1–40 字符）")
        origin = https_origin(entry.get("origin"))
        if entry_id in ids or origin in origins or origin == official:
            raise ConfigError("入口 ID、域名不能重复，也不能使用官方 Emby 入口")
        rotate = entry.get("rotate_proxy_key", False)
        if not isinstance(rotate, bool):
            raise ConfigError("rotate_proxy_key 必须是布尔值")
        # Pinned mode: a CDN that cannot route by path gets ONE stream domain
        # that proxies ONE node. Both fields must be set together; a stream
        # domain must not collide with the entry origin, the official Emby
        # origin or another entry.
        stream_origin = entry.get("stream_origin") or ""
        pinned_node = entry.get("node") or ""
        if bool(stream_origin) != bool(pinned_node):
            raise ConfigError("推流域名和固定节点必须同时填写")
        if stream_origin:
            stream_origin = https_origin(stream_origin)
            if not isinstance(pinned_node, str) or not NODE_RE.fullmatch(pinned_node):
                raise ConfigError("固定节点名无效")
            if node_names is not None and pinned_node not in node_names:
                raise ConfigError(f"固定节点 {pinned_node} 不在节点池中")
            if stream_origin in origins or stream_origin == origin or stream_origin == official:
                raise ConfigError("推流域名不能与入口域名、官方 Emby 入口或其他入口重复")
        # Keys are minted by the panel, never accepted from a submitted form.
        # A partial edit or a GET -> PUT round trip keeps the existing key.
        old_key = previous.get(entry_id, {}).get("proxy_key", "")
        row = {"id": entry_id, "origin": origin,
               "proxy_key": secrets.token_urlsafe(32) if rotate or not old_key else old_key}
        if stream_origin:
            row["stream_origin"] = stream_origin
            row["node"] = pinned_node
            origins.add(stream_origin)
        result.append(row)
        ids.add(entry_id)
        origins.add(origin)
    return result


@dataclass(frozen=True)
class PlaybackEntry:
    id: str
    origin: str
    # Pinned mode (CDN without path routing): one stream domain -> one node.
    stream_origin: str = ""
    node: str = ""

    @property
    def cache_scope(self) -> str:
        return f"entry:{self.id}:{self.origin}"

    @property
    def pinned(self) -> bool:
        return bool(self.stream_origin and self.node)


def identify_entry(headers: Any, entries: list[dict[str, Any]]) -> PlaybackEntry | None:
    """Only a registered proxy's credential can select a registered origin.

    The normal HTTP/forwarded host, source IP and Uvicorn's rewritten client
    address are deliberately irrelevant. Duplicate assertion headers fail
    closed; an invalid assertion retains the ordinary playback behaviour.
    """
    ids, keys = headers.getlist(ENTRY_HEADER), headers.getlist(KEY_HEADER)
    if len(ids) != 1 or len(keys) != 1 or not ID_RE.fullmatch(ids[0]):
        return None
    # Bound untrusted values and compare bytes (compare_digest(str) rejects
    # non-ASCII input, which must not turn a public request into an exception).
    if len(keys[0]) > 128:
        return None
    for entry in entries:
        if (entry["id"] == ids[0] and entry.get("proxy_key")
                and secrets.compare_digest(keys[0].encode(), entry["proxy_key"].encode())):
            return PlaybackEntry(id=entry["id"], origin=entry["origin"],
                                 stream_origin=entry.get("stream_origin") or "",
                                 node=entry.get("node") or "")
    return None


def entry_target(target: str, node: str, entry: PlaybackEntry) -> str:
    """Wrap a finished node URL. Never decode or rebuild its signed query.

    The node still verifies /s/... after the friend strips /_n/<node>. Signing
    the wrapper path would break every existing node's secure_link contract.

    Pinned entries have a stream domain that proxies exactly one node, so the
    path is passed through unchanged: ``https://<stream>/s/...``. If routing
    somehow picked a different node the direct target is returned rather than
    a URL the friend's CDN would 404 on.
    """
    parsed = urlsplit(target)
    if not NODE_RE.fullmatch(node) or not parsed.path.startswith("/s/"):
        return target
    if entry.pinned:
        if node != entry.node:
            return target
        stream = urlsplit(entry.stream_origin)
        return urlunsplit((stream.scheme, stream.netloc, parsed.path, parsed.query, ""))
    origin = urlsplit(entry.origin)
    return urlunsplit((origin.scheme, origin.netloc,
                       f"/_n/{node}{parsed.path}", parsed.query, ""))
