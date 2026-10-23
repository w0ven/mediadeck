"""Failed-login throttle: short, self-expiring, never touches successful logins."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.login_throttle import (
    ADDRESS,
    PAIR,
    LoginThrottle,
    Policy,
    account_key,
    client_address,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pair_blocks_after_repeated_failures_and_expires():
    clock = Clock()
    t = LoginThrottle(clock=clock)
    for _ in range(PAIR.limit - 1):
        t.failure("203.0.113.5", "demo")
    assert t.retry_after("203.0.113.5", "demo") == 0
    t.failure("203.0.113.5", "demo")
    assert t.retry_after("203.0.113.5", "demo") == PAIR.block
    # Other accounts from the address and the same account elsewhere are free.
    assert t.retry_after("203.0.113.5", "other") == 0
    assert t.retry_after("198.51.100.9", "demo") == 0
    clock.now += PAIR.block - 1
    assert t.retry_after("203.0.113.5", "demo") == 1
    clock.now += 1
    assert t.retry_after("203.0.113.5", "demo") == 0  # expired by itself


def test_failures_outside_the_window_do_not_accumulate():
    clock = Clock()
    t = LoginThrottle(clock=clock)
    for _ in range(PAIR.limit * 3):
        t.failure("203.0.113.5", "demo")
        clock.now += PAIR.window / (PAIR.limit - 1) + 1
        assert t.retry_after("203.0.113.5", "demo") == 0


def test_success_clears_pair_history():
    t = LoginThrottle(clock=Clock())
    for _ in range(PAIR.limit - 1):
        t.failure("203.0.113.5", "demo")
    t.success("203.0.113.5", "demo")
    t.failure("203.0.113.5", "demo")
    assert t.retry_after("203.0.113.5", "demo") == 0


def test_address_blocks_many_account_guessing_with_high_limit():
    clock = Clock()
    t = LoginThrottle(clock=clock)
    for i in range(ADDRESS.limit - 1):
        t.failure("203.0.113.5", f"user{i}")
    assert t.retry_after("203.0.113.5", "fresh") == 0
    t.failure("203.0.113.5", "last")
    assert t.retry_after("203.0.113.5", "fresh") == ADDRESS.block
    assert t.retry_after("198.51.100.9", "fresh") == 0
    clock.now += ADDRESS.block
    assert t.retry_after("203.0.113.5", "fresh") == 0


def test_unknown_or_proxy_address_never_throttles():
    assert client_address("127.0.0.1") == "" and client_address("::1") == ""
    assert client_address("testclient") == "" and client_address(None) == ""
    assert client_address(" 203.0.113.5 ") == "203.0.113.5"
    t = LoginThrottle(clock=Clock())
    for _ in range(100):
        t.failure("", "demo")
    assert t.retry_after("", "demo") == 0 and len(t) == 0


def test_state_is_bounded():
    t = LoginThrottle(clock=Clock(), max_keys=50)
    for i in range(500):
        t.failure(f"198.51.100.{i % 250}", f"user{i}")
    assert len(t) <= 50


def test_account_key_normalises():
    assert account_key(" Demo ") == "demo"
    assert account_key("", "U1-abc") == "u1-abc"
    assert len(account_key("x" * 1000)) == 128


@pytest.fixture()
def api(monkeypatch):
    clock = Clock()
    with TestClient(app, client=("203.0.113.5", 50000)) as c:
        app.state.login_throttle = LoginThrottle(
            pair=Policy(window=600, limit=3, block=60), address=Policy(window=600, limit=5, block=120),
            clock=clock)
        calls = []

        async def upstream(_emby, _headers, _query, payload, user_id=""):
            calls.append(payload.get("Username") or user_id)
            if payload.get("Pw") == "correct":
                return 200, {"User": {"Id": "u1"}, "SessionInfo": {"Id": "s", "UserId": "u1", "Client": "Hills"}}
            return 401, {"error": "authentication rejected"}

        monkeypatch.setattr("app.main.login_request", upstream)
        monkeypatch.setattr("app.main._restrict_session_client", AsyncMock(return_value=None))
        yield c, clock, calls


def _login(c, user, pw, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return c.post("/api/access/emby-login", json={"Username": user, "Pw": pw}, headers=headers)


def test_api_blocks_failed_pair_without_asking_emby(api):
    c, clock, calls = api
    assert [_login(c, "demo", "wrong").status_code for _ in range(3)] == [401, 401, 401]
    blocked = _login(c, "demo", "correct")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert blocked.headers["cache-control"] == "private, no-store"
    assert calls == ["demo"] * 3  # the blocked attempt never reached Emby
    # Another member behind the same address logs in normally.
    assert _login(c, "alice", "correct").status_code == 200
    clock.now += 60
    assert _login(c, "demo", "correct").status_code == 200


def test_api_successful_logins_are_never_counted(api):
    c, _clock, calls = api
    for _ in range(50):
        assert _login(c, "demo", "correct").status_code == 200
    assert len(calls) == 50


def test_api_upstream_errors_are_not_counted(api, monkeypatch):
    c, _clock, _calls = api

    async def down(*_args, **_kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr("app.main.login_request", down)
    for _ in range(10):
        assert _login(c, "demo", "x").status_code == 503
    assert app.state.login_throttle.retry_after("203.0.113.5", "demo") == 0
