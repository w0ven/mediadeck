"""Disposable nginx processes, optionally from a host-network Docker image.

Set MEDIADECK_NGINX_DOCKER=nginx:1.27-alpine to exercise the real binary without
installing packages or touching a system service. Only test directories mount.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid

NGINX = os.environ.get("MEDIADECK_NGINX_BINARY") or shutil.which("nginx")
IMAGE = os.environ.get("MEDIADECK_NGINX_DOCKER", "")
NGINX_AVAILABLE = bool(NGINX or (IMAGE and shutil.which("docker")))


def nginx_command(directory, *args):
    if IMAGE:
        return ["docker", "run", "--rm", "--network", "host",
                "--name", f"mediadeck-test-{uuid.uuid4().hex}",
                "--volume", f"{directory}:{directory}", "--entrypoint", "nginx",
                IMAGE, *args]
    return [NGINX, *args]


def stop_proxy(process, command):
    if command[0] == "docker":
        subprocess.run(["docker", "rm", "-f", command[command.index("--name") + 1]],
                       check=True, capture_output=True, timeout=15)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
