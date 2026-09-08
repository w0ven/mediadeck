"""Membership, groups, roles, enforcement, statistics and image cache.

These cover the parts where a mistake costs money or locks people out, so the
assertions are about *behaviour under failure*, not just happy paths.
"""
from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.mock import MockEmby
from app.core.db import Database
from app.core.errors import ConfigError
from app.main import app
from app.modules.enforcement import EnforcementService, desired_policy
from app.modules.groups import GroupService
from app.modules.imagecache import ImageCache
from app.modules.members import (
    MemberService,
    merge_effective,
    parse_roles,
    period_start,
    rate_bytes_per_sec,
    validate_overrides,
)
from app.modules.stats import StatsService
from app.modules.usage import UsageSampler, is_playing, session_bitrate

GIB = 1024 ** 3


def _basic(user: str = "admin", password: str = "change-me") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def stack():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "t.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    emby = MockEmby()
    enforcement = EnforcementService(db, members, emby)
    return {"db": db, "groups": groups, "members": members,
            "emby": emby, "enforcement": enforcement, "tmp": tmp}


# ---- groups ----------------------------------------------------------------
def test_seeded_groups_match_owner_model(stack) -> None:
    """standard = time+traffic, vip = traffic only, standard is the default."""
    groups = {g["id"]: g for g in stack["groups"].list()}
    assert groups["standard"]["billing_mode"] == "both"
    assert groups["standard"]["is_default"] == 1
    assert groups["standard"]["duration_days"] == 30
    assert groups["standard"]["traffic_quota_bytes"] == 1024 * GIB
    assert groups["vip"]["billing_mode"] == "traffic"
    assert groups["vip"]["duration_days"] == 0


def test_metered_group_must_have_a_meter(stack) -> None:
    """A traffic group with no quota silently grants unlimited access."""
    with pytest.raises(ConfigError):
        stack["groups"].create({"id": "bad", "name": "x",
                                "billing_mode": "traffic",
                                "traffic_quota_bytes": 0})
    with pytest.raises(ConfigError):
        stack["groups"].create({"id": "bad2", "name": "x",
                                "billing_mode": "time", "duration_days": 0})


def test_group_in_use_cannot_be_deleted(stack) -> None:
    """Deleting a group under live members leaves limits nobody can explain."""
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    with pytest.raises(ConfigError):
        stack["groups"].delete("standard")
    stack["members"].delete("u1")
    assert stack["groups"].delete("standard")


def test_only_one_default_group(stack) -> None:
    stack["groups"].create({"id": "g2", "name": "G2", "billing_mode": "none",
                            "is_default": True})
    defaults = [g for g in stack["groups"].list() if g["is_default"]]
    assert len(defaults) == 1 and defaults[0]["id"] == "g2"


def test_billing_mode_none_never_expires_or_exhausts(stack) -> None:
    stack["groups"].create({"id": "free", "name": "Free", "billing_mode": "none"})
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "free",
                             "traffic_used_bytes": 9999 * GIB})
    assert m.get("u1")["state"] == "active"


# ---- roles -----------------------------------------------------------------
def test_roles_are_whitelisted_and_deduplicated() -> None:
    assert parse_roles("admin,uploader,admin") == ["admin", "uploader"]
    assert parse_roles(["ADMIN", "bogus", "uploader"]) == ["admin", "uploader"]
    assert parse_roles("") == []


