"""Intake pipeline observability.

The page is read during an incident, so the tests that matter most are the
unhappy ones: a missing directory, a truncated JSON file, a log in an
unexpected shape and an unreachable media server must each degrade on their
own and never blank the page or raise.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.adapters.mock import MockEmby
from app.main import app
from app.modules.downloaders import QbittorrentClient
from app.modules.intake import (
    DEFAULT_THRESHOLDS,
    MAX_QUEUE_PARSE,
    FsReader,
    IntakeCollector,
    IntakePaths,
    parse_notify_tail,
    parse_probe_hotspots,
    probe_group,
)
from app.modules.intake_plugin import IntakePipelinePlugin, IntakeStore


def _basic(user: str = "admin", pw_value: str = "change-me") -> dict[str, str]:
    creds = base64.b64encode(f"{user}:{pw_value}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def intake_root(tmp_path):
    """A fully populated, credential-free stand-in for a real host layout."""
    now = time.time()
    root = tmp_path / "state"

    queue = root / "refresh-queue"
    queue.mkdir(parents=True)
    (queue / "a.json").write_text(json.dumps({
        "container_dir": "/media/Shows/Demo/Season 1",
        "relative_dir": "Shows/Demo/Season 1",
        "event_count": 12,
        "first_event_ts": now - 9 * 3600,
        "paths": ["/media/Shows/Demo/Season 1/e01.mkv"],
    }), encoding="utf-8")
    (queue / "b.json").write_text(json.dumps({
        "relative_dir": "Shows/Other/Season 2",
        "event_count": 3,
        "first_event_ts": now - 600,
        "paths": [],
    }), encoding="utf-8")
    sent = root / "refresh-sent"
    sent.mkdir()
    (sent / "x.last").write_text("1", encoding="utf-8")

    pending = root / "notify-pending"
    pending.mkdir()
    (pending / "p1.json").write_text("{}", encoding="utf-8")

    notify_log = root / "notify.log"
    notify_log.write_text(
        "[2026-01-01 10:00:00]   queued something\n"
        "[2026-01-01 10:05:00]   pending series sent final=removed title=Demo S01\n",
        encoding="utf-8")

    lanes = root / "lanes"
    (lanes / "lane-a" / "Shows").mkdir(parents=True)
    (lanes / "lane-a" / "Shows" / "one.mkv").write_bytes(b"x" * 2048)
    (lanes / "lane-a" / "Shows" / ".keep").write_bytes(b"")
    (lanes / "lane-b").mkdir()

    staging = root / "staging"
    (staging / "Shows").mkdir(parents=True)
    (staging / "Shows" / "s.mkv").write_bytes(b"y" * 512)

    upload_state = root / "upload-state"
    upload_state.mkdir()
    (upload_state / "identity-one.rate-limited").write_text("", encoding="utf-8")
    (upload_state / "identity-two.last-probe").write_text("", encoding="utf-8")

    claims = root / "claims"
    claims.mkdir()
    for job in ("job1", "job2", "job3"):
        (claims / f"{job}.json").write_text("{}", encoding="utf-8")
    done = root / "done"
    done.mkdir()
    (done / "job1-done-20260101-000000.json").write_text("{}", encoding="utf-8")

    backlog = root / "backlog.json"
    backlog.write_text(json.dumps({"at": now, "rows": [{"job_id": "a"}, {"job_id": "b"}]}),
                       encoding="utf-8")
    queue_file = root / "queue.json"
    queue_file.write_text(json.dumps(["/one", "/two"]), encoding="utf-8")
    active = root / "active.json"
    active.write_text(json.dumps({
        "mode": "folder_sequential", "created_at": now - 3600,
        "manifest": [{"n": 1}, {"n": 2}, {"n": 3}],
    }), encoding="utf-8")

    return IntakePaths(
        refresh_queue_dir=str(queue),
        refresh_sent_dir=str(sent),
        refresh_suppress_file=str(root / "suppress-flag"),
        notify_pending_dir=str(pending),
        notify_log=str(notify_log),
        upload_lane_root=str(lanes),
        staging_dir=str(staging),
        local_fallback_dir=str(root / "missing-fallback"),
        quarantine_dir=str(root / "missing-quarantine"),
        upload_state_dir=str(upload_state),
        cloud_claims_dir=str(claims),
        cloud_done_dir=str(done),
        cloud_pending_dir=str(root / "pending-identity"),
        cloud_events_dir=str(root / "events"),
        cloud_backlog_file=str(backlog),
        cloud_queue_file=str(queue_file),
        cloud_active_file=str(active),
    )


class StubEmby:
    """Media server whose three calls can each be made to fail on their own."""

    def __init__(self, tasks=None, latest=None, log="", fail=()):
        self._tasks = tasks if tasks is not None else []
        self._latest = latest if latest is not None else {"Items": []}
        self._log = log
        self._fail = set(fail)

    async def scheduled_tasks(self):
        if "tasks" in self._fail:
            raise RuntimeError("boom")
        return self._tasks

    async def latest_created(self, limit=1):
        if "latest" in self._fail:
            raise RuntimeError("boom")
        return self._latest

    async def server_log_tail(self, max_bytes=512_000):
        if "log" in self._fail:
            raise RuntimeError("boom")
        return self._log


def _scan_task(state="Running", pct=42.0, status="Completed"):
    return {
        "Key": "RefreshLibrary", "Name": "Scan media library", "State": state,
        "CurrentProgressPercentage": pct,
        "LastExecutionResult": {"Status": status,
                                "EndTimeUtc": "2026-01-01T00:00:00.0000000Z"},
    }


# ---------------------------------------------------------------------------
# log / path parsing
# ---------------------------------------------------------------------------
def test_probe_group_takes_two_levels() -> None:
    assert probe_group("/media/Shows/Demo/Season 1/e01.mkv") == "/media/Shows"
    assert probe_group("/media/x.mkv") == "/media/x.mkv"
    assert probe_group("") == ""


def test_parse_probe_hotspots_detects_concentration() -> None:
    line = ("2026-01-01 00:00:00.000 Info MediaProbeManager: ProcessRun 'ffprobe' "
            "Execute: /bin/ffprobe -i file:\"{p}\" -threads 0")
    text = "\n".join(
        [line.format(p="/media/Shows/Loop/a.mkv")] * 8
        + [line.format(p="/media/Movies/Other/b.mkv")] * 2)
    out = parse_probe_hotspots(text)
    assert out["available"] is True
    assert out["samples"] == 10
    assert out["top_group"] == "/media/Shows"
    assert out["top_ratio"] == 0.8


def test_parse_probe_hotspots_handles_unmatched_log() -> None:
    """A log in a shape we do not recognise is 'unknown', never 'nothing'."""
    out = parse_probe_hotspots("some other service wrote this\nand this\n")
    assert out["available"] is False
    assert out["samples"] == 0
    assert out["groups"] == []


def test_parse_probe_hotspots_handles_empty() -> None:
    assert parse_probe_hotspots("")["available"] is False


def test_parse_notify_tail_finds_last_delivery() -> None:
    text = ("[2026-01-01 10:00:00]   queued\n"
            "[2026-01-01 10:05:00]   pending series sent final=removed title=A\n"
            "[2026-01-01 10:06:00]   emby episode wait\n")
    out = parse_notify_tail(text)
    assert out["available"] is True
    assert "title=A" in out["line"]
    assert out["ts"] is not None


def test_parse_notify_tail_ignores_chatter_without_delivery() -> None:
    """Retry chatter is not a delivery; reporting the newest line would hide
    that nothing has actually been sent."""
    out = parse_notify_tail("[2026-01-01 10:00:00]   emby episode wait\n")
    assert out["available"] is False


def test_parse_notify_tail_accepts_alternate_marker() -> None:
    out = parse_notify_tail("[2026-01-01 10:00:00]   card sent and removed ok\n")
    assert out["available"] is True


# ---------------------------------------------------------------------------
# collector: healthy host
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_collect_refresh_reads_queue(intake_root) -> None:
    collector = IntakeCollector(paths=intake_root, fs=FsReader())
    out = collector.collect_refresh()
    assert out["available"] is True
    assert out["total"] == 2
    assert out["parsed"] == 2
    assert out["unreadable"] == 0
    assert out["oldest_age_seconds"] > 8 * 3600
    assert out["suppressed"] is False
    assert out["sent_total"] == 1
    assert out["top"][0]["label"] == "Shows/Demo/Season 1"


def test_collect_refresh_reports_suppression(intake_root, tmp_path) -> None:
    flag = tmp_path / "state" / "suppress-flag"
    flag.write_text("", encoding="utf-8")
    out = IntakeCollector(paths=intake_root, fs=FsReader()).collect_refresh()
    assert out["suppressed"] is True


def test_collect_refresh_survives_corrupt_json(intake_root) -> None:
    """One unreadable entry is counted and skipped, not fatal."""
    from pathlib import Path
    Path(intake_root.refresh_queue_dir, "bad.json").write_text(
        "{not json", encoding="utf-8")
    out = IntakeCollector(paths=intake_root, fs=FsReader()).collect_refresh()
    assert out["available"] is True
    assert out["total"] == 3
    assert out["parsed"] == 2
    assert out["unreadable"] == 1


def test_collect_refresh_missing_directory() -> None:
    out = IntakeCollector(paths=IntakePaths(refresh_queue_dir="/nonexistent/x"),
                          fs=FsReader()).collect_refresh()
    assert out["available"] is False
    assert out["suppressed"] is False


def test_collect_notify(intake_root) -> None:
    out = IntakeCollector(paths=intake_root, fs=FsReader()).collect_notify()
    assert out["pending"]["available"] is True
    assert out["pending"]["total"] == 1
    assert out["last_sent"]["available"] is True


def test_collect_notify_missing_everything() -> None:
    out = IntakeCollector(paths=IntakePaths(), fs=FsReader()).collect_notify()
    assert out["pending"]["available"] is False
    assert out["last_sent"]["available"] is False


def test_collect_upload_skips_placeholder_files(intake_root) -> None:
    """Zero-byte lane markers must not read as queued work."""
    out = IntakeCollector(paths=intake_root, fs=FsReader()).collect_upload()
    lanes = out["lanes"]
    assert lanes["available"] is True
    assert lanes["items"] == 1
    assert lanes["bytes"] == 2048
    names = {x["name"] for x in lanes["lanes"]}
    assert names == {"lane-a", "lane-b"}
    assert out["rate_limited"] == ["identity-one"]
    buffers = {b["name"]: b for b in out["buffers"]}
    assert buffers["staging"]["available"] is True
    assert buffers["local-fallback"]["available"] is False


def test_collect_upload_missing_root() -> None:
    out = IntakeCollector(paths=IntakePaths(), fs=FsReader()).collect_upload()
    assert out["lanes"]["available"] is False
    assert out["rate_limited_known"] is False


def test_collect_cloud_counts_outstanding_claims(intake_root) -> None:
    out = IntakeCollector(paths=intake_root, fs=FsReader()).collect_cloud()
    assert out["claims"]["total"] == 3
    assert out["claims"]["done"] == 1
    assert out["claims"]["outstanding"] == 2
    assert out["claims"]["truncated"] is False
    assert out["backlog"]["rows"] == 2
    assert out["queue"]["depth"] == 2
    assert out["active"]["manifest_items"] == 3
    assert out["pending_identity"] is None


def test_collect_cloud_refuses_to_subtract_a_truncated_listing(
        intake_root, monkeypatch) -> None:
    """A capped listing cannot support claims-minus-receipts.

    Observed live: with both directories at ~8k entries and a 4000 cap, every
    claim whose receipt fell outside the window counted as unfinished and the
    page reported 2103 outstanding jobs against a true figure of five. An
    admitted unknown is worth more than a confident wrong number.
    """
    monkeypatch.setattr("app.modules.intake.MAX_COUNT_FILES", 2)
    out = IntakeCollector(paths=intake_root, fs=FsReader()).collect_cloud()
    assert out["claims"]["available"] is True
    assert out["claims"]["truncated"] is True
    assert out["claims"]["outstanding"] is None


def test_refresh_depth_is_exact_beyond_the_parse_limit(tmp_path) -> None:
    """Queue depth is a headline number: it must count every entry even though
    only a handful are opened and rendered."""
    queue = tmp_path / "queue"
    queue.mkdir()
    for i in range(MAX_QUEUE_PARSE + 25):
        (queue / f"{i:05d}.json").write_text("{}", encoding="utf-8")
    out = IntakeCollector(paths=IntakePaths(refresh_queue_dir=str(queue)),
                          fs=FsReader()).collect_refresh()
    assert out["total"] == MAX_QUEUE_PARSE + 25
    assert out["truncated"] is False
    assert len(out["top"]) <= 5


def test_collect_cloud_missing_state() -> None:
    out = IntakeCollector(paths=IntakePaths(), fs=FsReader()).collect_cloud()
    assert out["claims"]["available"] is False
    assert out["backlog"]["available"] is False
    assert out["queue"]["available"] is False


def test_collect_cloud_corrupt_backlog(tmp_path) -> None:
    bad = tmp_path / "backlog.json"
    bad.write_text("{{{", encoding="utf-8")
    out = IntakeCollector(paths=IntakePaths(cloud_backlog_file=str(bad)),
                          fs=FsReader()).collect_cloud()
    assert out["backlog"]["available"] is False


# ---------------------------------------------------------------------------
# collector: media server
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_collect_emby_full() -> None:
    created = time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime())
    emby = StubEmby(
        tasks=[{"Key": "Other", "Name": "Scan External Tracks", "State": "Idle"},
               _scan_task()],
        latest={"Items": [{"Type": "Episode", "DateCreated": created}]},
        log=("Info MediaProbeManager: ProcessRun 'ffprobe' Execute: "
             "/bin/ffprobe -i file:\"/media/Shows/A/x.mkv\" -threads 0"))
    out = await IntakeCollector(emby=emby).collect_emby()
    assert out["scan"]["running"] is True
    assert out["scan"]["progress"] == 42.0
    assert out["latest"]["available"] is True
    assert out["latest"]["age_minutes"] < 5
    assert out["probe"]["available"] is True


@pytest.mark.asyncio
async def test_collect_emby_prefers_task_key_over_name() -> None:
    """Several tasks have 'scan' in the name; the key is the stable match."""
    emby = StubEmby(tasks=[
        {"Key": "ScanExternalTrackTask", "Name": "Scan External Tracks",
         "State": "Idle"},
        {"Key": "ScanInternalMetadataFolderTask", "Name": "Scan Metadata Folder",
         "State": "Idle"},
        _scan_task(state="Running"),
    ])
    out = await IntakeCollector(emby=emby).collect_emby()
    assert out["scan"]["name"] == "Scan media library"
    assert out["scan"]["running"] is True


@pytest.mark.asyncio
async def test_collect_emby_each_call_degrades_alone() -> None:
    emby = StubEmby(tasks=[_scan_task()], fail=("latest", "log"))
    out = await IntakeCollector(emby=emby).collect_emby()
    assert out["scan"]["available"] is True
    assert out["latest"]["available"] is False
    assert out["probe"]["available"] is False


@pytest.mark.asyncio
async def test_collect_emby_unreachable() -> None:
    emby = StubEmby(fail=("tasks", "latest", "log"))
    out = await IntakeCollector(emby=emby).collect_emby()
    assert out["available"] is True
    assert out["scan"]["available"] is False
    assert out["latest"]["available"] is False


@pytest.mark.asyncio
async def test_collect_emby_not_configured() -> None:
    out = await IntakeCollector(emby=None).collect_emby()
    assert out["available"] is False


@pytest.mark.asyncio
async def test_collect_emby_missing_scan_task() -> None:
    out = await IntakeCollector(emby=StubEmby(tasks=[{"Key": "Other"}])).collect_emby()
    assert out["scan"]["available"] is False


@pytest.mark.asyncio
async def test_latest_created_timestamp_is_utc() -> None:
    """A UTC stamp parsed as local time would report a fresh item as hours old
    and trip the stall alarm on a healthy system."""
    created = time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime())
    out = await IntakeCollector(
        emby=StubEmby(latest={"Items": [{"DateCreated": created}]})).collect_emby()
    assert out["latest"]["age_seconds"] < 120


# ---------------------------------------------------------------------------
# health verdict
# ---------------------------------------------------------------------------
def _snapshot(latest_minutes=1.0, pending=0, top_ratio=0.1,
              refresh_age=60.0, rate_limited=(), suppressed=False,
              scanning=False):
    return {
        "emby": {
            "latest": {"available": True, "age_minutes": latest_minutes},
            "probe": {"available": True, "top_ratio": top_ratio,
                      "top_group": "/media/Shows"},
            "scan": {"available": True, "running": scanning},
        },
        "notify": {"pending": {"available": True, "total": pending}},
        "refresh": {"available": True, "oldest_age_seconds": refresh_age,
                    "suppressed": suppressed},
        "upload": {"rate_limited": list(rate_limited)},
    }


def test_health_green_on_healthy_pipeline() -> None:
    out = IntakeCollector().evaluate(_snapshot())
    assert out["level"] == "ok"
    assert out["alerts"] == []


def test_health_red_when_stalled_with_pending_notifications() -> None:
    out = IntakeCollector().evaluate(_snapshot(latest_minutes=200, pending=4))
    assert out["level"] == "bad"


def test_health_quiet_night_is_not_an_alert() -> None:
    """No arrivals and nothing waiting is an idle system, not a fault. This is
    the false positive that would make the light useless overnight."""
    out = IntakeCollector().evaluate(_snapshot(latest_minutes=600, pending=0))
    assert out["level"] == "ok"


def test_health_red_on_probe_loop() -> None:
    out = IntakeCollector().evaluate(_snapshot(top_ratio=0.9))
    assert out["level"] == "bad"
    assert "探测" in out["alerts"][0]["message"]


def test_health_probe_concentration_during_a_scan_is_expected() -> None:
    """A running full-library scan walks one directory at a time, so it
    concentrates probes by construction -- observed at 100% on a healthy
    server. Alarming then would hold the page red for the whole scan, which is
    how an indicator stops being read."""
    out = IntakeCollector().evaluate(_snapshot(top_ratio=1.0, scanning=True))
    assert out["level"] == "idle"
    assert "扫描进行中" in out["alerts"][0]["message"]


def test_health_probe_loop_still_red_when_nothing_is_scanning() -> None:
    out = IntakeCollector().evaluate(_snapshot(top_ratio=1.0, scanning=False))
    assert out["level"] == "bad"
    assert "疑似探测循环" in out["alerts"][0]["message"]


def test_health_probe_below_threshold_is_quiet() -> None:
    out = IntakeCollector().evaluate(_snapshot(top_ratio=0.4))
    assert out["level"] == "ok"


def test_health_amber_on_old_refresh_queue() -> None:
    out = IntakeCollector().evaluate(_snapshot(refresh_age=8 * 3600))
    assert out["level"] == "warn"


def test_health_old_refresh_queue_is_expected_while_suppressed() -> None:
    """With the switch deliberately off, a growing queue is the switch working.
    Flagging it amber trains the operator to ignore the colour."""
    out = IntakeCollector().evaluate(
        _snapshot(refresh_age=8 * 3600, suppressed=True))
    assert out["level"] == "idle"
    assert any("属预期" in a["message"] for a in out["alerts"])


def test_health_amber_on_rate_limit_marker() -> None:
    out = IntakeCollector().evaluate(_snapshot(rate_limited=["identity-one"]))
    assert out["level"] == "warn"


def test_health_grey_when_suppressed() -> None:
    out = IntakeCollector().evaluate(_snapshot(suppressed=True))
    assert out["level"] == "idle"


def test_health_thresholds_are_configurable() -> None:
    strict = IntakeCollector(thresholds={**DEFAULT_THRESHOLDS,
                                         "stale_intake_minutes": 10})
    assert strict.evaluate(_snapshot(latest_minutes=30, pending=1))["level"] == "bad"
    relaxed = IntakeCollector(thresholds={**DEFAULT_THRESHOLDS,
                                          "stale_intake_minutes": 600})
    assert relaxed.evaluate(_snapshot(latest_minutes=30, pending=1))["level"] == "ok"


def test_health_ignores_unavailable_sections() -> None:
    """Missing evidence must not manufacture a verdict."""
    snapshot = {"emby": {"latest": {"available": False},
                         "probe": {"available": False}},
                "notify": {"pending": {"available": False}},
                "refresh": {"available": False},
                "upload": {}}
    assert IntakeCollector().evaluate(snapshot)["level"] == "ok"


# ---------------------------------------------------------------------------
# downloader summaries
# ---------------------------------------------------------------------------
def test_downloader_summarise_splits_complete_and_running() -> None:
    out = QbittorrentClient.summarise([
        {"progress": 1.0, "total_size": 100},
        {"progress": 1, "total_size": 50},
        {"progress": 0.4, "total_size": 20},
    ])
    assert out["total"] == 3
    assert out["completed"] == 2
    assert out["completed_bytes"] == 150
    assert out["downloading"] == 1
    assert out["downloading_bytes"] == 20


def test_downloader_summarise_tolerates_junk() -> None:
    out = QbittorrentClient.summarise([{"progress": "x", "total_size": "y"}, "junk"])
    assert out["total"] == 1
    assert out["downloading"] == 1


def test_downloader_summarise_non_list() -> None:
    assert QbittorrentClient.summarise({"error": 1})["total"] == 0


@pytest.mark.asyncio
async def test_collect_downloader_marks_failing_client() -> None:
    class Boom:
        name = "broken"

        async def summary(self):
            raise RuntimeError("nope")

    out = await IntakeCollector().collect_downloader([Boom()])
    assert out["clients"][0]["available"] is False
    assert out["clients"][0]["name"] == "broken"


# ---------------------------------------------------------------------------
# snapshot store + plugin
# ---------------------------------------------------------------------------
def test_store_reports_not_collected_yet() -> None:
    assert IntakeStore().get()["available"] is False


def test_store_keeps_last_good_snapshot_after_failure() -> None:
    """A failed collection must not erase the last good answer: an old number
    with an honest age beats nothing at all during an incident."""
    store = IntakeStore()
    store.put({"health": {"level": "ok"}})
    store.fail("RuntimeError: upstream down")
    out = store.get()
    assert out["available"] is True
    assert out["data"]["health"]["level"] == "ok"
    assert "upstream down" in out["error"]


@pytest.mark.asyncio
async def test_plugin_collects_into_store(intake_root) -> None:
    class Ctx:
        def __init__(self) -> None:
            self.intake_store = IntakeStore()
            self.intake_paths = intake_root
            self.intake_fs = FsReader()
            self.intake_emby = StubEmby(tasks=[_scan_task()])
            self.intake_downloaders = []

    ctx = Ctx()
    plugin = IntakePipelinePlugin(ctx)
    summary = await plugin.run(plugin.defaults())
    assert summary["健康"] in {"ok", "warn", "bad", "idle"}
    snapshot = ctx.intake_store.get()
    assert snapshot["available"] is True
    assert snapshot["data"]["refresh"]["total"] == 2


@pytest.mark.asyncio
async def test_plugin_without_store_reports_error() -> None:
    class Ctx:
        pass

    out = await IntakePipelinePlugin(Ctx()).run({})
    assert out["ok"] is False


def test_plugin_threshold_conversion() -> None:
    plugin = IntakePipelinePlugin(object())
    out = plugin.thresholds({"stale_intake_minutes": 45,
                             "probe_hotspot_ratio_percent": 70,
                             "refresh_age_warn_hours": 3})
    assert out["stale_intake_minutes"] == 45
    assert out["probe_hotspot_ratio"] == 0.7
    assert out["refresh_age_warn_hours"] == 3


def test_plugin_threshold_rejects_junk() -> None:
    out = IntakePipelinePlugin(object()).thresholds(
        {"stale_intake_minutes": "abc"})
    assert out["stale_intake_minutes"] == DEFAULT_THRESHOLDS["stale_intake_minutes"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def test_intake_api_requires_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/intake").status_code == 401


def test_intake_api_serves_snapshot_in_mock_mode() -> None:
    with TestClient(app) as client:
        refreshed = client.post("/api/intake/refresh", headers=_basic())
        assert refreshed.status_code == 200
        body = client.get("/api/intake", headers=_basic()).json()
        assert body["available"] is True
        data = body["data"]
        assert data["emby"]["scan"]["available"] is True
        assert data["downloader"]["clients"][0]["available"] is True
        assert "level" in data["health"]


def test_intake_api_degrades_without_configured_paths() -> None:
    """Nothing configured is a page full of honest 'not configured', not a 500."""
    with TestClient(app) as client:
        client.post("/api/intake/refresh", headers=_basic())
        data = client.get("/api/intake", headers=_basic()).json()["data"]
        assert data["refresh"]["available"] is False
        assert data["cloud"]["claims"]["available"] is False


def test_intake_plugin_is_registered() -> None:
    with TestClient(app) as client:
        cards = client.get("/api/plugins", headers=_basic()).json()
        ids = {c["id"] for c in cards}
        assert "intake_pipeline" in ids


def test_intake_is_a_live_stream_topic() -> None:
    """The page renders from pushed snapshots; without the topic it would fall
    back to polling an endpoint that only changes once a minute.

    Asserted against the registered producer rather than by consuming the SSE
    response: that stream never ends by design, so reading it in a test can
    only be made to terminate by duplicating the client's framing logic.
    """
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert "intake" in app.state.events._producers


def test_intake_page_is_served_and_reachable() -> None:
    """A page the shell never loads is a page nobody can open."""
    with TestClient(app) as client:
        index = client.get("/", headers=_basic()).text
        assert "/static/intake.js" in index
        assert client.get("/static/intake.js", headers=_basic()).status_code == 200


def test_intake_page_paints_before_awaiting() -> None:
    """Navigation paints before awaiting; pushes use paintIntake without a loader."""
    from pathlib import Path as FilePath

    source = (FilePath(__file__).resolve().parents[1]
              / "app" / "static" / "intake.js").read_text(encoding="utf-8")
    body = source.split("PAGES.intake = async (context = pageContext('intake')) => {", 1)[1]
    assert body.lstrip().startswith("renderView(pageLoading(), context);")
    live_handler = source.split("registerLiveUpdater('intake'", 1)[1]
    assert "paintIntake(payload, context)" in live_handler
    assert "pageLoading" not in live_handler


def test_mock_emby_supports_intake_calls() -> None:
    """Mock mode must cover every new external call, per DEVELOPMENT.md."""
    import asyncio

    emby = MockEmby()
    tasks = asyncio.run(emby.scheduled_tasks())
    assert any(t["Key"] == "RefreshLibrary" for t in tasks)
    latest = asyncio.run(emby.latest_created())
    assert latest["Items"]
    assert "ffprobe" in asyncio.run(emby.server_log_tail())
