#!/usr/bin/env python3
# ruff: noqa: EXE001
# The installer sets executable mode on the downloaded agent.
"""mediadeck node load probe — single-file agent for streaming nodes.

Deploy this one file to each streaming node; it exposes a tiny /load endpoint
the scheduler polls.  Zero third-party dependencies (stdlib only).

Usage:
    python3 loadprobe.py --port 9800 [--iface eth0] [--token SECRET]
                         [--speed-log /var/log/nginx/mediadeck-speed.log]

Response:
    GET /load  ->  {"ok": true, "active_streams": N, "egress_mbps": X,
                    "user_speeds": {"<tag>": bytes_per_second, ...}}

- active_streams: ESTABLISHED TCP connections on the given service ports
  (default 443,80) — a good proxy for concurrent media streams.
- egress_mbps: TX rate of the interface, sampled every second over a sliding
  3s window.
- user_speeds: real bytes/second per anonymised user tag, taken from the
  kernel's bytes_acked on sockets that are open *right now*. nginx logs a
  request only when it ends, and in production the median request runs 27s
  with 46% over a minute, so completed lines alone leave most active viewers
  unmeasured. The access log supplies peer-address -> user tag; the rate
  itself comes from live sockets.
- Optional bearer token via --token / LOADPROBE_TOKEN.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import unquote_to_bytes, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

#: How much of the tail of the speed log to replay on startup when rebuilding
#: the peer-address -> user map. Large enough to cover a busy hour, small
#: enough to read instantly on a multi-hundred-MB log.
SEED_BYTES = 4 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "meter redirect refused", headers, fp)


class SigningVerifier:
    """Request-thread verifier; independent of socket/NIC sampling and nft.

    The wire protocol is deliberately reproduced in this stdlib-only agent;
    cross-implementation vectors test it against the panel signer. No backend
    imports, extra deployed files or external crypto packages are required.
    """

    def __init__(self, config: dict) -> None:
        if not isinstance(config, dict) or config.get("version") != 2:
            raise ValueError("v2 signing configuration required")
        self._secret = config.get("secret")
        self.digest_arg = config.get("arg_digest", "k")
        self.expires_arg = config.get("arg_expires", "e")
        names = (self.digest_arg, self.expires_arg, "r", "u")
        if (not isinstance(self._secret, str) or not self._secret
                or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_]+", n) for n in names)
                or len({n.lower() for n in names}) != 4):
            raise ValueError("invalid signing configuration")
        self.metering = config.get("metering", False)
        self.metering_port = config.get("metering_port", 9801)
        if (type(self.metering) is not bool or type(self.metering_port) is not int
                or not 1 <= self.metering_port <= 65535):
            raise ValueError("invalid metering configuration")
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_file(cls, path: str):
        try:
            with open(path, encoding="utf-8") as handle:
                return cls(json.load(handle))
        except (OSError, ValueError, TypeError):
            # The probe may still report load. Its verification endpoint must
            # fail closed until a valid private config is installed/restarted.
            return None

    @staticmethod
    def _header(headers, name: str) -> str:
        if hasattr(headers, "get_all"):
            values = headers.get_all(name) or []
            if len(values) != 1:
                raise ValueError("missing or duplicate verifier header")
            return values[0]
        return headers[name]

    def verify(self, target: str, served_path: str, method: str, now: float | None = None) -> dict | None:
        """Raw request-target plus nginx's served URI must describe one file.

        Query names/values are not URL-decoded: nginx $arg_* uses literal
        bytes. Reject percent-encoded/case aliases and duplicate keys rather
        than authenticate one interpretation and rate-limit another.
        """
        try:
            if (method not in ("GET", "HEAD") or not target.startswith("/")
                    or "#" in target or len(target) > 65536
                    or any(ord(c) < 32 or ord(c) >= 127 for c in target)):
                return None
            raw_path, separator, query = target.partition("?")
            if not separator or re.search(r"%(?![0-9a-fA-F]{2})", raw_path):
                return None
            path = unquote_to_bytes(raw_path).decode("utf-8", "strict")
            if (path != served_path or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or any(p in (".", "..") for p in path.split("/"))):
                return None
            params, seen = {}, set()
            parts = query.split("&")
            if len(parts) > 32:
                return None
            for part in parts:
                key, eq, value = part.partition("=")
                if (not eq or not re.fullmatch(r"[A-Za-z0-9_]+", key)
                        or key.lower() in seen):
                    return None
                seen.add(key.lower())
                params[key] = value
            digest, expiry, rate, utag = (params[self.digest_arg], params[self.expires_arg],
                                        params["r"], params["u"])
            if (not re.fullmatch(r"v2\.[A-Za-z0-9_-]{43}", digest)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,18}", expiry)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,18}", rate)
                    or not re.fullmatch(r"[0-9a-f]{10}|", utag)):
                return None
            expires, rate_bps = int(expiry), int(rate)
            if max(expires, rate_bps) > (1 << 63) - 1:
                return None
            message = json.dumps(["mediadeck.file-url", 2, path, expires, rate_bps, utag],
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            mac = hmac.new(self._secret.encode("utf-8"), message, hashlib.sha256).digest()
            expected = "v2." + base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")
            current = time.time() if now is None else now
            if current >= expires or not hmac.compare_digest(expected, digest):
                return None
            return {"utag": utag, "rate_bps": rate_bps}
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None

    def authorize(self, headers) -> int:
        try:
            # HTTP header decoding is Latin-1; nginx's $uri bytes are UTF-8.
            path = self._header(headers, "X-Mediadeck-Path").encode("latin-1").decode("utf-8")
            result = self.verify(self._header(headers, "X-Mediadeck-Target"), path,
                                 self._header(headers, "X-Mediadeck-Method"))
            if result is None:
                return 403
            if not self.metering:
                return 204
            params = {key: self._header(headers, name) for key, name in (
                ("lip", "X-Mediadeck-Local-Addr"), ("lp", "X-Mediadeck-Local-Port"),
                ("a", "X-Mediadeck-Remote-Addr"), ("p", "X-Mediadeck-Remote-Port"),
                ("cid", "X-Mediadeck-Connection"))}
            params["u"] = result["utag"]  # never an independently asserted tag
        except (ValueError, KeyError, UnicodeError):
            return 403
        request = Request(f"http://127.0.0.1:{self.metering_port}/register?{urlencode(params)}")
        try:
            with self._opener.open(request, timeout=0.5) as response:
                return 204 if response.status in (200, 204) else 403
        except HTTPError as exc:
            # Preserve the existing optional metering transport fail-open,
            # but ONLY after v2 authentication. Deny/mixed identity stay deny.
            return 204 if exc.code in (502, 503, 504) else 403
        except (URLError, OSError, TimeoutError):
            return 204


def default_iface() -> str:
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            for line in fh.readlines()[1:]:
                fields = line.split()
                if fields[1] == "00000000":
                    return fields[0]
    except OSError:
        pass
    return "eth0"


def tx_bytes(iface: str) -> int | None:
    try:
        with open("/proc/net/dev", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith(iface + ":"):
                    return int(line.split()[9])
    except (OSError, IndexError, ValueError):
        pass
    return None


def established_count(ports: set[int]) -> int:
    count = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh.readlines()[1:]:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "01":  # 01 = ESTABLISHED
                        continue
                    local_port = int(fields[1].rsplit(":", 1)[1], 16)
                    if local_port in ports:
                        count += 1
        except (OSError, IndexError, ValueError):
            continue
    return count


def conn_bytes(ports: set[int]) -> dict[str, tuple[str, int]] | None:
    """Established sockets -> {peer: (peer_ip, bytes_acked)} on service ports.

    ``bytes_acked`` is the kernel's count of payload the peer has actually
    acknowledged, so sampling its delta measures a transfer *while it is
    still running* -- which nginx's access log, written only at request
    completion, structurally cannot do.
    """
    out: dict[str, tuple[str, int]] = {}
    try:
        proc = subprocess.run(
            ["ss", "-tinH", "state", "established"],
            capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None

    key: str | None = None
    for line in proc.stdout.splitlines():
        if not line:
            continue
        if not line[0].isspace():
            # Address line: Recv-Q Send-Q Local:Port Peer:Port [Process]
            key = None
            fields = line.split()
            if len(fields) < 4:
                continue
            local, peer = fields[2], fields[3]
            try:
                if int(local.rsplit(":", 1)[1]) not in ports:
                    continue
            except (IndexError, ValueError):
                continue
            key = peer
            continue
        if key is None:
            continue
        # Metric line belonging to the address line above.
        marker = "bytes_acked:"
        idx = line.find(marker)
        if idx == -1:
            continue
        digits = ""
        for ch in line[idx + len(marker):]:
            if not ch.isdigit():
                break
            digits += ch
        if digits:
            out[key] = (key.rsplit(":", 1)[0], int(digits))
        key = None
    return out


class SpeedLog:
    """Per-user live rates, measured on in-flight connections.

    Two sources, because neither alone is correct:

    * **Who** a connection belongs to is only knowable from nginx's log,
      which carries the signed ``u=`` tag (and now the peer address).
    * **How fast** it is going right now is only knowable from the kernel,
      because nginx logs a request when it *ends*.

    Trying to derive live speed from completed log lines alone is what made
    the dashboard wrong: in production the median request runs 27s and 46%
    of them run over a minute (observed max: 3869s), so at any instant most
    active viewers had no completed line inside the window and were reported
    as having no measurement at all. The panel then fell back to its
    estimate for nearly every session.

    So: learn peer-IP -> user tag from the log, and take the actual rate from
    ``bytes_acked`` deltas on live sockets. Sockets are matched to a member by
    exact ``ip:port`` where the log has taught us one, because a single egress
    address routinely carries several members (household, CGNAT, office) --
    measured in production, one address appeared under four different tags
    within a few hours. Where only the address is known, it is used solely if
    unambiguous; otherwise the traffic is left unattributed rather than
    credited to the wrong member.
    """

    WINDOW = 15.0        # seconds of completed-request history retained
    OWNER_TTL = 21600.0  # how long a peer IP stays associated with a tag
    #: A viewer can hold one connection open for a very long time (observed
    #: max request duration in production: 3869s) and log nothing meanwhile,
    #: so the association has to outlive a whole watching session.
    # Must match Sampler.WINDOW. The panel shows per-user rates and node
    # egress side by side, so measuring them over different spans makes the
    # two disagree whenever throughput is changing: a 5s user window against
    # a 3s egress window reported 117%-176% of egress as "attributed" while
    # speeds were falling, which reads as a plainly wrong number.
    RATE_WINDOW = 8.0
    #: Players buffer in bursts and may CLOSE the connection between bursts,
    #: reopening it a minute later for the next range. While no socket exists
    #: the viewer's true wire speed is zero -- keep reporting that zero for a
    #: while instead of vanishing, because vanishing sends the panel back to
    #: its bitrate estimate, which is the wrong number.
    LINGER = 600.0

    def __init__(self, path: str, ports: set[int] | None = None) -> None:
        self.path = path
        self.ports = ports or {443, 80}
        self._lock = threading.Lock()
        # list of (end_ts, start_ts, utag, bytes)
        self._events: list[tuple[float, float, str, int]] = []
        # peer_ip -> {utag: last_seen_ts}
        self._owners: dict[str, dict[str, float]] = {}
        # "peer_ip:peer_port" -> (utag, last_seen_ts); exact, survives sharing
        self._sock_owners: dict[str, tuple[str, float]] = {}
        # peer socket -> (ts, bytes_acked) samples over RATE_WINDOW
        self._conns: dict[str, list[tuple[float, int]]] = {}
        # utag -> last time we saw an attributed live socket for them
        self._tag_last_live: dict[str, float] = {}
        self.sampled_at = 0.0
        self.sample_ok = False
        if path:
            self._seed_owners()
            threading.Thread(target=self._tail, daemon=True).start()
        threading.Thread(target=self._sample_conns, daemon=True).start()

    def _seed_owners(self) -> None:
        """Learn peer-IP -> user tag from recent history before tailing.

        The tail starts at end-of-file, so without this the address map is
        empty on every restart and can only be refilled by requests that
        *complete* afterwards. Playback requests run for minutes, so a viewer
        already streaming would stay unattributed for their whole session:
        measured right after a restart, 9 of 9 live sockets had a tag sitting
        in the log yet only 2 were being reported.

        Reads the tail of the log rather than the whole file: it grows
        continuously, and anything older than OWNER_TTL is ignored anyway.
        """
        try:
            size = os.path.getsize(self.path)
            with open(self.path, "rb") as fh:
                if size > SEED_BYTES:
                    fh.seek(-SEED_BYTES, os.SEEK_END)
                    fh.readline()          # drop the partial first line
                blob = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return
        cutoff = time.time() - self.OWNER_TTL
        learned = 0
        for line in blob.splitlines():
            parsed = self.parse(line)
            if parsed is None:
                continue
            end_ts, utag, _sent, _took, peer_ip, peer_port = parsed
            if not peer_ip or end_ts < cutoff:
                continue
            self._owners.setdefault(peer_ip, {})[utag] = end_ts
            if peer_port:
                self._sock_owners[f"{peer_ip}:{peer_port}"] = (utag, end_ts)
            learned += 1
        if learned:
            print(f"seeded {len(self._owners)} peer addresses / "
                  f"{len(self._sock_owners)} sockets from "
                  f"{learned} recent log lines", flush=True)

    def _sample_conns(self) -> None:
        while True:
            seen = conn_bytes(self.ports)
            now = time.time()
            if seen is None:
                with self._lock:
                    self.sample_ok = False
                time.sleep(1.0)
                continue
            with self._lock:
                self.sampled_at = now
                self.sample_ok = True
                for peer, (_ip, acked) in seen.items():
                    samples = self._conns.setdefault(peer, [])
                    if samples and (now - samples[-1][0] > 15 or acked < samples[-1][1]):
                        samples.clear()  # outage/counter reset is unknown, not zero
                    samples.append((now, acked))
                    cutoff = now - self.RATE_WINDOW
                    while len(samples) > 2 and samples[1][0] < cutoff:
                        samples.pop(0)
                # Drop sockets that have closed.
                for peer in [p for p in self._conns if p not in seen]:
                    del self._conns[peer]
            time.sleep(1.0)

    def learn(self, peer_ip: str, peer_port: str, utag: str) -> None:
        """Attribute one live socket to a member, at request *start*.

        Called from nginx via a mirror subrequest the moment a media request
        begins. This is the only timely source for long transfers: the access
        log line does not exist until the request ends, and a playback
        connection's first request can run for an hour -- measured live,
        7 of 9 streaming IPs had no log line at all, so log-taught
        attribution structurally cannot cover them.
        """
        peer_ip = (peer_ip or "").strip()
        peer_port = (peer_port or "").strip()
        utag = (utag or "").strip()
        if not peer_ip or not utag or not peer_port.isdigit():
            return
        now = time.time()
        with self._lock:
            self._owners.setdefault(peer_ip, {})[utag] = now
            self._sock_owners[f"{peer_ip}:{peer_port}"] = (utag, now)

    def _owner_of(self, peer_ip: str, now: float) -> str | None:
        """The single user tag this IP belongs to, or None if ambiguous."""
        tags = self._owners.get(peer_ip)
        if not tags:
            return None
        live = {t: ts for t, ts in tags.items() if ts >= now - self.OWNER_TTL}
        if len(live) != 1:
            return None
        return next(iter(live))

    def _tail(self) -> None:
        fh = None
        inode = None
        while True:
            try:
                if fh is None:
                    # The tail keeps its descriptor between polls and across rotation checks.
                    fh = open(self.path, encoding="utf-8", errors="replace")  # noqa: SIM115
                    inode = os.fstat(fh.fileno()).st_ino
                    fh.seek(0, os.SEEK_END)
                line = fh.readline()
                if line:
                    self._ingest(line)
                    continue
                # idle: detect logrotate (new inode or shrunk file)
                time.sleep(1)
                try:
                    st = os.stat(self.path)
                    if st.st_ino != inode or st.st_size < fh.tell():
                        fh.close()
                        fh = None
                except OSError:
                    fh.close()
                    fh = None
            except OSError:
                fh = None
                time.sleep(5)

    @staticmethod
    def parse(line: str) -> tuple[float, str, int, float, str] | None:
        """Parse a log line into (end_ts, utag, bytes, seconds, peer_ip).

        Deliberately tolerant of *which* mediadeck_speed format a node runs.
        Nodes provisioned at different times write different shapes:

            <msec> a=<ip> u=<tag> r=<rate> <bytes> <secs>  # current
            <msec> u=<tag> r=<rate> <bytes> <secs>         # no peer address
            <msec> <tag> <bytes> <secs>                    # original

        Parsing only the positional form against a labelled log made every
        line raise ValueError on int("r=10000000"), so every line was dropped
        and user_speeds stayed empty forever. Accept all of them rather than
        couple the agent to one nginx template version.
        """
        parts = line.split()
        if len(parts) < 4:
            return None
        try:
            end_ts = float(parts[0])
        except ValueError:
            return None

        utag = ""
        peer_ip = ""
        peer_port = ""
        rest: list[str] = []
        for token in parts[1:]:
            if token.startswith("u="):
                utag = token[2:]
            elif token.startswith("a="):
                peer_ip = token[2:]
            elif token.startswith("p="):
                peer_port = token[2:]
            elif token.startswith(("r=", "s=")):
                continue          # rate cap / status are not needed for speed
            elif token.startswith("/") or "=" in token:
                continue          # URI or unknown labelled field
            else:
                rest.append(token)
        if not utag:
            # Positional form: the tag is the first field after the timestamp.
            if len(rest) < 3:
                return None
            utag, rest = rest[0], rest[1:]
        if len(rest) < 2:
            return None
        try:
            sent = int(rest[0])
            took = max(0.05, float(rest[1]))
        except ValueError:
            return None
        if not utag or utag == "-" or sent <= 0:
            return None
        if peer_ip == "-":
            peer_ip = ""
        if peer_port in ("-", "0") or not peer_port.isdigit():
            peer_port = ""
        return end_ts, utag, sent, took, peer_ip, peer_port

    def _ingest(self, line: str) -> None:
        parsed = self.parse(line)
        if parsed is None:
            return
        end_ts, utag, sent, took, peer_ip, peer_port = parsed
        with self._lock:
            self._events.append((end_ts, end_ts - took, utag, sent))
            if len(self._events) > 10000:
                del self._events[:5000]
            if peer_ip:
                # Remember which member this address belongs to so in-flight
                # sockets from the same client can be attributed live.
                self._owners.setdefault(peer_ip, {})[utag] = end_ts
                if len(self._owners) > 5000:
                    stale = end_ts - self.OWNER_TTL
                    self._owners = {
                        ip: tags for ip, tags in self._owners.items()
                        if any(ts >= stale for ts in tags.values())
                    }
            if peer_ip and peer_port:
                # Exact socket -> member. A request and the streaming that
                # follows it share one connection (keep-alive / HTTP2), so
                # the port learned here identifies that same socket later.
                # This is what keeps a shared egress address (household,
                # CGNAT, office) attributable instead of ambiguous.
                self._sock_owners[f"{peer_ip}:{peer_port}"] = (utag, end_ts)
                if len(self._sock_owners) > 20000:
                    stale = end_ts - self.OWNER_TTL
                    self._sock_owners = {
                        k: v for k, v in self._sock_owners.items()
                        if v[1] >= stale
                    }

    def speeds(self) -> dict[str, int]:
        live, _completed = self.speeds_split()
        return live

    def speeds_split(self) -> tuple[dict[str, int], dict[str, int]]:
        """utag -> bytes/second, measured on connections that are live now.

        Primary source is the kernel: for every established socket, the delta
        in ``bytes_acked`` over the sampling window is what that client is
        actually receiving *at this moment*, whether or not its request has
        finished. Sockets are attributed to a member via the peer address
        learned from the access log.

        Completed log lines remain the fallback for clients whose address is
        not yet known (the very first request of a session) so a new viewer is
        not invisible until their first request ends.
        """
        now = time.time()
        cutoff = now - self.WINDOW
        out: dict[str, float] = {}
        live_tags: set[str] = set()
        unknown_tags: set[str] = set()

        with self._lock:
            # --- live sockets (authoritative) --------------------------------
            attributed_ips: set[str] = set()
            for peer, samples in self._conns.items():
                peer_ip, peer_port = peer.rsplit(":", 1)
                peer_ip = peer_ip.strip("[]")
                # Exact socket match first: it stays correct when one address
                # carries several members. Fall back to the address-level map
                # only for sockets that have not logged a request yet, and
                # that fallback still refuses ambiguous addresses.
                exact = self._sock_owners.get(f"{peer_ip}:{peer_port}")
                # Completed logs have millisecond precision; a rounded stamp
                # may be a fraction of a millisecond ahead of this clock read.
                if exact and not -0.001 <= now - exact[1] <= self.OWNER_TTL:
                    exact = None
                utag = exact[0] if exact else self._owner_of(peer_ip, now)
                if not utag:
                    continue
                if len(samples) < 2 or not 0 <= now - samples[-1][0] <= 15:
                    unknown_tags.add(utag)
                    continue
                span = samples[-1][0] - samples[0][0]
                delta = samples[-1][1] - samples[0][1]
                if not 0 < span <= self.RATE_WINDOW + 2 or delta < 0:
                    unknown_tags.add(utag)
                    continue
                # An attributed socket that moved nothing is a real
                # measurement of ZERO, and it is the normal state of a
                # media player: fill the buffer, go quiet for a minute,
                # burst again. Dropping these made the panel fall back to
                # its bitrate estimate for every buffered viewer, which is
                # exactly the wrong number the owner kept seeing.
                live_tags.add(utag)
                self._tag_last_live[utag] = now
                out[utag] = out.get(utag, 0.0) + max(0.0, delta) / span
                attributed_ips.add(peer_ip)

            # Players may CLOSE the connection between buffer bursts and
            # reopen it a minute later for the next range. While no socket
            # exists the viewer's true wire speed IS zero -- keep saying so
            # for a while instead of vanishing, because vanishing sends the
            # panel back to its bitrate estimate.
            for utag, ts in list(self._tag_last_live.items()):
                if now - ts > self.LINGER:
                    del self._tag_last_live[utag]
                elif utag not in out and utag not in unknown_tags:
                    out[utag] = 0.0
                    live_tags.add(utag)

            # Completed-request rates stay out of user_speeds. Mixing them in
            # made the panel label a finished log line as a live node sample.
            self._events = [e for e in self._events if e[0] >= cutoff]
            fallback: dict[str, float] = {}
            for end_ts, start_ts, utag, sent in self._events:
                if utag in live_tags:
                    continue
                duration = max(0.05, end_ts - start_ts)
                overlap = max(0.0, min(end_ts, now) - max(start_ts, cutoff))
                if overlap <= 0:
                    continue
                fallback[utag] = fallback.get(utag, 0.0) + \
                    (sent / duration) * (overlap / self.WINDOW)

        live = {k: int(v) for k, v in out.items()
                if k not in unknown_tags and (v >= 1 or k in live_tags)}
        completed = {k: int(v) for k, v in fallback.items() if v >= 1}
        return live, completed


class Sampler:
    """Interface counters sampled fast enough to read as "live".

    The panel polls every 15s, so the number it renders is only as fresh as
    the last sample. Sampling on a 5s cycle meant a poll could show a rate
    measured up to 5s ago, averaged over the 5s before *that*: a transfer
    running at 51 MB/s was reported as 0.6, and a finished one kept showing
    its old peak. Sample every second and report a short trailing window, so
    the value tracks the wire closely while still ignoring single-tick noise.
    """

    INTERVAL = 1.0   # seconds between counter reads
    WINDOW = 8.0     # seconds of counter history averaged into the reading

    def __init__(self, iface: str, ports: set[int], speedlog: SpeedLog) -> None:
        self.iface = iface
        self.ports = ports
        self.speedlog = speedlog
        self.egress_mbps = 0.0
        self.egress_sampled_at = 0.0
        self.egress_window_seconds = 0.0
        self.egress_ok = False
        self.active = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        # (monotonic_ts, tx_bytes) samples covering the trailing window.
        initial = tx_bytes(self.iface)
        history: list[tuple[float, int]] = [(time.monotonic(), initial)] if initial is not None else []
        counter = 0
        active = established_count(self.ports)
        while True:
            time.sleep(self.INTERVAL)
            now_ts = time.monotonic()
            current_tx = tx_bytes(self.iface)
            if current_tx is None:
                with self._lock:
                    self.egress_ok = False
                history = []
                continue
            history.append((now_ts, current_tx))
            cutoff = now_ts - self.WINDOW
            # Keep one sample older than the cutoff so the window stays full.
            while len(history) > 2 and history[1][0] < cutoff:
                history.pop(0)

            first_ts, first_tx = history[0]
            span = now_ts - first_ts
            delta = history[-1][1] - first_tx
            # Counter wrap or interface reset: restart rather than report a
            # negative or absurd spike.
            if delta < 0 or span <= 0:
                history = [(now_ts, history[-1][1])]
                mbps = 0.0
            else:
                mbps = delta * 8 / span / 1e6

            # Connection counting walks /proc/net/tcp, which is far more
            # expensive than reading one counter line; it does not need to run
            # at the full sample rate.
            counter += 1
            if counter % 5 == 0:
                active = established_count(self.ports)

            with self._lock:
                self.egress_mbps = round(max(0.0, mbps), 1)
                self.egress_ok = span > 0 and delta >= 0
                self.egress_sampled_at = time.time()
                self.egress_window_seconds = span
                self.active = active

    def snapshot(self) -> dict:
        live, completed = self.speedlog.speeds_split()
        with self._lock:
            return {"ok": True, "active_streams": self.active,
                    "egress_mbps": self.egress_mbps if self.egress_ok else None,
                    "egress_ok": self.egress_ok and time.time() - self.egress_sampled_at <= 15,
                    "egress_sampled_at": self.egress_sampled_at,
                    "egress_window_seconds": self.egress_window_seconds,
                    "user_speeds": live,
                    "user_speeds_source": "socket",
                    "user_speeds_ok": self.speedlog.sample_ok and time.time() - self.speedlog.sampled_at <= 15,
                    "user_speeds_sampled_at": self.speedlog.sampled_at,
                    "user_speeds_window_seconds": self.speedlog.RATE_WINDOW,
                    "user_speeds_fallback": completed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9800)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--iface", default=default_iface())
    parser.add_argument("--service-ports", default="443,80",
                        help="comma-separated ports counted as streams")
    parser.add_argument("--token", default=os.environ.get("LOADPROBE_TOKEN", ""))
    parser.add_argument("--signing-config", default=os.environ.get(
        "LOADPROBE_SIGNING_CONFIG", "/etc/mediadeck/signing.json"))
    parser.add_argument("--speed-log",
                        default=os.environ.get(
                            "LOADPROBE_SPEED_LOG",
                            "/var/log/nginx/mediadeck-speed.log"),
                        help="nginx mediadeck_speed log; empty disables")
    args = parser.parse_args()

    ports = {int(p) for p in args.service_ports.split(",") if p.strip()}
    sampler = Sampler(args.iface, ports, SpeedLog(args.speed_log, ports))

    speedlog = sampler.speedlog
    verifier = SigningVerifier.from_file(args.signing_config)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/verify":
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    code = 403
                elif verifier is None:
                    code = 503
                else:
                    code = verifier.authorize(self.headers)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Request-start attribution ping, mirrored by nginx on every media
            # request. Loopback-only in practice (nginx runs on the same box)
            # and carries no secrets: a hashed tag and a socket address.
            if self.path.startswith("/announce"):
                # This credential-free hook is for nginx on this host only.
                # Forwarded headers are not evidence of a local connection.
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    self.send_response(403)
                    self.end_headers()
                    return
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                speedlog.learn(q.get("a", [""])[0], q.get("p", [""])[0],
                               q.get("u", [""])[0])
                self.send_response(204)
                self.end_headers()
                return
            if self.path.rstrip("/") not in ("", "/load", "/healthz"):
                self.send_response(404)
                self.end_headers()
                return
            if args.token:
                auth = self.headers.get("Authorization", "")
                if auth != f"Bearer {args.token}":
                    self.send_response(401)
                    self.end_headers()
                    return
            body = json.dumps(sampler.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"loadprobe on {args.bind}:{args.port} iface={args.iface} ports={sorted(ports)}")
    server.serve_forever()


if __name__ == "__main__":
    main()