def test_set_roles_audits_the_change(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard"})
    m.set_roles("u1", ["admin"])
    assert m.get("u1")["roles"] == ["admin"]
    rows = stack["db"].query(
        "SELECT * FROM audit_log WHERE action='member.roles'")
    assert len(rows) == 1


def test_role_filter_in_list(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard"})
    m.upsert("u2", "bob", {"group_id": "standard"})
    m.set_roles("u1", ["uploader"])
    uploaders = m.list(role="uploader")
    assert [x["emby_user_id"] for x in uploaders] == ["u1"]


# ---- member state ----------------------------------------------------------
def test_state_is_derived_not_stored(stack) -> None:
    """Expiry and quota are time-dependent; a stale label would mis-bill."""
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard"})
    assert m.get("u1")["state"] == "active"

    m.upsert("u1", "alice", {"expires_at": int(time.time()) - 10})
    assert m.get("u1")["state"] == "expired"

    m.upsert("u1", "alice", {"expires_at": int(time.time()) + 86400,
                             "traffic_used_bytes": 1024 * GIB})
    assert m.get("u1")["state"] == "exhausted"


def test_vip_never_expires_but_still_exhausts(stack) -> None:
    """The whole point of the vip group: no clock, but the meter still runs."""
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "vip"})
    assert m.get("u1")["expires_at"] is None
    m.upsert("u1", "alice", {"traffic_used_bytes": 2048 * GIB})
    assert m.get("u1")["state"] == "exhausted"


def test_manual_states_survive_automatic_recalculation(stack) -> None:
    """A suspended account must not come back when its quota resets."""
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard"})
    m.set_status("u1", "suspended")
    m.reset_traffic("u1")
    assert m.get("u1")["state"] == "suspended"


def test_renew_extends_from_expiry_not_from_now(stack) -> None:
    """Renewing early must not throw away days already granted."""
    m = stack["members"]
    future = int(time.time()) + 20 * 86400
    m.upsert("u1", "alice", {"group_id": "standard", "expires_at": future})
    m.renew("u1", days=30)
    assert m.get("u1")["expires_at"] >= future + 30 * 86400 - 5


def test_assigning_a_timed_group_sets_an_expiry(stack) -> None:
    """New time billing arms the clock; traffic-only groups have no expiry."""
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    first = stack["members"].get("u1")["expires_at"]
    assert first is not None
    stack["members"].upsert("u1", "alice", {"group_id": "vip"})
    assert stack["members"].get("u1")["expires_at"] is None
    stack["members"].upsert(
        "u1", "alice", {"group_id": "standard", "expiry_policy": "apply_group"})
    assert stack["members"].get("u1")["expires_at"] is not None


def test_monthly_rollover_resets_quota_and_revives_exhausted(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard",
                             "traffic_used_bytes": 1024 * GIB})
    assert m.get("u1")["state"] == "exhausted"
    # Pretend the window closed long ago.
    stack["db"].execute(
        "UPDATE members SET traffic_period_start=? WHERE emby_user_id=?",
        (1, "u1"))
    assert m.roll_periods() == 1
    after = m.get("u1")
    assert after["traffic_used_bytes"] == 0 and after["state"] == "active"
    assert after["traffic_period_start"] == period_start(int(time.time()))


def test_rollover_drops_one_off_extra_traffic(stack) -> None:
    """The monthly top-up is a this-month gift, not a permanent raise."""
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard"})
    m.add_extra_traffic("u1", 10 * GIB)
    assert m.get("u1")["traffic_quota_bytes"] == 1034 * GIB
    stack["db"].execute(
        "UPDATE members SET traffic_period_start=? WHERE emby_user_id=?",
        (1, "u1"))
    m.roll_periods()
    assert m.get("u1")["traffic_quota_bytes"] == 1024 * GIB


def test_enroll_defaults_only_touches_unmanaged(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "vip"})
    users = [{"Id": "u1", "Name": "alice"}, {"Id": "u2", "Name": "bob"}]
    assert m.enroll_defaults(users) == 1
    assert m.get("u1")["group_id"] == "vip"          # untouched
    assert m.get("u2")["group_id"] == "standard"     # default group
    assert m.get("u2")["expires_at"] is not None     # 30-day clock armed


# ---- overrides -------------------------------------------------------------
def test_override_keys_are_whitelisted() -> None:
    with pytest.raises(ConfigError):
        validate_overrides({"max_bitrate_kbps": 5})  # the old bitrate key died
    with pytest.raises(ConfigError):
        validate_overrides({"nonsense": 1})
    assert validate_overrides({}) == {}


def test_merge_prefers_overrides_field_by_field(stack) -> None:
    group = stack["groups"].get("standard")
    eff = merge_effective(group, {"bandwidth_limit_kbps": 20000}, {})
    assert eff["bandwidth_limit_kbps"] == 20000
    assert eff["max_streams"] == group["max_streams"]  # inherited
    assert eff["overridden_keys"] == ["bandwidth_limit_kbps"]


