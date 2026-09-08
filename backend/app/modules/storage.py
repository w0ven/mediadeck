"""Rclone remote and systemd mount management.

The panel never shells out with user-controlled strings: names are allowlisted,
targets are confined to mount_root, and every subprocess call is a argv list.
"""
from __future__ import annotations

import configparser
import os
import re
import subprocess
import tempfile
from typing import Any

from app.core.errors import ConflictError

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_MARKERS = ("token", "secret", "password", "key", "pass")
_NOT_CONFIGURED = "storage management not configured"
_MOCK_MOUNT_ROOT = "/mnt/mediadeck"


def _validate_name(value: str, label: str = "name") -> str:
    if not _NAME_RE.fullmatch(value or ""):
        raise ValueError(f"invalid {label}")
    return value


def _validate_target(target: str, mount_root: str) -> str:
    if not target or not mount_root or any(ord(ch) < 32 or ord(ch) == 127 for ch in target):
        raise ValueError("invalid target")
    root = os.path.realpath(mount_root)
    resolved = os.path.realpath(os.path.join(mount_root, target))
    if not resolved.startswith(root + os.sep):
        raise ValueError("target outside mount root")
    return resolved


def _redact_options(options: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in options.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def _unit_arg(value: str) -> str:
    """systemd is not a shell: quote words and escape its own expansions."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("invalid mount option")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def _remote_options(rtype: str, options: dict[str, Any]) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", rtype):
        raise ValueError("invalid remote type")
    for key, value in options.items():
        if not re.fullmatch(r"[A-Za-z0-9_]+", str(key)):
            raise ValueError("invalid remote option name")
        if any(ch in str(value) for ch in "\r\n\x00"):
            raise ValueError("invalid remote option value")


def _new_parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    return parser


def _remote_dict(name: str, rtype: str, options: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "type": rtype, "options": _redact_options(options)}


def _mount_dict(
    name: str,
    remote: str,
    target: str,
    status: str,
    remote_path: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "remote": remote,
        "remote_path": remote_path,
        "target": target,
        "status": status,
    }


class StorageManager:
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def _require_configured(self) -> None:
        if not self._settings.rclone_config_path or not self._settings.mount_root:
            raise ValueError(_NOT_CONFIGURED)

    def _config_path(self) -> str:
        path = self._settings.rclone_config_path
        if not path:
            raise ValueError(_NOT_CONFIGURED)
        return path

    def _unit_name(self, name: str) -> str:
        return f"{self._settings.systemd_unit_prefix}{name}.service"

    def _unit_path(self, name: str) -> str:
        return os.path.join(self._settings.systemd_unit_dir, self._unit_name(name))

    def _run(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _load_parser(self) -> configparser.ConfigParser:
        parser = _new_parser()
        path = self._settings.rclone_config_path
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    parser.read_file(handle)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, configparser.Error):
                raise RuntimeError("cannot read rclone configuration") from None
        return parser

    def _write_parser(self, parser: configparser.ConfigParser) -> None:
        path = self._config_path()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=".rclone-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                parser.write(handle)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _daemon_reload(self) -> None:
        proc = self._run(["systemctl", "daemon-reload"])
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "daemon-reload failed").strip())

    def list_remotes(self) -> list[dict[str, Any]]:
        parser = self._load_parser()
        remotes: list[dict[str, Any]] = []
        for name in parser.sections():
            options = dict(parser.items(name))
            rtype = str(options.pop("type", ""))
            remotes.append(_remote_dict(name, rtype, options))
        return remotes

    def add_remote(self, name: str, rtype: str, options: dict[str, Any] | None) -> dict[str, Any]:
        name = _validate_name(str(name or ""))
        rtype = str(rtype or "")
        if not rtype:
            raise ValueError("type required")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise ValueError("options must be an object")  # noqa: TRY004
        _remote_options(rtype, options)
        self._require_configured()
        parser = self._load_parser()
        if parser.has_section(name):
            parser.remove_section(name)
        parser.add_section(name)
        parser.set(name, "type", str(rtype))
        for key, value in options.items():
            if key == "type":
                continue
            parser.set(name, str(key), str(value))
        self._write_parser(parser)
        stored = dict(options)
        stored.pop("type", None)
        return _remote_dict(name, str(rtype), stored)

    def delete_remote(self, name: str) -> dict[str, bool]:
        _validate_name(name)
        self._require_configured()
        parser = self._load_parser()
        if not parser.has_section(name):
            raise ValueError("unknown remote")
        used = [m["name"] for m in self.list_mounts() if m.get("remote") == name]
        if used:
            raise ConflictError(
                f"远程账号仍被挂载点 {', '.join(used)} 引用，请先删除这些挂载点")
        parser.remove_section(name)
        self._write_parser(parser)
        return {"ok": True}

    def test_remote(self, name: str) -> dict[str, Any]:
        _validate_name(name)
        cfg = self._config_path()
        try:
            proc = self._run(
                [
                    self._settings.rclone_binary,
                    "lsd",
                    f"{name}:",
                    "--max-depth",
                    "1",
                    "--config",
                    cfg,
                ],
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "message": f"remote test failed: {type(exc).__name__}"}
        # rclone stderr can contain OAuth responses and signed URLs.
        return {"ok": proc.returncode == 0,
                "message": "ok" if proc.returncode == 0 else f"remote test failed (exit {proc.returncode})"}

    def list_mounts(self) -> list[dict[str, Any]]:
        unit_dir = self._settings.systemd_unit_dir
        prefix = self._settings.systemd_unit_prefix
        if not unit_dir or not os.path.isdir(unit_dir):
            return []
        mounts: list[dict[str, Any]] = []
        for filename in sorted(os.listdir(unit_dir)):
            if not filename.startswith(prefix) or not filename.endswith(".service"):
                continue
            name = filename[len(prefix) : -len(".service")]
            if not _NAME_RE.fullmatch(name):
                continue
            path = os.path.join(unit_dir, filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                continue
            remote = ""
            remote_path = ""
            target = ""
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("# mediadeck_remote="):
                    remote = stripped.split("=", 1)[1]
                elif stripped.startswith("# mediadeck_remote_path="):
                    remote_path = stripped.split("=", 1)[1]
                elif stripped.startswith("# mediadeck_target="):
                    target = stripped.split("=", 1)[1]
            proc = self._run(["systemctl", "is-active", filename])
            status = (proc.stdout or "").strip() or "unknown"
            mounts.append(_mount_dict(name, remote, target, status, remote_path))
        return mounts

    def create_mount(self, spec: dict[str, Any]) -> dict[str, Any]:
        self._require_configured()
        name = _validate_name(str(spec.get("name") or ""))
        remote = _validate_name(str(spec.get("remote") or ""), "remote")
        remote_path = str(spec.get("remote_path") or "")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in remote_path):
            raise ValueError("invalid remote_path")
        target = _validate_target(str(spec.get("target") or ""), self._settings.mount_root)
        if not self._load_parser().has_section(remote):
            raise ValueError("unknown remote")
        path = self._unit_path(name)
        if os.path.lexists(path):
            raise ConflictError("mount already exists")
        text = self._render_unit(spec, name, remote, remote_path, target)
        os.makedirs(target, exist_ok=True)
        unit_dir = self._settings.systemd_unit_dir
        os.makedirs(unit_dir, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(text)
        try:
            self._daemon_reload()
        except Exception:
            os.unlink(path)
            raise
        return _mount_dict(name, remote, target, "inactive", remote_path)

    def start_mount(self, name: str) -> dict[str, Any]:
        _validate_name(name)
        self._require_configured()
        proc = self._run(["systemctl", "start", self._unit_name(name)])
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "start failed").strip())
        return {"ok": True, "name": name, "status": "active"}

    def stop_mount(self, name: str) -> dict[str, Any]:
        _validate_name(name)
        self._require_configured()
        proc = self._run(["systemctl", "stop", self._unit_name(name)])
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "stop failed").strip())
        return {"ok": True, "name": name, "status": "inactive"}

    def delete_mount(self, name: str) -> dict[str, bool]:
        _validate_name(name)
        self._require_configured()
        unit = self._unit_name(name)
        proc = self._run(["systemctl", "stop", unit])
        if proc.returncode != 0:
            raise RuntimeError("mount stop failed; unit retained")
        path = self._unit_path(name)
        if os.path.isfile(path):
            os.remove(path)
        self._daemon_reload()
        return {"ok": True}

    def _render_unit(
        self,
        spec: dict[str, Any],
        name: str,
        remote: str,
        remote_path: str,
        target: str,
    ) -> str:
        args = [
            self._settings.rclone_binary,
            "mount",
            f"{remote}:{remote_path}",
            target,
            "--config",
            self._settings.rclone_config_path,
        ]
        if spec.get("read_only"):
            args.append("--read-only")
        if spec.get("allow_other"):
            args.append("--allow-other")
        optional = (
            ("--uid", spec.get("uid")),
            ("--gid", spec.get("gid")),
            ("--vfs-cache-mode", spec.get("vfs_cache_mode")),
            ("--vfs-cache-max-size", spec.get("vfs_cache_max_size")),
            ("--vfs-cache-max-age", spec.get("vfs_cache_max_age")),
            ("--dir-cache-time", spec.get("dir_cache_time")),
            ("--buffer-size", spec.get("buffer_size")),
        )
        for flag, value in optional:
            if value is not None and value != "":
                args.extend([flag, str(value)])
        cache_root = self._settings.cache_root
        if cache_root:
            args.extend(["--cache-dir", os.path.join(cache_root, name)])
        exec_start = " ".join(_unit_arg(part) for part in args)
        exec_stop = f"/bin/fusermount3 -uz {_unit_arg(target)}"
        return (
            f"# mediadeck_remote={remote}\n"
            f"# mediadeck_remote_path={remote_path}\n"
            f"# mediadeck_target={target}\n"
            "[Unit]\n"
            f"Description=mediadeck mount {name}\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=notify\n"
            "Restart=on-failure\n"
            f"ExecStart={exec_start}\n"
            f"ExecStop={exec_stop}\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )


class MockStorage:
    def __init__(self) -> None:
        self._remotes: dict[str, dict[str, Any]] = {
            "mock-drive": {
                "type": "drive",
                "options": {"client_id": "demo-client", "token": "redact-me"},
            },
            "mock-s3": {
                "type": "s3",
                "options": {"provider": "Other", "region": "auto"},
            },
        }
        self._mounts: dict[str, dict[str, Any]] = {
            "media-main": {
                "remote": "mock-drive",
                "remote_path": "media",
                "target": "/mnt/mediadeck/media-main",
                "active": True,
            },
            "media-cold": {
                "remote": "mock-s3",
                "remote_path": "cold",
                "target": "/mnt/mediadeck/media-cold",
                "active": False,
            },
        }

    def list_remotes(self) -> list[dict[str, Any]]:
        return [
            _remote_dict(name, item["type"], dict(item["options"]))
            for name, item in self._remotes.items()
        ]

    def add_remote(self, name: str, rtype: str, options: dict[str, Any] | None) -> dict[str, Any]:
        name = _validate_name(str(name or ""))
        rtype = str(rtype or "")
        if not rtype:
            raise ValueError("type required")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise ValueError("options must be an object")  # noqa: TRY004
        _remote_options(rtype, options)
        stored = dict(options)
        stored.pop("type", None)
        self._remotes[name] = {"type": str(rtype), "options": stored}
        return _remote_dict(name, str(rtype), stored)

    def delete_remote(self, name: str) -> dict[str, bool]:
        _validate_name(name)
        if name not in self._remotes:
            raise ValueError("unknown remote")
        used = [m["name"] for m in self.list_mounts() if m.get("remote") == name]
        if used:
            raise ConflictError(
                f"远程账号仍被挂载点 {', '.join(used)} 引用，请先删除这些挂载点")
        del self._remotes[name]
        return {"ok": True}

    def test_remote(self, name: str) -> dict[str, Any]:
        _validate_name(name)
        if name not in self._remotes:
            return {"ok": False, "message": "unknown remote"}
        return {"ok": True, "message": "ok"}

    def list_mounts(self) -> list[dict[str, Any]]:
        mounts: list[dict[str, Any]] = []
        for name, item in self._mounts.items():
            status = "active" if item["active"] else "inactive"
            mounts.append(
                _mount_dict(name, item["remote"], item["target"], status, item["remote_path"]),
            )
        return mounts

    def create_mount(self, spec: dict[str, Any]) -> dict[str, Any]:
        name = _validate_name(str(spec.get("name") or ""))
        remote = _validate_name(str(spec.get("remote") or ""), "remote")
        remote_path = str(spec.get("remote_path") or "")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in remote_path):
            raise ValueError("invalid remote_path")
        target = _validate_target(str(spec.get("target") or ""), _MOCK_MOUNT_ROOT)
        if remote not in self._remotes:
            raise ValueError("unknown remote")
        if name in self._mounts:
            raise ConflictError("mount already exists")
        for value in spec.values():
            _unit_arg(str(value))
        self._mounts[name] = {
            "remote": remote,
            "remote_path": remote_path,
            "target": target,
            "active": False,
        }
        return _mount_dict(name, remote, target, "inactive", remote_path)

    def start_mount(self, name: str) -> dict[str, Any]:
        _validate_name(name)
        item = self._mounts.get(name)
        if item is None:
            raise ValueError("unknown mount")
        item["active"] = True
        return {"ok": True, "name": name, "status": "active"}

    def stop_mount(self, name: str) -> dict[str, Any]:
        _validate_name(name)
        item = self._mounts.get(name)
        if item is None:
            raise ValueError("unknown mount")
        item["active"] = False
        return {"ok": True, "name": name, "status": "inactive"}

    def delete_mount(self, name: str) -> dict[str, bool]:
        _validate_name(name)
        if name not in self._mounts:
            raise ValueError("unknown mount")
        self._mounts[name]["active"] = False
        del self._mounts[name]
        return {"ok": True}
