"""Export a private entry proxy config without displaying credentials or templates."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Panel redirects are not accepted")


def export(panel: str, credentials: Path, entry: str, output: Path, server: str = "caddy") -> None:
    if server not in ("caddy", "nginx"):
        raise ValueError("Unsupported proxy server")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", entry):
        raise ValueError("Invalid entry ID")
    parsed = urlsplit(panel)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c.isspace() for c in panel)):
        raise ValueError("Panel must be a literal HTTPS origin without credentials")
    # O_NOFOLLOW prevents accidentally following a replaced credential symlink.
    fd = os.open(credentials, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Credentials must be a private regular file (mode 600)")
        data = json.load(handle)
    username, password = data["username"], data["password"]
    if not isinstance(username, str) or not isinstance(password, str) or ":" in username:
        raise ValueError("Invalid credential file")
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    url = panel.rstrip("/") + "/api/integration/frontend?" + urlencode({
        "server": server, "entry": entry,
    })
    request = Request(url, headers={"Authorization": "Basic " + encoded,
                                    "Accept": "application/json"})
    # Do not forward credentials through inherited environment proxies or
    # redirects; HTTPS certificate verification uses the system trust store.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        body = json.load(response)
    config = body["config"]
    if not isinstance(config, str) or not config.strip():
        raise ValueError("Empty template response")
    # Export a candidate for review; never overwrite an installed config.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", required=True)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--server", choices=("caddy", "nginx"), default="caddy")
    args = parser.parse_args()
    try:
        export(args.panel, args.credentials, args.entry, args.output, args.server)
    except HTTPError as exc:
        print(f"Export refused: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, URLError):
        # Never print a response body, credential, request header or exception
        # containing server-controlled content.
        print("Export failed: check the HTTPS endpoint, private input and new output path", file=sys.stderr)
        return 1
    print(f"Private template saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