def test_expiry_override_beats_stored_expiry_in_timed_group(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard"})
    past = int(time.time()) - 10
    m.set_overrides("u1", {"expires_at_override": past})
    assert m.get("u1")["state"] == "expired"
    m.set_overrides("u1", {})
    assert m.get("u1")["state"] == "active"


def test_extra_traffic_extends_the_quota(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard",
                             "traffic_used_bytes": 1024 * GIB})
    assert m.get("u1")["state"] == "exhausted"
    m.add_extra_traffic("u1", 50 * GIB)
    assert m.get("u1")["state"] == "active"


# ---- enforcement -----------------------------------------------------------
def test_policy_maps_limits_onto_emby_fields(stack) -> None:
    stack["groups"].create({
        "id": "capped", "name": "Capped", "billing_mode": "both",
        "duration_days": 7, "traffic_quota_bytes": 50 * GIB,
        "bandwidth_limit_kbps": 8000, "max_streams": 1, "max_devices": 1,
        "allow_transcode": 0, "allow_download": 0,
    })
    stack["members"].upsert("u1", "alice", {"group_id": "capped"})
    policy = desired_policy(stack["members"].get("u1"))
    assert policy["SimultaneousStreamLimit"] == 1
    # kbps -> bps: this is Emby's *bandwidth* cap, not a media bitrate.
    assert policy["RemoteClientBitrateLimit"] == 8000 * 1000
    assert policy["EnableVideoPlaybackTranscoding"] is False
    assert policy["EnableContentDownloading"] is False
    assert policy["IsDisabled"] is False


def test_policy_prefers_member_overrides(stack) -> None:
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["members"].set_overrides("u1", {"bandwidth_limit_kbps": 20000,
                                          "max_streams": 5})
    policy = desired_policy(stack["members"].get("u1"))
    assert policy["RemoteClientBitrateLimit"] == 20000 * 1000
    assert policy["SimultaneousStreamLimit"] == 5


def test_rate_bytes_per_sec_is_what_nginx_limit_rate_reads() -> None:
    assert rate_bytes_per_sec(0) == 0
    assert rate_bytes_per_sec(8000) == 1_000_000
    kbps = round(15 * 1048576 / 125)
    assert rate_bytes_per_sec(kbps) == kbps * 125


def test_terminate_users_stops_only_matching_sessions(stack) -> None:
    stack["emby"].set_sessions([
        {"Id": "s1", "UserId": "u1"},
        {"Id": "s2", "UserId": "u2"},
    ])
    stopped = asyncio.run(
        stack["enforcement"].terminate_users({"u1"}, "限速已更新"))
    assert stopped == 1
    assert stack["emby"].stopped == [("s1", "限速已更新")]
    assert [s["Id"] for s in stack["emby"]._sessions] == ["s2"]


def test_zero_means_unlimited_in_both_fields(stack) -> None:
    stack["members"].upsert("u1", "alice", {"group_id": "vip"})
    stack["members"].set_overrides("u1", {"bandwidth_limit_kbps": 0,
                                          "max_streams": 0})
    policy = desired_policy(stack["members"].get("u1"))
    assert policy["RemoteClientBitrateLimit"] == 0
    assert policy["SimultaneousStreamLimit"] == 0


def test_blocking_states_disable_the_account(stack) -> None:
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["members"].set_status("u1", "suspended")
    assert desired_policy(stack["members"].get("u1"))["IsDisabled"] is True


def test_ungrouped_member_only_gets_the_block_flag(stack) -> None:
    stack["members"].upsert("u1", "alice", {})
    policy = desired_policy(stack["members"].get("u1"))
    assert set(policy) == {"IsDisabled"}


