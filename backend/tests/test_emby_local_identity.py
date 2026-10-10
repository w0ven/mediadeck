"""Synthetic local credential authority; no production tokens or databases."""
import asyncio
import hashlib
import sqlite3
import uuid

import httpx
import pytest

from app.adapters.live import LiveEmby

GUID = "123456781234123412341234567890ab"
OTHER = "aaaaaaaa1234123412341234567890ab"
BASE = "https://emby.example.com"


@pytest.fixture
def authority(tmp_path):
    with sqlite3.connect(tmp_path / "authentication.db") as db:
        db.execute("CREATE TABLE Tokens_2 (AccessToken TEXT, UserId INTEGER, IsActive INTEGER)")
        db.execute("INSERT INTO Tokens_2 VALUES ('personal',7,1)")
    with sqlite3.connect(tmp_path / "users.db") as db:
        db.execute("CREATE TABLE LocalUsersv2 (Id INTEGER, guid TEXT)")
        db.execute("INSERT INTO LocalUsersv2 VALUES (?,?)", (7, GUID))
    return tmp_path


def adapter(root, monkeypatch, *, status=200, keys=None, bound=BASE):
    live = LiveEmby(lambda: {"enabled": True, "url": BASE, "api_key": "configured-api"},
                    identity_data_dir=str(root), identity_url=bound)
    def reply(request):
        assert request.url.path == "/emby/Auth/Keys", "local identity must not depend on global Sessions"
        return httpx.Response(status, json={"Items": keys or []})
    monkeypatch.setattr(live, "_client", lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(reply)))
    return live


@pytest.mark.parametrize("status", [200, 403])
def test_admin_and_ordinary_personal_tokens_have_exact_owner(authority, monkeypatch, status):
    # 200: admin personal token may see 155+ global Session owners. Never use
    # that list, caller DeviceId or the target UID as identity evidence.
    live = adapter(authority, monkeypatch, status=status)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in authority.iterdir()}
    assert asyncio.run(live.personal_user_for_token("personal")) == GUID
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in authority.iterdir()} == before


@pytest.mark.parametrize("token,status,keys", [
    ("personal", 401, []),
    ("personal", 200, [{"AccessToken": "personal"}]),
    ("missing", 403, []),
    ("' OR 1=1 --", 403, []),
    ("", 403, []),
])
def test_invalid_missing_and_api_credentials_refused(authority, monkeypatch, token, status, keys):
    live = adapter(authority, monkeypatch, status=status, keys=keys)
    assert asyncio.run(live.personal_user_for_token(token)) is None


@pytest.mark.parametrize("statement", [
    "UPDATE Tokens_2 SET IsActive=0",
    "UPDATE Tokens_2 SET UserId=NULL",
    "UPDATE Tokens_2 SET UserId=0",
    "UPDATE Tokens_2 SET UserId=99",
    "INSERT INTO Tokens_2 VALUES ('personal',7,0)",
    "INSERT INTO Tokens_2 VALUES ('personal',8,1)",
])
def test_revoked_null_missing_and_ambiguous_credential_refused(authority, monkeypatch, statement):
    with sqlite3.connect(authority / "authentication.db") as db:
        db.execute(statement)
    assert asyncio.run(adapter(authority, monkeypatch).personal_user_for_token("personal")) is None


@pytest.mark.parametrize("statement", [
    "UPDATE LocalUsersv2 SET guid=''",
    "UPDATE LocalUsersv2 SET guid='not-a-guid'",
    "UPDATE LocalUsersv2 SET guid='00000000000000000000000000000000'",
    "INSERT INTO LocalUsersv2 VALUES (7,'aaaaaaaa1234123412341234567890ab')",
    "INSERT INTO LocalUsersv2 VALUES (8,'12345678-1234-1234-1234-1234567890ab')",
])
def test_missing_malformed_and_ambiguous_owner_refused(authority, monkeypatch, statement):
    with sqlite3.connect(authority / "users.db") as db:
        db.execute(statement)
    assert asyncio.run(adapter(authority, monkeypatch).personal_user_for_token("personal")) is None


def test_revocation_is_not_cached(authority, monkeypatch):
    live = adapter(authority, monkeypatch)
    assert asyncio.run(live.personal_user_for_token("personal")) == GUID
    with sqlite3.connect(authority / "authentication.db") as db:
        db.execute("UPDATE Tokens_2 SET IsActive=0")
    assert asyncio.run(live.personal_user_for_token("personal")) is None


@pytest.mark.parametrize("bound", ["", "https://different.example.com"])
def test_incomplete_or_rebound_server_fails_closed(authority, monkeypatch, bound):
    with pytest.raises(RuntimeError, match="URL mismatch"):
        asyncio.run(adapter(authority, monkeypatch, bound=bound).personal_user_for_token("personal"))


def test_unavailable_authority_does_not_create_or_fall_back(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="authority unavailable"):
        asyncio.run(adapter(tmp_path, monkeypatch).personal_user_for_token("personal"))
    assert not list(tmp_path.iterdir())


def test_dashed_guid_is_normalized(authority, monkeypatch):
    with sqlite3.connect(authority / "users.db") as db:
        db.execute("UPDATE LocalUsersv2 SET guid='12345678-1234-1234-1234-1234567890ab'")
    assert asyncio.run(adapter(authority, monkeypatch).personal_user_for_token("personal")) == GUID


def test_real_emby_dotnet_guid_blob_maps_to_public_guid(authority, monkeypatch):
    with sqlite3.connect(authority / "users.db") as db:
        db.execute("UPDATE LocalUsersv2 SET guid=?", (uuid.UUID(GUID).bytes_le,))
    assert asyncio.run(adapter(authority, monkeypatch).personal_user_for_token("personal")) == GUID


def test_blob_and_text_duplicate_owner_guid_is_ambiguous(authority, monkeypatch):
    with sqlite3.connect(authority / "users.db") as db:
        db.execute("UPDATE LocalUsersv2 SET guid=?", (uuid.UUID(GUID).bytes_le,))
        db.execute("INSERT INTO LocalUsersv2 VALUES (8,?)", (GUID,))
    assert asyncio.run(adapter(authority, monkeypatch).personal_user_for_token("personal")) is None


def test_malformed_guid_blob_refused(authority, monkeypatch):
    with sqlite3.connect(authority / "users.db") as db:
        db.execute("UPDATE LocalUsersv2 SET guid=?", (b"short",))
    assert asyncio.run(adapter(authority, monkeypatch).personal_user_for_token("personal")) is None
