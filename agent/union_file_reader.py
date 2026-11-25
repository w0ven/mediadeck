"""Loopback-only sequential first-found file reader for multi-mount media pools.

Replaces ``rclone serve http union:`` on loopback so:
1. Upstream mounts are probed strictly in priority order (first match wins
   immediately without waiting on slower secondary/tertiary cloud mounts).
2. Individual file opens use kernel path lookup (``openat2`` ``RESOLVE_BENEATH``
   on strict mounts, or canonical realpath -> relative ``openat2`` containment
   on symlinked mounts) instead of re-listing large parent directories every 5s.
3. Root directories are opened per request so FUSE unmount/remount recovery is
   immediate, and bounded concurrency prevents thread exhaustion under stalls.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import math
import mimetypes
import os
import re
import socket
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote_to_bytes, urlsplit

SYS_OPENAT2 = 437
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
CHUNK_BYTES = 65536
DEFAULT_MAX_INFLIGHT = 128
SOCKET_TIMEOUT_S = 30.0
BODY_WRITE_TIMEOUT_S = 630.0
INFLIGHT_LOG_STEP = 16
REJECT_LOG_INTERVAL_S = 10.0

_NOT_FOUND_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EISDIR})
_ESCAPE_ERRNOS = frozenset({errno.ELOOP, errno.EXDEV})


class EscapeError(PermissionError):
    """Raised when a path or symlink attempts to escape its mount root."""


class NotRegularFileError(FileNotFoundError):
    """Raised when a path resolves to a directory or special file."""


class OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def validate_relative(relative: str) -> list[str]:
    if (not relative or "\\" in relative
            or any(ord(c) < 32 or ord(c) == 127 for c in relative)):
        raise ValueError("noncanonical path")
    parts = relative.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("noncanonical path")
    return parts


def open_strict_beneath_fd(root_fd: int, relative: str) -> int:
    parts = validate_relative(relative)
    how = OpenHow(
        os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK,
        0,
        RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH,
    )
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(
        SYS_OPENAT2, root_fd, os.fsencode(relative), ctypes.byref(how), ctypes.sizeof(how)
    )
    if fd < 0:
        code = ctypes.get_errno()
        if code in _ESCAPE_ERRNOS:
            raise EscapeError(code, os.strerror(code))
        if code != errno.ENOSYS:
            raise OSError(code, os.strerror(code))
        parent = root_fd
        owned: list[int] = []
        try:
            for part in parts[:-1]:
                try:
                    parent = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=parent,
                    )
                except OSError as exc:
                    if exc.errno in _ESCAPE_ERRNOS:
                        raise EscapeError(exc.errno, exc.strerror) from None
                    raise
                owned.append(parent)
            try:
                fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=parent,
                )
            except OSError as exc:
                if exc.errno in _ESCAPE_ERRNOS:
                    raise EscapeError(exc.errno, exc.strerror) from None
                raise
        finally:
            for opened in reversed(owned):
                os.close(opened)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise NotRegularFileError(errno.EISDIR, "not a regular file")
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if flags & os.O_NONBLOCK:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_strict_beneath_path(root_path: str, relative: str) -> int:
    root_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return open_strict_beneath_fd(root_fd, relative)
    finally:
        os.close(root_fd)


def open_contained_symlinks(root_real: str, relative: str) -> int:
    parts = validate_relative(relative)
    # Fast top-level check on local ext4 directory avoids touching nested FUSE
    # submounts when top-level prefix (e.g. TV/ or _health/) is absent.
    top = os.path.join(root_real, parts[0])
    os.lstat(top)
    candidate = os.path.join(root_real, *parts)
    os.lstat(candidate)
    resolved = os.path.realpath(candidate)
    if resolved == root_real or os.path.commonpath([root_real, resolved]) != root_real:
        raise EscapeError(errno.EXDEV, "symlink escapes root")
    rel_resolved = os.path.relpath(resolved, root_real).replace("\\", "/")
    # Re-open strictly beneath root_real via openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)
    # + O_NONBLOCK so there is zero TOCTOU race and FIFOs never block.
    return open_strict_beneath_path(root_real, rel_resolved)


class UnsatisfiableRangeError(ValueError):
    """Raised when a byte Range header is syntactically invalid or out of bounds."""


def parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    stripped = header.strip()
    # RFC 9110 §14.2: ignore unknown range units (do not 416).
    if not stripped.lower().startswith("bytes="):
        return None
    spec = stripped[6:].strip()
    # Multi-range requests are unsupported; ignore and serve full 200 representation.
    if "," in spec:
        return None
    if len(stripped) > 128:
        raise UnsatisfiableRangeError("range header too long")
    match = re.fullmatch(r"(\d*)-(\d*)", spec)
    if not match or not (match[1] or match[2]) or size <= 0:
        raise UnsatisfiableRangeError("invalid byte range")
    if match[1]:
        start = int(match[1])
        end = min(int(match[2]), size - 1) if match[2] else size - 1
    else:
        suffix = int(match[2])
        if suffix <= 0:
            raise UnsatisfiableRangeError("invalid suffix range")
        start, end = max(0, size - suffix), size - 1
    if start >= size or start > end:
        raise UnsatisfiableRangeError("unsatisfiable range")
    return start, end


class MountRoot:
    __slots__ = ("follow_contained_symlinks", "path")

    def __init__(self, path: str, follow_contained_symlinks: bool = False) -> None:
        self.path = path
        self.follow_contained_symlinks = follow_contained_symlinks


class UnionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256

    def __init__(
        self,
        bind: tuple[str, int],
        specs: list[tuple[str, bool]],
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        body_write_timeout: float = BODY_WRITE_TIMEOUT_S,
    ) -> None:
        if bind[0] not in ("127.0.0.1", "::1"):
            raise ValueError("loopback only")
        if not specs:
            raise ValueError("at least one root required")
        if max_inflight < 1:
            raise ValueError("max_inflight must be >= 1")
        if not math.isfinite(body_write_timeout) or body_write_timeout <= 0 or body_write_timeout > 86400:
            raise ValueError("body_write_timeout must be finite and in (0, 86400]")
        self.address_family = socket.AF_INET6 if ":" in bind[0] else socket.AF_INET
        roots: list[MountRoot] = []
        for raw_path, follow in specs:
            if follow:
                real = os.path.realpath(raw_path)
                if not os.path.isdir(real):
                    raise ValueError(f"not a directory: {raw_path}")
                roots.append(MountRoot(path=real, follow_contained_symlinks=True))
            else:
                st = os.lstat(raw_path)
                if not stat.S_ISDIR(st.st_mode):
                    raise ValueError(f"not a strict directory: {raw_path}")
                roots.append(MountRoot(path=raw_path, follow_contained_symlinks=False))
        self.roots = roots
        self.max_inflight = max_inflight
        self.body_write_timeout = float(body_write_timeout)
        self.inflight = threading.BoundedSemaphore(max_inflight)
        self._stats_lock = threading.Lock()
        self.inflight_current = 0
        self.inflight_peak = 0
        self.rejected_503 = 0
        self._last_peak_logged = 0
        self._last_503_log_at = 0.0
        super().__init__(bind, UnionHandler)

    def _emit(self, line: str) -> None:
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass

    def _record_enter(self) -> None:
        log_line: str | None = None
        step = min(INFLIGHT_LOG_STEP, self.max_inflight)
        with self._stats_lock:
            self.inflight_current += 1
            if self.inflight_current > self.inflight_peak:
                self.inflight_peak = self.inflight_current
                if (
                    self.inflight_peak >= self._last_peak_logged + step
                    or self.inflight_peak == self.max_inflight
                ):
                    self._last_peak_logged = self.inflight_peak
                    log_line = (
                        f"[union_file_reader] inflight_peak={self.inflight_peak}/{self.max_inflight} "
                        f"current={self.inflight_current} rejected_503={self.rejected_503}"
                    )
        if log_line:
            self._emit(log_line)

    def _record_leave(self) -> None:
        with self._stats_lock:
            if self.inflight_current > 0:
                self.inflight_current -= 1

    def _record_reject_503(self) -> None:
        now = time.monotonic()
        log_line: str | None = None
        with self._stats_lock:
            self.rejected_503 += 1
            if self._last_503_log_at == 0.0 or (now - self._last_503_log_at) >= REJECT_LOG_INTERVAL_S:
                self._last_503_log_at = now
                log_line = (
                    f"[union_file_reader] inflight_full rejected_503={self.rejected_503} "
                    f"current={self.inflight_current}/{self.max_inflight} peak={self.inflight_peak}"
                )
        if log_line:
            self._emit(log_line)

    def _process_request_guarded(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._record_leave()
            self.inflight.release()

    def process_request(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        if not self.inflight.acquire(blocking=False):
            self._record_reject_503()
            try:
                request.settimeout(0.5)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: 12\r\n"
                    b"Connection: close\r\n\r\nReader busy\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        entered = False
        try:
            self._record_enter()
            entered = True
            worker = threading.Thread(
                target=self._process_request_guarded,
                args=(request, client_address),
                daemon=self.daemon_threads,
            )
            worker.start()
        except BaseException:
            if entered:
                self._record_leave()
            self.inflight.release()
            self.shutdown_request(request)
            raise


class UnionHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S
    # Unbuffered wfile so each 64 KiB write maps directly to sendall() under body_write_timeout.
    wbufsize = 0

    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.serve()

    def do_HEAD(self) -> None:
        self.serve()

    def open_first(self, relative: str) -> int:
        validate_relative(relative)
        storage_err: OSError | None = None
        for root in self.server.roots:
            try:
                if root.follow_contained_symlinks:
                    return open_contained_symlinks(root.path, relative)
                return open_strict_beneath_path(root.path, relative)
            except EscapeError:
                raise
            except NotRegularFileError:
                continue
            except OSError as exc:
                if exc.errno in _NOT_FOUND_ERRNOS:
                    continue
                if exc.errno in _ESCAPE_ERRNOS:
                    raise EscapeError(exc.errno, exc.strerror) from None
                if storage_err is None:
                    storage_err = exc
                continue
        if storage_err is not None:
            raise storage_err
        raise FileNotFoundError(relative)

    def serve(self) -> None:
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.send_error(403)
            self.close_connection = True
            return
        req_parts = self.raw_requestline.decode("latin-1", "replace").rstrip("\r\n").split()
        if len(req_parts) not in (2, 3):
            self.send_error(400)
            self.close_connection = True
            return
        raw_target = req_parts[1]
        if not raw_target.startswith("/") or raw_target.startswith("//"):
            self.send_error(400)
            self.close_connection = True
            return
        try:
            raw = urlsplit(raw_target).path
            if re.search(r"%(?![0-9a-fA-F]{2})", raw) or not raw.startswith("/"):
                self.send_error(400)
                self.close_connection = True
                return
            decoded = unquote_to_bytes(raw[1:]).decode("utf-8", "strict")
            validate_relative(decoded)
        except (UnicodeError, ValueError):
            self.send_error(400)
            self.close_connection = True
            return

        try:
            fd = self.open_first(decoded)
        except EscapeError:
            self.send_error(404)
            self.close_connection = True
            return
        except FileNotFoundError:
            self.send_error(404)
            self.close_connection = True
            return
        except ValueError:
            self.send_error(400)
            self.close_connection = True
            return
        except OSError:
            self.send_error(503, "Storage mount unavailable")
            self.close_connection = True
            return

        try:
            size = os.fstat(fd).st_size
            ignore_range = self.command == "HEAD" or bool(self.headers.get("If-Range"))
            range_hdr = "" if ignore_range else self.headers.get("Range", "")
            try:
                selected = parse_byte_range(range_hdr, size)
            except UnsatisfiableRangeError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return

            start, end = selected if selected else (0, size - 1)
            amount = (end - start + 1) if size > 0 else 0
            self.send_response(206 if selected else 200)
            ctype = mimetypes.guess_type(decoded)[0] or "application/octet-stream"
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(amount))
            if selected:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD" or amount == 0:
                return

            offset = start
            remaining = amount
            # Separate streaming body write timeout from header read timeout so paused
            # players under nginx proxy_buffering=off are not cut at 30s; always restore
            # the header timeout on exit so reused sockets can never stall idly for 630s.
            self.connection.settimeout(self.server.body_write_timeout)
            try:
                while remaining > 0:
                    data = os.pread(fd, min(CHUNK_BYTES, remaining), offset)
                    if not data:
                        break
                    self.wfile.write(data)
                    n = len(data)
                    offset += n
                    remaining -= n
            finally:
                try:
                    self.connection.settimeout(self.timeout)
                except OSError:
                    pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            os.close(fd)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9812)
    parser.add_argument("--max-inflight", type=int, default=DEFAULT_MAX_INFLIGHT)
    parser.add_argument(
        "--body-write-timeout",
        type=float,
        default=BODY_WRITE_TIMEOUT_S,
        help="Per-write socket timeout in seconds while streaming response body",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Read-only root directory searched in order (no symlinks followed)",
    )
    parser.add_argument(
        "--symlink-root",
        action="append",
        default=[],
        help="Read-only root searched after --root, allowing symlinks that resolve inside itself",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    specs = [(p, False) for p in args.root] + [(p, True) for p in args.symlink_root]
    server = UnionServer(
        (args.host, args.port),
        specs,
        max_inflight=args.max_inflight,
        body_write_timeout=args.body_write_timeout,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