def test_enforcement_never_touches_unenrolled_accounts(stack) -> None:
    """The whole safety story: hundreds of pre-existing accounts stay untouched."""
    stack["members"].upsert("u1", "demo-user-1", {"group_id": "standard"})
    result = asyncio.run(stack["enforcement"].reconcile(apply=True))
    touched = {c["user_id"] for c in result["changes"]}
    assert touched <= {"u1"}
    # u2 exists in Emby but was never enrolled.
    assert stack["emby"]._users["u2"]["Policy"] == {"IsDisabled": True}


def test_administrators_are_never_disabled(stack) -> None:
    """Locking the operator out of their own server is not an acceptable bug."""
    stack["members"].upsert("admin", "demo-admin", {"group_id": "standard"})
    stack["members"].set_status("admin", "suspended")
    result = asyncio.run(stack["enforcement"].reconcile(apply=True))
    assert any(s["reason"] == "administrator" for s in result["skipped"])


def test_reconcile_is_idempotent(stack) -> None:
    stack["members"].upsert("u1", "demo-user-1", {"group_id": "standard"})
    first = asyncio.run(stack["enforcement"].reconcile(apply=True))
    second = asyncio.run(stack["enforcement"].reconcile(apply=True))
    assert first["applied"] >= 1 and second["applied"] == 0


def test_dry_run_writes_nothing(stack) -> None:
    stack["members"].upsert("u1", "demo-user-1", {"group_id": "standard"})
    result = asyncio.run(stack["enforcement"].reconcile(apply=False))
    assert result["planned"] >= 1 and result["applied"] == 0
    assert "SimultaneousStreamLimit" not in stack["emby"]._users["u1"]["Policy"]


def test_terminate_sessions_stops_only_that_user(stack) -> None:
    stack["emby"].set_sessions([
        {"Id": "s1", "UserId": "u1"}, {"Id": "s2", "UserId": "u2"}])
    stopped = asyncio.run(stack["enforcement"].terminate_sessions("u1", "quota"))
    assert stopped == 1
    assert [s["Id"] for s in stack["emby"]._sessions] == ["s2"]


# ---- usage accounting ------------------------------------------------------
def _session(sid: str, user: str, bitrate: int, paused: bool = False,
             item: str = "i1") -> dict:
    return {
        "Id": sid, "UserId": user, "UserName": user, "DeviceId": f"dev-{user}",
        "Client": "TestClient", "RemoteEndPoint": "10.0.0.1",
        "NowPlayingItem": {"Id": item, "Name": f"Title {item}",
                           "Type": "Movie", "Bitrate": bitrate},
        "PlayState": {"IsPaused": paused, "PlayMethod": "DirectStream"},
    }


def test_paused_sessions_are_not_billed(stack) -> None:
    """Charging for time a user did not watch is the worst kind of bug."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000, paused=True)])
    asyncio.run(sampler.tick())
    time.sleep(0.05)
    asyncio.run(sampler.tick())
    assert stack["members"].get("u1")["traffic_used_bytes"] == 0


def test_first_sighting_bills_nothing(stack) -> None:
    """Billing a full interval on first sight charges for playback that just began."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    result = asyncio.run(sampler.tick())
    assert result["billed_bytes"] == 0


def test_traffic_accrues_while_playing(stack) -> None:
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    time.sleep(0.2)
    result = asyncio.run(sampler.tick())
    assert result["billed_bytes"] > 0
    assert stack["members"].get("u1")["traffic_used_bytes"] > 0


def test_live_speed_reflects_last_window_and_pauses_to_zero(stack) -> None:
    """The dashboard shows wire speed: playing = bytes/s, paused = 0."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    time.sleep(0.2)
    asyncio.run(sampler.tick())
    assert sampler.live_speeds()["s1"] > 0
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000, paused=True)])
    asyncio.run(sampler.tick())
    assert sampler.live_speeds()["s1"] == 0


def test_long_gaps_are_clamped(stack) -> None:
    """A panel outage must not produce a surprise bill."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    # Simulate the sampler having been down for a day.
    sampler._live["s1"]["last_ts"] = time.time() - 86400
    result = asyncio.run(sampler.tick())
    max_possible = 8_000_000 / 8 * 121
    assert 0 < result["billed_bytes"] <= max_possible


