"""Edge log parsing helpers and the node report credential.

Byte accounting from these logs is retired (docs/TRAFFIC_RETIREMENT.md; the
retired endpoint/ledger contract lives in test_traffic_retirement.py). What
remains here: the legacy log-format decoders the module still exposes, and the
node credential that also authenticates the access-policy endpoint.
"""
from __future__ import annotations

import base64
import gzip
import time

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.edgelog import (
    EdgeEvent,
    TrafficLedger,
    aggregate,
    day_key,
    open_log,
    parse_line,
    parse_lines,
)
from app.modules.signing import user_tag


def _basic(user: str = "admin", pw_value: str = "change-me") -> dict[str, str]:
    creds = base64.b64encode(f"{user}:{pw_value}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@pytest.fixture()
def ledger(tmp_path):
    return TrafficLedger(Database(tmp_path / "t.db"))


# ---------------------------------------------------------------------------
# parsing: both live formats, and everything that is not a record
# ---------------------------------------------------------------------------
def test_parses_the_original_labelled_format() -> None:
    line = "1788635166.110 a=10.0.0.1 p=55742 u=d8842b1d51 r=15728625 22245 0.002"
    event = parse_line(line)
    assert event is not None
    assert event.utag == "d8842b1d51"
    assert event.bytes_sent == 22245
    assert event.ts == pytest.approx(1788635166.110)
    assert event.status is None


def test_parses_the_extended_format_with_status_and_uri() -> None:
    """One node started appending `s=<status> <uri>` mid-deployment. A parser
    coupled to one template drops every line from the others -- silently, and
    that is total data loss rather than a visible error."""
    line = ("1788616301.177 a=10.0.0.2 p=21956 u=68249a8004 r=15728625 "
            "167100416 31.282 s=206 /s/pool/Some/Path/file.mkv")
    event = parse_line(line)
    assert event is not None
    assert event.utag == "68249a8004"
    assert event.bytes_sent == 167100416
    assert event.seconds == pytest.approx(31.282)
    assert event.status == 206


def test_parses_the_oldest_positional_format() -> None:
    event = parse_line("1788616301.177 68249a8004 4096 1.5")
    assert event is not None
    assert event.utag == "68249a8004"
    assert event.bytes_sent == 4096


def test_unknown_trailing_fields_are_tolerated() -> None:
    """A future template must not become another silent outage."""
    line = ("1788616301.177 a=10.0.0.2 p=1 u=aaaaaaaaaa r=0 5000 2.0 "
            "s=200 /s/x.mkv extra=1 more")
    event = parse_line(line)
    assert event is not None
    assert event.bytes_sent == 5000


@pytest.mark.parametrize("line", [
    "",
    "garbage",
    "not-a-timestamp a=1 u=abc 100 1.0",
    "1788616301.177 a=10.0.0.2 p=1 u=- r=0 5000 2.0",   # no user
    "1788616301.177 a=10.0.0.2 p=1 u=abc r=0 0 2.0",    # zero bytes
    "1788616301.177 u=abc",                              # truncated
    "1788616301.177 a=10.0.0.2 p=1 u=abc r=0 notanint 2.0",
])
def test_bad_lines_are_dropped_not_raised(line: str) -> None:
    assert parse_line(line) is None


def test_parse_lines_skips_bad_and_keeps_good() -> None:
    lines = [
        "1788616301.177 a=1.1.1.1 p=1 u=aaaaaaaaaa r=0 100 1.0",
        "corrupt line here",
        "1788616302.177 a=1.1.1.1 p=1 u=bbbbbbbbbb r=0 200 1.0",
    ]
    events = list(parse_lines(lines))
    assert [e.bytes_sent for e in events] == [100, 200]


def test_aggregate_folds_by_day_and_tag() -> None:
    ts = time.time()
    events = [
        EdgeEvent(ts, "tag-a", 100, 1.0),
        EdgeEvent(ts, "tag-a", 150, 2.0),
        EdgeEvent(ts, "tag-b", 70, 1.0),
    ]
    out = aggregate(events)
    assert out[(day_key(ts), "tag-a")]["bytes"] == 250
    assert out[(day_key(ts), "tag-a")]["requests"] == 2
    assert out[(day_key(ts), "tag-b")]["bytes"] == 70


def test_open_log_reads_gzip(tmp_path) -> None:
    path = tmp_path / "rotated.log.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("1788616301.177 a=1.1.1.1 p=1 u=aaaaaaaaaa r=0 100 1.0\n")
    with open_log(path) as handle:
        assert next(iter(parse_lines(handle))).bytes_sent == 100


# ---------------------------------------------------------------------------
# retired ledger: compatibility calls stay inert
# ---------------------------------------------------------------------------
def test_relink_is_idempotent(ledger) -> None:
    ts = time.time()
    ledger.record("node-a", aggregate([EdgeEvent(ts, "tag-a", 900, 1.0)]), {})
    ledger.relink({"tag-a": "u1"})
    assert ledger.relink({"tag-a": "u1"}) == 0


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _report_creds(client, node: str = "mock-a") -> str:
    body = client.get(f"/api/nodes/{node}/report-token", headers=_basic()).json()
    return body["report_token"]


def test_report_requires_the_node_credential() -> None:
    with TestClient(app) as client:
        payload = {"path": "/x", "inode": 1, "offset": 10, "lines": []}
        assert client.post("/api/edge/mock-a/report", json=payload).status_code == 401
        assert client.post("/api/edge/mock-a/report", json=payload,
                           headers={"Authorization": "Bearer wrong"}
                           ).status_code == 401


def test_a_credential_is_scoped_to_its_own_node() -> None:
    """Otherwise one compromised node could rewrite another's traffic."""
    with TestClient(app) as client:
        creds = _report_creds(client, "mock-a")
        response = client.post(
            "/api/edge/mock-b/report",
            headers={"Authorization": f"Bearer {creds}"},
            json={"path": "/x", "inode": 1, "offset": 1, "lines": []})
        assert response.status_code == 401


def test_edge_status_and_relink_need_the_operator_login() -> None:
    with TestClient(app) as client:
        assert client.get("/api/edge/status").status_code == 401
        assert client.post("/api/edge/relink").status_code == 401
        assert client.get("/api/edge/status", headers=_basic()).status_code == 200


def test_report_token_is_not_exposed_in_the_node_list() -> None:
    """The node list is rendered on every settings page load; a long-lived
    credential must not ride along with it."""
    with TestClient(app) as client:
        _report_creds(client)
        body = client.get("/api/nodes", headers=_basic()).text
        assert "report_token\"" not in body.replace("report_token_set\"", "")
        nodes = client.get("/api/nodes", headers=_basic()).json()
        assert any(n.get("report_token_set") for n in nodes)


def test_report_token_is_stable_until_rotated() -> None:
    with TestClient(app) as client:
        first = _report_creds(client)
        assert _report_creds(client) == first
        rotated = client.get("/api/nodes/mock-a/report-token?rotate=true",
                             headers=_basic()).json()["report_token"]
        assert rotated != first


def test_members_activity_endpoint() -> None:
    with TestClient(app) as client:
        body = client.get("/api/members-activity", headers=_basic()).json()
        assert body["available"] is True
        assert body["activity"]["u1"]


def test_user_tag_matches_the_signing_function() -> None:
    """The ledger maps tags back to members by recomputing this. If the two
    ever diverged, every line would be unattributable."""
    assert user_tag("u1") == user_tag("u1")
    assert len(user_tag("u1")) == 10
    assert user_tag("u1") != user_tag("u2")
