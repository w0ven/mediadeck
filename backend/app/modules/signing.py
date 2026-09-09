"""MediaDeck v2 signed file URLs: domain-separated HMAC-SHA256.

The existing URL path and argument names stay unchanged. The digest value is
``v2.<base64url HMAC>``. There is deliberately no v1 verification. The MAC input
is canonical UTF-8 JSON: protocol, version, decoded file path, expiry, rate and
anonymised user tag. nginx delegates verification to the existing node probe;
secure_link_md5 must never remain an alternative acceptance path.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import quote

MIN_TTL = 60
MAX_TTL = 86400 * 7
DEFAULT_ARG_DIGEST = "md5"
DEFAULT_ARG_EXPIRES = "expires"
VERSION_PREFIX = "v2."
PROTOCOL = "mediadeck.file-url"
MAX_INTEGER = (1 << 63) - 1


def generate_secret(length: int = 30) -> str:
    return secrets.token_urlsafe(length)


def user_tag(user_id: str) -> str:
    """Existing stable, anonymised account identity; not a signature."""
    if not user_id:
        return ""
    return hashlib.md5(str(user_id).encode()).hexdigest()[:10]


def _fields(decoded_path: str, expires: int, rate_bps: int | None, utag: str) -> bytes:
    if not isinstance(decoded_path, str):
        raise TypeError("path must be text")
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    if (any(ord(c) < 32 or ord(c) == 127 for c in decoded_path)
            or any(part in (".", "..") for part in decoded_path.split("/"))):
        raise ValueError("noncanonical media path")
    rate = 0 if rate_bps is None else rate_bps
    if (type(expires) is not int or not 0 <= expires <= MAX_INTEGER
            or type(rate) is not int or not 0 <= rate <= MAX_INTEGER):
        raise ValueError("invalid expiry or rate")
    if not isinstance(utag, str) or not re.fullmatch(r"[0-9a-f]{10}|", utag):
        raise ValueError("invalid user tag")
    return json.dumps([PROTOCOL, 2, decoded_path, expires, rate, utag],
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def compute_digest(decoded_path: str, expires: int, secret: str,
                   rate_bps: int | None = None, utag: str = "") -> str:
    """Return v2 HMAC. ``None`` rate is canonical zero, never a v1 request."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("signing secret required")
    message = _fields(decoded_path, expires, rate_bps, utag)
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return VERSION_PREFIX + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def validate_arg_names(arg_digest: str, arg_expires: str) -> None:
    names = (arg_digest, arg_expires, "r", "u")
    if (any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]+", name) for name in names)
            or len({name.lower() for name in names}) != len(names)):
        raise ValueError("invalid or overlapping signing argument names")


def sign_url(base_url: str, decoded_path: str, secret: str, ttl: int,
             arg_digest: str = DEFAULT_ARG_DIGEST,
             arg_expires: str = DEFAULT_ARG_EXPIRES,
             now: float | None = None,
             rate_bps: int = 0, utag: str = "") -> str:
    """Mint a v2 URL; transparent reverse proxies need no new path/header."""
    validate_arg_names(arg_digest, arg_expires)
    ttl = max(MIN_TTL, min(int(ttl), MAX_TTL))
    expires = int(time.time() if now is None else now) + ttl
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    digest = compute_digest(decoded_path, expires, secret, rate_bps=rate_bps, utag=utag)
    return (f"{base_url.rstrip('/')}{quote(decoded_path, safe='/')}"
            f"?r={rate_bps}&u={quote(utag)}"
            f"&{arg_expires}={expires}&{arg_digest}={digest}")


def public_url(base_url: str, decoded_path: str) -> str:
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    return f"{base_url.rstrip('/')}{quote(decoded_path, safe='/')}"


def verify(decoded_path: str, digest: str, expires: int, secret: str,
           now: float | None = None,
           rate_bps: int | None = None, utag: str = "") -> bool:
    """V2 only. Invalid types/fields/versions fail closed, not by exception."""
    if not isinstance(digest, str) or not re.fullmatch(r"v2\.[A-Za-z0-9_-]{43}", digest):
        return False
    try:
        expected = compute_digest(decoded_path, expires, secret, rate_bps=rate_bps, utag=utag)
        current = time.time() if now is None else now
        return current < expires and hmac.compare_digest(expected, digest)
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return False