def test_bitrate_prefers_transcode_then_source_then_floor() -> None:
    assert session_bitrate({"TranscodingInfo": {"Bitrate": 3_000_000},
                            "NowPlayingItem": {"Bitrate": 9_000_000}}) == 3_000_000
    assert session_bitrate({"NowPlayingItem": {"Bitrate": 9_000_000}}) == 9_000_000
    # Unreported must not be free.
    assert session_bitrate({"NowPlayingItem": {}}) == 4_000_000


def test_is_playing_requires_an_item() -> None:
    assert is_playing({"NowPlayingItem": {"Id": "x"}, "PlayState": {}}) is True
    assert is_playing({"PlayState": {}}) is False


def test_devices_are_tracked_from_sessions(stack) -> None:
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    assert stack["members"].get("u1")["device_count"] == 1


def test_max_devices_refuses_new_device_and_audits(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"group_id": "standard"})
    m.set_overrides("u1", {"max_devices": 1})
    assert m.register_device("u1", "phone") is True
    assert m.register_device("u1", "tablet") is False
    assert m.register_device("u1", "phone") is True  # existing still refreshes
    rows = stack["db"].query(
        "SELECT action FROM audit_log WHERE action='device.refused'")
    assert len(rows) == 1


def test_exhausted_member_is_cut_off_within_one_tick(stack) -> None:
    """The gap between 'quota reached' and 'playback stops' is free traffic."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"],
                           stack["enforcement"])
    stack["groups"].create({"id": "tiny", "name": "Tiny",
                            "billing_mode": "traffic",
                            "traffic_quota_bytes": 1024})
    stack["members"].upsert("u1", "demo-user-1", {"group_id": "tiny"})
    stack["emby"].set_sessions([_session("s1", "u1", 80_000_000)])
    asyncio.run(sampler.tick())
    time.sleep(0.2)
    result = asyncio.run(sampler.tick())
    assert result.get("enforced", 0) >= 1
    assert stack["members"].get("u1")["state"] == "exhausted"
    assert stack["emby"]._users["u1"]["Policy"]["IsDisabled"] is True
    assert stack["emby"]._sessions == []


def test_switching_title_records_two_plays(stack) -> None:
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000, item="i1")])
    asyncio.run(sampler.tick())
    sampler._live["s1"]["seconds"] = 60.0
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000, item="i2")])
    asyncio.run(sampler.tick())
    rows = stack["db"].query("SELECT item_id FROM play_events")
    assert [r["item_id"] for r in rows] == ["i1"]


def test_short_plays_are_not_recorded(stack) -> None:
    """A mis-tap must not pollute 'top titles'."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    stack["emby"].set_sessions([])
    asyncio.run(sampler.tick())
    assert stack["db"].query("SELECT * FROM play_events") == []


# ---- statistics ------------------------------------------------------------
def test_overview_counts_by_group_state(stack) -> None:
    stack["members"].upsert("u1", "alice", {"group_id": "standard"})
    stack["members"].upsert("u2", "bob", {"group_id": "vip"})
    stack["members"].upsert("u3", "carol", {
        "group_id": "standard", "expires_at": int(time.time()) - 10})
    o = StatsService(stack["db"]).overview(30)
    assert o["members"]["total"] == 3
    assert o["members"]["active"] == 2
    assert o["members"]["expired"] == 1
    assert "revenue" not in o  # money left with the plans


def test_daily_series_is_zero_filled(stack) -> None:
    """Watch gaps are zero-filled; unavailable daily measured bytes stay unknown."""
    series = StatsService(stack["db"]).daily_series(7)
    assert len(series) == 7
    assert all(p['hours'] == 0 and p['bytes'] is None for p in series)
    assert series[0]["day"] < series[-1]["day"]


def test_prune_drops_old_history(stack) -> None:
    stack["db"].execute(
        "INSERT INTO play_events (emby_user_id,item_id,started_at,ended_at,"
        "bytes,seconds) VALUES ('u1','old',?,?,1,1)",
        (int(time.time()) - 500 * 86400, int(time.time()) - 500 * 86400))
    assert StatsService(stack["db"]).prune(400)["play_events"] == 1


