"""The template export path must keep credentials in private files/process memory."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "export-entry-proxy.py"
spec = importlib.util.spec_from_file_location("entry_export", SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / "admin.json"
    path.write_text(json.dumps({"username": "demo-admin", "password": "synthetic-password"}))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("server", ["caddy", "nginx"])
def test_export_private_file_and_no_overwrite(credentials, tmp_path, monkeypatch, capsys, server):
    requests = []
    config = "synthetic-private-template"

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps({"config": config}).encode())

    monkeypatch.setattr(exporter, "build_opener", lambda *args: Opener())
    output = tmp_path / "friend.Caddyfile"
    exporter.export("https://panel.example.com", credentials, "friend-one", output, server)
    assert requests[0].full_url == (
        f"https://panel.example.com/api/integration/frontend?server={server}&entry=friend-one")
    assert "synthetic-password" not in requests[0].full_url
    assert requests[0].get_header("Authorization").startswith("Basic ")
    assert output.read_text() == config and output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        exporter.export("https://panel.example.com", credentials, "friend-one", output)
    assert output.read_text() == config
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("panel", ["http://panel.example.com", "https://a:b@panel.example.com",
                                    "https://panel.example.com/path", "https://panel.example.com?q=1"])
def test_export_rejects_insecure_panel(panel, credentials, tmp_path):
    with pytest.raises(ValueError):
        exporter.export(panel, credentials, "friend-one", tmp_path / "out")


def test_export_rejects_public_credentials_and_redirects(credentials, tmp_path):
    credentials.chmod(0o644)
    with pytest.raises(ValueError):
        exporter.export("https://panel.example.com", credentials, "friend-one", tmp_path / "out")
    with pytest.raises(ValueError):
        exporter.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example.com")


def test_export_errors_never_display_server_body(credentials, tmp_path, monkeypatch, capsys):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 403, "synthetic-secret-in-message", {},
                            io.BytesIO(b"synthetic-secret-in-body"))

    monkeypatch.setattr(exporter, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(exporter.sys, "argv", [str(SCRIPT), "--panel", "https://panel.example.com",
                                             "--credentials", str(credentials), "--entry", "friend-one",
                                             "--output", str(tmp_path / "out")])
    assert exporter.main() == 1
    result = capsys.readouterr()
    assert result.out == "" and result.err == "Export refused: HTTP 403\n"
