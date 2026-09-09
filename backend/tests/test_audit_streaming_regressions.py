"""Scoped full-source playback/watch/cache/measurement regressions, local only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import NodePool, StreamNode
from app.core.db import Database
from app.modules.edgelog import aggregate, parse_line
from app.modules.imagecache import ImageCache
from app.modules.metering import UNIT, MeasuredMeteringService
from app.modules.playback import PlaybackRouter
from app.modules.scheduler import Scheduler
from app.modules.sharing import SharingDetector
from app.modules.stats import StatsService
from app.modules.usage import UsageSampler


def test_signed_rate_and_identity_fields_cannot_be_repartitioned():
    from app.modules.signing import compute_digest, verify

    path, expiry, secret = "/s/main/demo.mkv", 1800000060, "fixture-signing-secret"
    digest = compute_digest(path, expiry, secret, rate_bps=1000, utag="1abcdef012")
    # The old r/u repartition is invalid, not an equivalent authentication.
    with pytest.raises(ValueError):
        compute_digest(path, expiry, secret, rate_bps=10001, utag="abcdef012")
    # Even two syntactically valid fields cannot migrate bytes across path/r.
    assert compute_digest(path + "1", expiry, secret, 0, "1abcdef012") != compute_digest(
        path, expiry, secret, 10, "1abcdef012")
    assert not verify(path, digest, expiry, secret, now=1800000000,
                      rate_bps=10001, utag="abcdef012")


def test_explicit_unknown_media_source_falls_back_instead_of_serving_other_file():
    node = StreamNode(name="node-a", base_url="https://node.example.com", probe_url="http://127.0.0.1/load",
                      pools=[NodePool(name="main", emby_prefix="/media", url_prefix="/s/main")])
    router = PlaybackRouter(
        SimpleNamespace(item_media_paths=AsyncMock(return_value={"source-a": "/media/a.mkv"}),
                        verify_item_access=AsyncMock(return_value=True)),
        SimpleNamespace(pick=lambda **kw: SimpleNamespace(node=node)),
        lambda: {"enabled": True}, lambda: {"url": "https://origin.example.com"})
    decision = asyncio.run(router.route("item", "Videos/item/original.mkv",
                                        {"MediaSourceId": "missing"}, "token", True))
    assert not decision.redirected
    assert decision.reason == "unresolved-item"


def test_cancelled_image_owner_releases_waiters_and_allows_retry(tmp_path):
    async def check():
        cache = ImageCache(tmp_path / "images")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def producer():
            entered.set()
            await release.wait()
            return b"image", "image/png", "tag"

        owner = asyncio.create_task(cache.fetch("a" * 64, producer))
        await entered.wait()
        waiter = asyncio.create_task(cache.fetch("a" * 64, producer))
        await asyncio.sleep(0)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert await asyncio.wait_for(waiter, 0.1) is None
        release.set()
        assert await cache.fetch("a" * 64, producer) == (b"image", "image/png", "tag")
        assert not cache._inflight
    asyncio.run(check())


@pytest.mark.parametrize("metadata", ['[]', '{"stored_at":"bad"}', '{"stored_at":1e999}'])
def test_corrupt_cache_metadata_is_a_miss(tmp_path, metadata):
    cache = ImageCache(tmp_path)
    key = "a" * 64
    cache.store(key, b"image", "image/png")
    cache._paths(key)[1].write_text(metadata)
    assert cache.lookup(key) is None


def test_image_producer_failure_does_not_leave_unretrieved_future(tmp_path):
    async def check():
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx))
        cache = ImageCache(tmp_path)
        assert await cache.fetch("a" * 64, AsyncMock(side_effect=OSError("unavailable"))) is None
        assert not errors
    asyncio.run(check())


@pytest.mark.parametrize("ts", ["nan", "inf", "1e300"])
def test_bad_edge_timestamp_does_not_abort_batch(ts):
    assert parse_line(f"{ts} u=tag r=0 100 1.0") is None


def test_anonymous_edge_bytes_are_retained_as_unknown():
    event = parse_line("1800000000 a=192.0.2.1 p=1234 u= r=0 100 1.0")
    assert event is not None
    assert aggregate([event]) == {("2027-01-15", "unknown"): {"bytes": 100, "requests": 1, "seconds": 1}}


def envelope(boot, seq, at, counter=100, **kwargs):
    return {"node": "node-a", "boot_id": boot, "seq": seq, "observed_at": at,
            "nft_ok": True, "unit": UNIT, "samples": [
                {"conn_id": "conn", "generation": 1, "utag": "tag", "coverage": "observed",
                 "counter_bytes": counter, "observed_at": at}], **kwargs}


def test_meter_coverage_uses_latest_observation_across_epochs_and_replays(tmp_path):
    db = Database(tmp_path / "meter.db")
    service = MeasuredMeteringService(db, lambda: {"tag": "u1"})
    service.ingest(envelope("old", 10, 1800000000))
    service.ingest(envelope("new", 1, 1800000300, 200))
    service.ingest(envelope("old", 10, 1800000000))
    coverage = service.snapshot("u1", now=1800000301)["coverage"]["nodes"][0]
    assert coverage["ok"] and coverage["boot_id"] == "new"
    service.ingest(envelope("new", 0, 1800000001, 100, nft_ok=False))
    assert service.snapshot("u1", now=1800000301)["coverage"]["nodes"][0]["ok"]
    db.close()


def test_equal_late_counter_cannot_move_month_gap_anchor_backwards(tmp_path):
    db = Database(tmp_path / "meter.db")
    service = MeasuredMeteringService(db, lambda: {"tag": "u1"})
    service.ingest(envelope("epoch", 2, 1800000500))
    service.ingest(envelope("epoch", 1, 1800000000))
    assert db.one("SELECT observed_at FROM measured_watermarks")["observed_at"] == 1800000500
    db.close()


@pytest.mark.parametrize("stamp", ["bad", float("nan"), float("inf"), 1e300])
def test_invalid_meter_timestamp_cannot_poison_ledger(tmp_path, stamp):
    db = Database(tmp_path / "meter.db")
    service = MeasuredMeteringService(db)
    out = service.ingest(envelope("epoch", 1, stamp))
    assert not out["ok"]
    assert not db.query("SELECT * FROM measured_envelopes")
    db.close()


def test_invalid_sample_does_not_abort_other_measured_connections(tmp_path):
    db = Database(tmp_path / "meter.db")
    service = MeasuredMeteringService(db, lambda: {"tag": "u1"})
    env = envelope("epoch", 1, 1800000000)
    env["samples"].extend([None, {"conn_id": "bad", "generation": 1, "coverage": "observed",
                                  "counter_bytes": float("inf")}])
    assert service.ingest(env)["credited"] == 100
    db.close()


def test_meter_future_observation_is_not_fresh(tmp_path):
    db = Database(tmp_path / "meter.db")
    service = MeasuredMeteringService(db)
    service.ingest(envelope("epoch", 1, 1800000300))
    assert not service.snapshot("u1", now=1800000000)["coverage"]["nodes"][0]["ok"]
    db.close()


def test_malformed_probe_is_isolated_and_failed_rate_keeps_partial_coverage(monkeypatch):
    async def check():
        clock = 1800000000
        monkeypatch.setattr("app.modules.scheduler.time.time", lambda: clock)
        probe = AsyncMock()
        probe.load.return_value = {"ok": True, "active_streams": 1, "user_speeds": {"tag": 100}}
        scheduler = Scheduler([StreamNode(name=n, base_url=f"https://{n}.example.com", probe_url="http://127.0.0.1/load")
                               for n in ("node-a", "node-b")], probe)
        await scheduler.refresh()
        probe.load.side_effect = [{"ok": True, "active_streams": 1, "user_speeds": None,
                                   "user_speeds_ok": False},
                                  {"ok": True, "active_streams": 1, "user_speeds": {"tag": 10}}]
        await scheduler.refresh()
        assert scheduler.user_speed_view()["tag"]["bps"] is None
        probe.load.side_effect = [{"ok": True, "active_streams": "invalid"}, None]
        await scheduler.refresh()
        assert not any(row["ok"] for row in scheduler.snapshot())
    asyncio.run(check())


def test_sampler_only_passes_actually_playing_current_networks(tmp_path, monkeypatch):
    clock = [1800000000.0]
    monkeypatch.setattr("app.modules.usage.time.time", lambda: clock[0])
    db = Database(tmp_path / "watch.db")
    members = SimpleNamespace(register_device=lambda *a, **kw: True, get=lambda uid: {})
    emby = SimpleNamespace(active_sessions_raw=AsyncMock())
    detector = SharingDetector(db, min_seconds=1)
    sampler = UsageSampler(db, members, emby, sharing=detector)
    session = {"Id": "s", "UserId": "u1", "DeviceId": "d", "RemoteEndPoint": "192.0.2.1",
               "NowPlayingItem": {"Id": "item", "Bitrate": 8000000}, "PlayState": {}}
    emby.active_sessions_raw.return_value = [session]
    assert asyncio.run(sampler.tick())["playing"] == 1
    clock[0] += 30
    session["RemoteEndPoint"] = "198.51.100.1"
    asyncio.run(sampler.tick())
    assert set(detector._seen["u1"]) == {"198.51.100.0/24"}
    clock[0] += 30
    session["PlayState"] = {"IsPaused": True}
    assert asyncio.run(sampler.tick())["playing"] == 0
    assert not detector._seen
    assert StatsService(db).watch_summary("u1", clock[0])["recorded_seconds"] == 30
    db.close()