# ---- image cache -----------------------------------------------------------
def test_cache_key_ignores_credentials_but_not_size() -> None:
    c = ImageCache(tempfile.mkdtemp())
    k1 = c.key("1", "Primary", {"maxWidth": "400", "api_key": "a"})
    k2 = c.key("1", "Primary", {"maxWidth": "400", "api_key": "b"})
    k3 = c.key("1", "Primary", {"maxWidth": "800"})
    assert k1 == k2 and k1 != k3


def test_concurrent_misses_produce_one_upstream_fetch() -> None:
    """A library grid must not turn one cold poster into N upstream requests."""
    c = ImageCache(tempfile.mkdtemp())
    key = c.key("1", "Primary", {})
    calls = []

    async def produce():
        calls.append(1)
        await asyncio.sleep(0.05)
        return (b"\xff\xd8\xff" + b"x" * 500, "image/jpeg", "etag")

    async def main():
        await asyncio.gather(*[c.fetch(key, produce) for _ in range(20)])
        return await c.fetch(key, produce)

    result = asyncio.run(main())
    assert len(calls) == 1
    assert result and len(result[0]) == 503
    assert c.stats()["entries"] == 1


def test_non_images_are_never_cached() -> None:
    """An HTML error page cached as a poster persists a transient failure."""
    c = ImageCache(tempfile.mkdtemp())
    key = c.key("1", "Primary", {})
    assert c.store(key, b"<html>error</html>", "text/html") is False
    assert c.lookup(key) is None


def test_negative_results_are_remembered_briefly() -> None:
    """Otherwise an item with no artwork is refetched on every render."""
    c = ImageCache(tempfile.mkdtemp())
    key = c.key("1", "Primary", {})
    calls = []

    async def produce():
        calls.append(1)

    async def main():
        await c.fetch(key, produce)
        await c.fetch(key, produce)

    asyncio.run(main())
    assert len(calls) == 1


def test_lru_eviction_respects_the_budget() -> None:
    c = ImageCache(tempfile.mkdtemp(), max_bytes=64 * 1024 * 1024)
    blob = b"\xff\xd8\xff" + b"x" * (1024 * 1024)
    for i in range(40):
        c.store(c.key(str(i), "Primary", {}), blob, "image/jpeg")
    before = c.stats()["bytes"]
    c._max_bytes = 8 * 1024 * 1024
    c.sweep(force=True)
    assert c.stats()["bytes"] < before


# ---- API -------------------------------------------------------------------
def test_membership_api_roundtrip() -> None:
    with TestClient(app) as client:
        groups = client.get("/api/groups", headers=_basic()).json()
        assert {g["id"] for g in groups} >= {"standard", "vip"}

        listing = client.get("/api/members", headers=_basic()).json()
        assert "members" in listing and "unmanaged" in listing
        # Mock Emby users start unenrolled -- the population that costs money.
        assert any(u["username"] == "demo-user-1" for u in listing["unmanaged"])

        m = client.put("/api/members/u1", headers=_basic(),
                       json={"group_id": "standard",
                             "username": "demo-user-1"}).json()
        assert m["group_id"] == "standard" and m["state"] == "active"

        m = client.post("/api/members/u1/roles", headers=_basic(),
                        json={"roles": ["uploader"]}).json()
        assert m["roles"] == ["uploader"]

        detail = client.get("/api/members/u1", headers=_basic()).json()
        assert "member" in detail and "usage" in detail and "devices" in detail

        r = client.put("/api/members/u1/overrides", headers=_basic(),
                       json={"bandwidth_limit_kbps": 20000})
        assert r.status_code == 200
        assert r.json()["overridden_keys"] == ["bandwidth_limit_kbps"]

        r = client.put("/api/members/u1/overrides", headers=_basic(),
                       json={"max_bitrate_kbps": 1})
        assert r.status_code in (400, 422)

        assert client.delete("/api/members/u1",
                             headers=_basic()).json()["deleted"]


