"""Retired flow-meter import helpers; no collector can be instantiated."""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import subprocess
from typing import Any

TABLE = "mediadeck_meter"
SET_V4 = "media_v4"
SET_V6 = "media_v6"
FAMILY_V4 = "inet"
FAMILY_V6 = "inet6"


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 99, "", f"{type(exc).__name__}: {exc}"


def normalize_ip(value: str) -> str:
    return str(ipaddress.ip_address(value.strip()))


def classify_family(ip: str) -> str:
    parsed = ipaddress.ip_address(ip)
    return FAMILY_V6 if parsed.version == 6 else FAMILY_V4


def format_tuple(local_ip: str, local_port: int, remote_ip: str, remote_port: int) -> str:
    return f"{local_ip} . {int(local_port)} . {remote_ip} . {int(remote_port)}"


def parse_concat_counters(text: str, family: str) -> dict[str, dict[str, int]]:
    """Map ``'lip . lport . rip . rport'`` -> {packets, bytes}."""
    if family == FAMILY_V6:
        item = (
            r"([0-9a-fA-F:]+) \. (\d+) \. ([0-9a-fA-F:]+) \. (\d+) "
            r"counter packets (\d+) bytes (\d+)"
        )
    else:
        item = (
            r"(\d+\.\d+\.\d+\.\d+) \. (\d+) \. (\d+\.\d+\.\d+\.\d+) \. (\d+) "
            r"counter packets (\d+) bytes (\d+)"
        )
    out: dict[str, dict[str, int]] = {}
    for lip, lp, rip, rp, pkts, nbytes in re.findall(item, text):
        key = format_tuple(normalize_ip(lip), int(lp), normalize_ip(rip), int(rp))
        out[key] = {"packets": int(pkts), "bytes": int(nbytes)}
    return out


class NftError(RuntimeError):
    pass


class DenyPublisher:
    """Atomic write + optional nginx -t/reload of the dedicated deny map.

    Reload is off unless ``nginx_bin`` is set. Same body hash is a no-op.
    A failed -t restores the previous map. A failed reload leaves the new
    file (it is already valid) and reports not-applied so the next cycle
    retries the signal.
    """

    def __init__(self, path: str, *, nginx_bin: str | None = None,
                 nginx_conf: str | None = None, nginx_prefix: str | None = None
                 ) -> None:
        self.path = path
        self.nginx_bin = nginx_bin
        self.nginx_conf = nginx_conf
        self.nginx_prefix = nginx_prefix
        self.last_hash: str | None = None
        self.last_error: str | None = None
        self.reload_count = 0

    @staticmethod
    def render(tags: list[str] | set[str]) -> str:
        cleaned = sorted({str(t).strip() for t in tags if str(t).strip()})
        return "".join(f'"{tag}" 1;\n' for tag in cleaned) or "# none\n"

    @staticmethod
    def digest(body: str) -> str:
        return hashlib.sha256(body.encode()).hexdigest()

    @staticmethod
    def write_file(path: str, body: str) -> None:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)

    def _nginx_cmd(self, *extra: str) -> list[str]:
        cmd = [self.nginx_bin or "nginx"]
        if self.nginx_prefix:
            cmd.extend(["-p", self.nginx_prefix])
        if self.nginx_conf:
            cmd.extend(["-c", self.nginx_conf])
        cmd.extend(extra)
        return cmd

    def publish(self, tags: list[str], *, force: bool = False) -> dict[str, Any]:
        body = self.render(tags)
        digest = self.digest(body)
        if not force and self.last_hash == digest:
            return {"ok": True, "changed": False, "reloaded": False,
                    "hash": digest, "error": None, "reload_count": self.reload_count}
        previous = None
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                previous = fh.read()
        try:
            self.write_file(self.path, body)
        except OSError as exc:
            self.last_error = f"write:{exc}"
            return {"ok": False, "changed": False, "reloaded": False,
                    "hash": digest, "error": self.last_error,
                    "reload_count": self.reload_count}
        if not self.nginx_bin:
            self.last_hash = digest
            self.last_error = None
            return {"ok": True, "changed": True, "reloaded": False,
                    "hash": digest, "error": None, "reload_count": self.reload_count}
        check = subprocess.run(self._nginx_cmd("-t"), capture_output=True,
                               text=True, timeout=10, check=False)
        if check.returncode != 0:
            if previous is not None:
                self.write_file(self.path, previous)
            else:
                try:
                    os.remove(self.path)
                except OSError:
                    pass
            err = (check.stderr or check.stdout or "nginx -t failed")[-400:]
            self.last_error = f"nginx_t:{err}"
            return {"ok": False, "changed": False, "reloaded": False,
                    "hash": digest, "error": self.last_error,
                    "reload_count": self.reload_count}
        reload = subprocess.run(self._nginx_cmd("-s", "reload"),
                                capture_output=True, text=True, timeout=10,
                                check=False)
        if reload.returncode != 0:
            err = (reload.stderr or reload.stdout or "nginx reload failed")[-400:]
            self.last_error = f"nginx_reload:{err}"
            return {"ok": False, "changed": True, "reloaded": False,
                    "hash": digest, "error": self.last_error,
                    "reload_count": self.reload_count}
        self.last_hash = digest
        self.last_error = None
        self.reload_count += 1
        return {"ok": True, "changed": True, "reloaded": True,
                "hash": digest, "error": None, "reload_count": self.reload_count}


class FlowMeter:
    """Retired import shim. Cannot open a database or enable kernel counters."""
    def __init__(self, *args, **kwargs):
        raise RuntimeError('Byte-flow metering retired; use meterd.AccessPolicy for access control')
