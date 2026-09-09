"""Independent HMAC vectors and strict nginx/probe interpretation boundaries."""
import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote, urlsplit

import pytest

from app.modules.playback import PlaybackRouter
from app.modules.provisioning import signing_config
from app.modules.signing import compute_digest, sign_url, user_tag, verify

SPEC = importlib.util.spec_from_file_location("v2_loadprobe", Path(__file__).resolve().parents[2] / "agent/loadprobe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
SECRET = "synthetic-v2-key"
NOW = 1800000000
PATH = "/s/main/测试 file %2F.mkv"
TAG = user_tag("test-viewer")


def independent_digest(path=PATH, expires=NOW + 60, rate=125000, tag=TAG):
    message = json.dumps(["mediadeck.file-url", 2, path, expires, rate, tag],
                         ensure_ascii=False, separators=(",", ":")).encode()
    return "v2." + base64.urlsafe_b64encode(hmac.digest(SECRET.encode(), message, "sha256")).decode().rstrip("=")


def raw_target(path=PATH, expires=NOW + 60, rate=125000, tag=TAG, digest=None, args=("k", "e")):
    digest = digest or independent_digest(path, expires, rate, tag)
    return f"{quote(path, safe='/')}?r={rate}&u={tag}&{args[1]}={expires}&{args[0]}={digest}"


def verifier(metering=False, args=("k", "e")):
    return probe.SigningVerifier({"version": 2, "secret": SECRET,
                                  "arg_digest": args[0], "arg_expires": args[1], "metering": metering})


@pytest.mark.parametrize("args", [("k", "e"), ("md5", "expires"), ("Key", "End")])
@pytest.mark.parametrize("rate,tag", [(0, TAG), (125000, TAG), (0, "")])
def test_independent_hmac_vector_and_wire_contract(args, rate, tag):
    expected = independent_digest(rate=rate, tag=tag)
    assert compute_digest(PATH, NOW + 60, SECRET, rate, tag) == expected
    url = sign_url("https://edge.example.com", PATH, SECRET, 60, *args, now=NOW, rate_bps=rate, utag=tag)
    assert urlsplit(url).query.endswith(f"{args[0]}={expected}")
    assert urlsplit(url).path == quote(PATH, safe="/")
    target = urlsplit(url).path + "?" + urlsplit(url).query
    assert verifier(args=args).verify(target, PATH, "GET", NOW) == {"rate_bps": rate, "utag": tag}
    assert verifier(args=args).verify(target, PATH, "HEAD", NOW)
    assert verify(PATH, expected, NOW + 60, SECRET, NOW, rate, tag)


@pytest.mark.parametrize("legacy_extra", ["", f"125000{TAG}"])
def test_independently_minted_v1_is_never_accepted(legacy_extra):
    v1 = base64.urlsafe_b64encode(hashlib.md5(f"{NOW + 60}{PATH}{legacy_extra} {SECRET}".encode()).digest()).decode().rstrip("=")
    assert not verify(PATH, v1, NOW + 60, SECRET, NOW, 125000, TAG)
    assert verifier().verify(raw_target(digest=v1), PATH, "GET", NOW) is None


@pytest.mark.parametrize("edit", [
    lambda q: q.replace("r=125000", "r=125001"),
    lambda q: q.replace("r=125000", "r=0"),
    lambda q: q.replace(f"u={TAG}", "u=1234567890"),
    lambda q: q.replace(f"u={TAG}", "u="),
    lambda q: q.replace("e=1800000060", "e=1800000061"),
    lambda q: q.replace("v2.", "v1."),
    lambda q: q + "&r=0", lambda q: q + "&R=125000",
    lambda q: q + "&e=1800000060", lambda q: q + "&u=" + TAG,
    lambda q: q + "&k=" + independent_digest(),
    lambda q: q.replace("r=125000", "r=0125000"),
    lambda q: q.replace("r=125000", "r=%31%32%35%30%30%30"),
    lambda q: q.replace("r=125000", "%72=125000"),
    lambda q: q.replace("&k=", "&K="), lambda q: q.replace("&e=", "&%65="),
    lambda q: q + "&bad", lambda q: q.replace("v2.", "v2.%"),
])
def test_signed_fields_and_duplicate_interpretations_fail_closed(edit):
    assert verifier().verify(edit(raw_target()), PATH, "GET", NOW) is None


@pytest.mark.parametrize("path", ["/s/main/other.mkv", "/s/gd3/测试 file %2F.mkv", "/s/main/../测试 file %2F.mkv"])
def test_path_is_authenticated_and_matches_nginx_served_path(path):
    assert verifier().verify(raw_target(), path, "GET", NOW) is None
    assert verifier().verify(raw_target(path=path, digest=independent_digest()), path, "GET", NOW) is None


def test_expiry_method_encoding_and_repartition_boundaries():
    gate = verifier()
    assert gate.verify(raw_target(), PATH, "GET", NOW + 60) is None
    assert gate.verify(raw_target(), PATH, "POST", NOW) is None
    assert gate.verify(raw_target().replace("%252F", "%2F"), PATH, "GET", NOW) is None
    assert gate.verify(raw_target().replace("%E6", "%ZZ"), PATH, "GET", NOW) is None
    good = compute_digest("/s/a1", NOW + 60, SECRET, 250, TAG)
    assert not verify("/s/a", good, NOW + 60, SECRET, NOW, 1250, TAG)
    assert not verify(PATH, independent_digest(), NOW + 60, SECRET, NOW, 125000, TAG[:-1])


def headers(target):
    result = Message()
    for key, value in {
        "X-Mediadeck-Target": target, "X-Mediadeck-Path": PATH.encode().decode("latin-1"),
        "X-Mediadeck-Method": "GET", "X-Mediadeck-Local-Addr": "127.0.0.1",
        "X-Mediadeck-Local-Port": "443", "X-Mediadeck-Remote-Addr": "192.0.2.1",
        "X-Mediadeck-Remote-Port": "40000", "X-Mediadeck-Connection": "1.2",
    }.items():
        result[key] = value
    return result


def test_bad_signature_never_reaches_optional_meter_and_off_does_not_call_it(monkeypatch):
    monkeypatch.setattr(probe.time, "time", lambda: NOW)
    for metering in (False, True):
        gate = verifier(metering)
        gate._opener = Mock()
        assert gate.authorize(headers(raw_target(digest="v2." + "a" * 43))) == 403
        gate._opener.open.assert_not_called()
        if not metering:
            assert gate.authorize(headers(raw_target())) == 204
            gate._opener.open.assert_not_called()
    duplicate = headers(raw_target())
    duplicate["X-Mediadeck-Path"] = "/s/other"
    assert gate.authorize(duplicate) == 403


@pytest.mark.parametrize("code,expected", [(403, 403), (500, 403), (502, 204), (503, 204), (504, 204), (302, 403)])
def test_optional_meter_status_only_applies_after_v2_validation(monkeypatch, code, expected):
    monkeypatch.setattr(probe.time, "time", lambda: NOW)
    gate = verifier(True)
    gate._opener = SimpleNamespace(open=Mock(side_effect=probe.HTTPError("http://127.0.0.1/", code, "fixture", {}, None)))
    assert gate.authorize(headers(raw_target())) == expected
    assert f"u={TAG}" in gate._opener.open.call_args.args[0].full_url


def test_private_config_defaults_do_not_enable_metering(tmp_path):
    node = SimpleNamespace(sign_secret=SECRET, sign_arg_digest="k", sign_arg_expires="e")
    cfg = json.loads(signing_config(node))
    assert cfg["metering"] is False
    assert probe.SigningVerifier.from_file(str(tmp_path / "absent")) is None
    with pytest.raises(ValueError):
        probe.SigningVerifier({**cfg, "arg_digest": "r"})


def test_unknown_caller_returns_to_origin_not_an_uncapped_signed_identity():
    node = SimpleNamespace(name="node-a", sign_secret=SECRET,
                           pools=[SimpleNamespace(name="main", emby_prefix="/media", url_prefix="/s/main")])
    router = PlaybackRouter(SimpleNamespace(verify_item_access=AsyncMock(return_value=True),
                                             item_media_paths=AsyncMock(return_value={"source": "/media/demo.mkv"})),
                            SimpleNamespace(pick=lambda **kw: SimpleNamespace(node=node)),
                            lambda: {"enabled": True}, lambda: {"url": "https://origin.example.com"},
                            rate_resolver=AsyncMock(return_value=(0, "")))
    result = asyncio.run(router.route("item", "Videos/item/original.mkv", {}, "token", True))
    assert not result.redirected and result.reason == "unattributed-caller"