def test_enroll_defaults_endpoint() -> None:
    with TestClient(app) as client:
        out = client.post("/api/members/enroll-defaults",
                          headers=_basic()).json()
        # Mock Emby: u1 + u2 are plain users, admin is excluded.
        assert out["enrolled"] == 2
        listing = client.get("/api/members", headers=_basic()).json()
        enrolled = {m["emby_user_id"] for m in listing["members"]}
        assert enrolled == {"u1", "u2"}


def test_sessions_report_speed_not_bitrate() -> None:
    with TestClient(app) as client:
        sessions = client.get("/api/emby/sessions", headers=_basic()).json()
        assert sessions and "SpeedMBps" in sessions[0]
        assert "BitrateMbps" not in sessions[0] and "SpeedSource" in sessions[0]


def test_group_in_use_delete_is_http_422() -> None:
    with TestClient(app) as client:
        client.put("/api/members/u1", headers=_basic(),
                   json={"group_id": "standard", "username": "demo-user-1"})
        r = client.delete("/api/groups/standard", headers=_basic())
        assert r.status_code == 422
        assert "用户" in (r.json().get("detail") or "")


def test_admin_role_may_use_panel_with_emby_credentials() -> None:
    with TestClient(app) as client:
        client.put("/api/members/u1", headers=_basic(),
                   json={"group_id": "vip", "username": "demo-user-1"})
        client.post("/api/members/u1/roles", headers=_basic(),
                    json={"roles": ["admin"]})
        # Mock Emby accepts any non-empty password unless one was set.
        r = client.get("/api/groups", headers=_basic("demo-user-1", "pw"))
        assert r.status_code == 200
        # A non-admin member must not get in.
        client.put("/api/members/u2", headers=_basic(),
                   json={"group_id": "standard", "username": "demo-user-2"})
        r = client.get("/api/groups", headers=_basic("demo-user-2", "pw"))
        assert r.status_code == 401


def test_stats_endpoints_answer() -> None:
    with TestClient(app) as client:
        assert client.get("/api/stats/overview", headers=_basic()).status_code == 200
        assert len(client.get("/api/stats/daily?days=7", headers=_basic()).json()) == 7
        for path in ("top-users", "top-titles", "clients", "nodes", "play-methods"):
            assert client.get(f"/api/stats/{path}", headers=_basic()).status_code == 200
        assert client.get("/api/audit", headers=_basic()).status_code == 200
        assert client.get("/api/usage/status", headers=_basic()).status_code == 200


def test_image_cache_settings_validation() -> None:
    with TestClient(app) as client:
        cfg = client.get("/api/settings/image-cache", headers=_basic()).json()
        assert cfg["enabled"] is True and "stats" in cfg
        assert client.put("/api/settings/image-cache", headers=_basic(),
                          json={"max_gib": 0}).status_code == 422
        assert client.put("/api/settings/image-cache", headers=_basic(),
                          json={"max_age_days": 99999}).status_code == 422
        saved = client.put("/api/settings/image-cache", headers=_basic(),
                           json={"max_gib": 8, "max_age_days": 14}).json()
        assert saved["max_gib"] == 8 and saved["max_age_days"] == 14


def test_membership_settings_validation() -> None:
    with TestClient(app) as client:
        got = client.get("/api/settings/membership", headers=_basic())
        assert got.status_code == 200
        assert "enforcement_enabled" in got.json()
        overview = client.get("/api/settings", headers=_basic()).json()
        assert "membership" in overview and "image_cache" in overview
        assert client.put("/api/settings/membership", headers=_basic(),
                          json={"sample_interval_seconds": 1}).status_code == 422
        assert client.put("/api/settings/membership", headers=_basic(),
                          json={"retention_days": 5}).status_code == 422
        saved = client.put("/api/settings/membership", headers=_basic(),
                           json={"enforcement_enabled": True,
                                 "sample_interval_seconds": 20}).json()
        assert saved["enforcement_enabled"] is True


def test_storage_remote_in_use_cannot_be_deleted() -> None:
    with TestClient(app) as client:
        r = client.delete("/api/storage/remotes/mock-drive", headers=_basic())
        assert r.status_code == 409
        assert "挂载" in (r.json().get("detail") or "")
