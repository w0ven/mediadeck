"""Static contract checks for the members page module."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


def test_index_loads_members_js_after_ops() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    ops = html.find("/static/ops.js")
    members = html.find("/static/members.js")
    assert ops != -1 and members != -1
    assert ops < members


def test_members_js_uses_live_updater_contract() -> None:
    src = (STATIC / "members.js").read_text(encoding="utf-8")
    assert "PAGES.members" in src
    assert "registerLiveUpdater('members', ['members']" in src
    assert "data-live-key" in src
    assert "data-live-preserve" in src
    assert "renderView(" in src
    assert "context.isCurrent" in src
    assert "/api/stream?topics=members" in src
    assert "function go(" not in src
    assert "renderPage(" not in src


def test_app_js_untouched_by_members_page() -> None:
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "registerLiveUpdater" not in src
    assert "PAGES.members =" not in src


def test_ops_js_no_longer_owns_members_page() -> None:
    src = (STATIC / "ops.js").read_text(encoding="utf-8")
    assert "PAGES.members =" not in src
    assert "bootPanel()" not in src
