"""mediadeck backend entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
import time
from pathlib import Path as FilePath
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from app.adapters.live import LiveEmby, LiveProbe, probe_emby
from app.adapters.mock import MockEmby, MockProbe
from app.core.cache import TTLCache
from app.core.config import settings
from app.core.db import Database
from app.core.errors import ConfigError, ConflictError, NotConfigured, UpstreamError
from app.core.store import SettingsStore
from app.modules import member_ops
from app.modules.access import AccessRules
from app.modules.downloaders import MockDownloader, QbittorrentClient
from app.modules.edgelog import MAX_LINES_PER_INGEST, TrafficLedger
from app.modules.enforcement import EnforcementService
from app.modules.entries import entry_target, identify_entry
from app.modules.entry_proxy import SERVERS as ENTRY_SERVERS
from app.modules.entry_proxy import friend_config
from app.modules.events import EventStream, safe_stream
from app.modules.groups import GroupService
from app.modules.imagecache import ALLOWED_IMAGE_TYPES, ImageCache
from app.modules.imports import ImportManager, JobKind, MockExecutor
from app.modules.intake import FsReader, IntakePaths
from app.modules.intake_plugin import IntakeStore
from app.modules.members import MemberService, random_password, rate_bytes_per_sec
from app.modules.metering import UNIT as METER_UNIT
from app.modules.metering import MeasuredMeteringService
from app.modules.mounts import MockMounts, MountsReader
from app.modules.pipeline import MockPipeline, PipelineReader
from app.modules.playback import PlaybackRouter, caller_device, caller_token
from app.modules.plugins import PluginRegistry
from app.modules.plugins_builtin import (
    PluginContext,
    ensure_request_library_watch,
    migrate_legacy_telegram_jobs,
    register_builtin,
)
from app.modules.points import PointsService
from app.modules.provisioning import (
    emby_frontend_snippet,
    enroll_command,
    install_script,
)
from app.modules.registration import RegistrationService
from app.modules.requests import RequestError, RequestService, parse_status
from app.modules.scheduler import PROBE_INTERVAL, Scheduler
from app.modules.settings import SettingsService
from app.modules.sharing import SharingDetector
from app.modules.shop import ShopError, ShopService
from app.modules.signing import user_tag
from app.modules.stats import StatsService
from app.modules.storage import MockStorage, StorageManager
from app.modules.tasks import MockTasks, TasksReader
from app.modules.telegram import TelegramBot
from app.modules.tmdb import TmdbClient
from app.modules.updater import MockUpdater, Updater
from app.modules.usage import UsageSampler, run_usage_io

app = FastAPI(title="mediadeck", version="0.1.0")
security = HTTPBasic(auto_error=False)
SESSION_COOKIE = "mediadeck_session"
SESSION_TTL = 7 * 86400


async def _check_login(username: str, password: str) -> str | None:
    cfg = settings()
    if not username or not password:
        return None
    user_ok = secrets.compare_digest(username, cfg.mediadeck_admin_user)
    pass_ok = secrets.compare_digest(password, cfg.mediadeck_admin_password)
    if user_ok and pass_ok:
        return username
    # Members holding the admin *role* may operate the panel with their Emby
    # credentials (owner decision 2026-08-30). The role check runs first and
    # reads only our own DB, so a random visitor cannot use the panel login
    # form to brute-force Emby passwords of non-admin accounts.
    return await _role_admin_auth(username, password)


def _session_user(request: Request) -> str | None:
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if not token or not hasattr(app.state, "cache"):
        return None
    user = app.state.cache.get(f"panelsess:{token}")
    return str(user) if user else None


def _issue_session(response: Response, user: str) -> None:
    token = secrets.token_urlsafe(32)
    if hasattr(app.state, "cache"):
        app.state.cache.set(f"panelsess:{token}", user, ttl=SESSION_TTL)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        max_age=SESSION_TTL, path="/")


async def _auth(request: Request,
                credentials: HTTPBasicCredentials | None = Depends(security)) -> str:  # noqa: B008
    if credentials is not None:
        user = await _check_login(credentials.username, credentials.password)
        if user:
            return user
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    user = _session_user(request)
    if user:
        return user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED)


async def _role_admin_auth(username: str, password: str) -> str | None:
    members = getattr(app.state, "members", None)
    emby = getattr(app.state, "emby", None)
    if not members or not emby or not username or not password:
        return None
    candidates = [m for m in await asyncio.to_thread(members.list, role="admin", limit=200)
                  if (m.get("username") or "").lower() == username.lower()
                  and m.get("state") == "active"]
    if not candidates:
        return None
    # Positive results are cached briefly so every API call in a browsing
    # session does not become an Emby authentication round-trip.
    cache_key = f"panelauth:{username}"
    entry = app.state.cache.get(cache_key) if hasattr(app.state, "cache") else None
    if entry and secrets.compare_digest(entry, _digest(password)):
        return username
    try:
        user = await emby.authenticate_user(username, password)
    except Exception:  # noqa: BLE001 - Emby down must read as 401, not 500
        return None
    if not user or str(user.get("Id")) != str(candidates[0]["emby_user_id"]):
        return None
    if hasattr(app.state, "cache"):
        app.state.cache.set(cache_key, _digest(password), ttl=300)
    return username


def _digest(secret_text: str) -> str:
    import hashlib
    return hashlib.sha256(secret_text.encode()).hexdigest()


def _build_downloaders(cfg: Any) -> list[Any]:
    """Torrent clients summarised on the intake page.

    Credentials are read from local configuration into the client object and
    never leave it: they are not returned by any endpoint, not logged, and not
    part of any snapshot.
    """
    if cfg.mediadeck_mock:
        return [MockDownloader("mock-downloader")]
    out = []
    for spec in cfg.intake_downloader_specs():
        out.append(QbittorrentClient(
            name=spec.get("name") or "downloader",
            base_url=spec.get("url") or "",
            username=spec.get("username") or "",
            pw_value=spec.get("password") or "",
        ))
    return out


_storage_lock = threading.RLock()


def _storage_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    # These file read/modify/write operations used to be serialized by the
    # event loop. Preserve that property inside the worker, including when
    # the request is cancelled but its blocking system call is still running.
    with _storage_lock:
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, str(exc)) from None


@app.exception_handler(ConfigError)
async def _config_error_handler(_: Any, exc: ConfigError) -> JSONResponse:
    """Invalid operator input: answer with the message the UI should show."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(ConflictError)
async def _conflict_error_handler(_: Any, exc: ConflictError) -> JSONResponse:
    """Well-formed but currently impossible: the operator must resolve state first."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(NotConfigured)
async def _not_configured_handler(_: Any, exc: NotConfigured) -> JSONResponse:
    """An integration has not been connected yet — surfaced as a setup prompt."""
    return JSONResponse(
        status_code=409, content={"detail": str(exc), "needs_setup": True}
    )


@app.exception_handler(UpstreamError)
async def _upstream_error_handler(_: Any, exc: UpstreamError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.on_event("startup")
async def _startup() -> None:
    cfg = settings()
    store = SettingsStore(cfg.settings_file)
    app.state.store = store
    app.state.cache = TTLCache()
    app.state.member_snapshot_lock = asyncio.Lock()
    app.state.member_snapshot_revision = 0
    app.state.settings_service = SettingsService(store)
    # First run: migrate .env values into the editable settings document so
    # existing deployments keep working, then never read them again.
    app.state.settings_service.bootstrap_from_env(cfg)

    if cfg.mediadeck_mock:
        app.state.emby = MockEmby()
        probe = MockProbe()
    else:
        app.state.emby = LiveEmby(app.state.settings_service.emby_config)
        probe = LiveProbe()
    app.state.pipeline = (
        MockPipeline() if cfg.mediadeck_mock else PipelineReader(cfg.pipeline_snapshot_path)
    )
    app.state.mounts = (
        MockMounts() if cfg.mediadeck_mock else MountsReader(cfg.mounts_snapshot_path)
    )
    app.state.storage = MockStorage() if cfg.mediadeck_mock else StorageManager(cfg)
    app.state.tasks = (
        MockTasks() if cfg.mediadeck_mock else TasksReader(cfg.tasks_snapshot_path)
    )
    app.state.imports = ImportManager(MockExecutor() if cfg.mediadeck_mock else None)

    # ---- intake pipeline observability -----------------------------------
    # Assembled here so the collector's three seams (paths, filesystem, media
    # server) are all injected from one place, and so mock mode gets a fully
    # populated page without touching a real host.
    app.state.intake_store = IntakeStore()
    app.state.intake_paths = IntakePaths.from_mapping(cfg.intake_paths())
    app.state.intake_downloaders = _build_downloaders(cfg)
    if cfg.mediadeck_mock or not cfg.repo_root:
        app.state.updater = MockUpdater()
    else:
        app.state.updater = Updater(cfg.repo_root, cfg.service_name)

    dispatch = app.state.settings_service.dispatch_config()
    # Nodes always come from the settings store -- in mock mode the demo fleet
    # was seeded into it at bootstrap, so the settings page and the scheduler
    # can never disagree about which nodes exist.
    app.state.scheduler = Scheduler(
        app.state.settings_service.nodes(),
        probe,
        policy=dispatch["policy"],
        load_threshold=dispatch["load_threshold"],
    )
    # Node/policy edits in the UI reconfigure this scheduler in place.
    app.state.settings_service.bind_scheduler(app.state.scheduler)

    # Playback interception: the piece that puts the scheduler on the real
    # client path instead of only the /stream test edge.
    async def _member_rate(token: str, device: str = "",
                           cache_scope: str = "direct") -> tuple[int, str]:
        """Caller credential -> (bandwidth cap in bytes/s, anonymised user tag).

        Signed into every node URL, so the node enforces the member's cap and
        can attribute real transfer bytes back to the user. Cached briefly:
        one playback start must not cost two extra round trips every time.

        The device id is part of the cache key, not just the token: one admin
        api_key is shared by every session it can see, so caching by token
        alone would hand the first resolved user's cap to everyone else.
        """
        cache_key = f"rate:{cache_scope}:{user_tag(token)}:{user_tag(device)}"
        cached = app.state.cache.get(cache_key)
        if cached is not None:
            return cached
        result: tuple[int, str] = (0, "")
        try:
            uid = await app.state.emby.user_for_token(token, device)
            if uid:
                member = app.state.members.get(uid)
                kbps = int((member or {}).get("bandwidth_limit_kbps") or 0)
                result = (rate_bytes_per_sec(kbps), user_tag(uid))
        except Exception:  # noqa: BLE001 - fail open: sign uncapped
            result = (0, "")
        # An unresolved caller is cached only briefly: it means the stream is
        # running uncapped and unattributed, and that must self-heal as soon
        # as the session registers rather than persist for a full minute.
        app.state.cache.set(cache_key, result, ttl=60 if result[1] else 10)
        return result

    app.state.playback = PlaybackRouter(
        app.state.emby,
        app.state.scheduler,
        app.state.settings_service.playback_config,
        app.state.settings_service.emby_config,
        rate_resolver=_member_rate,
    )

    # ---- membership, billing, statistics --------------------------------
    # Operational data lives in SQLite rather than the settings JSON: traffic
    # accounting appends continuously and has to answer aggregate questions,
    # which a rewritten-in-full document cannot do safely.
    app.state.db = Database(cfg.data_dir / "mediadeck.db")
    app.state.groups = GroupService(app.state.db)
    app.state.groups.seed_defaults()
    app.state.members = MemberService(app.state.db, app.state.groups)
    app.state.enforcement = EnforcementService(
        app.state.db, app.state.members, app.state.emby)
    app.state.stats = StatsService(
        app.state.db,
        measured_cutover=lambda: bool(
            app.state.settings_service.metering_config().get("cutover")),
    )
    # Bytes measured on the edge. Kept separate from the sampled estimate in
    # usage_daily: conflating a measurement with a guess is what made the old
    # traffic figure unsafe to act on.
    app.state.ledger = TrafficLedger(app.state.db)
    app.state.metering = MeasuredMeteringService(
        app.state.db,
        tag_to_user=_tag_map,
        expected_nodes=lambda: [n.name for n in app.state.settings_service.nodes() if n.enabled],
    )
    app.state.members.bind_metering(
        app.state.metering,
        cutover=lambda: bool(
            app.state.settings_service.metering_config().get("cutover")),
    )
    # Points are a ledger, so the service is a thin wrapper over the database
    # and can be built as soon as it exists. The shop is what spends them.
    app.state.points = PointsService(app.state.db)
    app.state.shop = ShopService(app.state.db, app.state.members,
                                 app.state.points)
    app.state.shop.seed_defaults()
    # Requests need a group to promote into (/prouser) and one to charge the
    # monthly allowance against, so they are built after groups and members.
    app.state.tmdb = TmdbClient(
        lambda: app.state.settings_service.integration_config())
    app.state.requests = RequestService(
        app.state.db, app.state.members, app.state.groups, app.state.tmdb, app.state.emby)
    # Rides along with the sampler: it already holds the only live view of who
    # is playing from where, so detection costs no extra Emby calls.
    app.state.sharing = SharingDetector(app.state.db)
    app.state.access = AccessRules(app.state.db)
    app.state.usage = UsageSampler(
        app.state.db, app.state.members, app.state.emby, app.state.enforcement,
        sharing=app.state.sharing)
    app.state.stats.bind_live_watch(app.state.usage.live_watch)

    image_cfg = app.state.settings_service.image_cache_config()
    app.state.images = ImageCache(
        cfg.data_dir / "imagecache",
        max_bytes=image_cfg["max_bytes"],
        max_age_seconds=image_cfg["max_age_days"] * 86400,
    )

    def _node_for_item(item_id: str) -> str:
        """Which node most recently served this item, for traffic attribution.

        Read from the dispatch log rather than tracked separately: the log is
        already the authoritative record of what the scheduler decided, so a
        second source could only ever disagree with it.
        """
        if not item_id:
            return ""
        for entry in reversed(app.state.playback.recent(60)):
            if entry.get("item_id") == item_id and entry.get("node"):
                return str(entry["node"])
        return ""

    async def usage_loop() -> None:
        """Sample playback, roll billing periods, and keep caches bounded.

        One loop rather than several timers: these steps must not interleave
        (rolling a period while sampling could reset a quota mid-write), and
        serialising them keeps the ordering obvious.

        Every step is individually guarded: a failure in housekeeping must not
        stop metering, because unmetered playback is the one outcome that
        costs money.
        """
        housekeeping_due = 0.0
        prune_due = 0.0
        member_sync_due = 0.0
        while True:
            membership = app.state.settings_service.membership_config()
            await asyncio.sleep(max(5, int(membership["sample_interval_seconds"])))
            with contextlib.suppress(Exception):
                await app.state.usage.tick(node_of=_node_for_item)

            now = time.time()
            if now >= member_sync_due:
                member_sync_due = now + 900
                # Flag members whose Emby account disappeared, clear the flag
                # for accounts that came back, and follow renames. Never
                # deletes and never enrolls: both are operator decisions.
                with contextlib.suppress(Exception):
                    users = await app.state.emby.list_users()
                    await run_usage_io(app.state.members.sync_emby, users, apply=True)
            if now >= housekeeping_due:
                housekeeping_due = now + 600
                with contextlib.suppress(Exception):
                    await run_usage_io(app.state.members.roll_periods)
                # Enforcement only writes to Emby once the operator has
                # switched it on; until then the panel observes and reports.
                if membership["enforcement_enabled"]:
                    with contextlib.suppress(Exception):
                        await app.state.enforcement.reconcile(apply=True)
                with contextlib.suppress(Exception):
                    await run_usage_io(app.state.images.sweep)

            if now >= prune_due:
                prune_due = now + 86400
                with contextlib.suppress(Exception):
                    await run_usage_io(app.state.stats.prune, int(membership["retention_days"]))

    app.state.usage_task = asyncio.create_task(usage_loop())

    async def probe_loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await app.state.scheduler.refresh()
            # Wakes early when nodes change, so a node added in the UI shows
            # its real health at once rather than after up to 15 seconds.
            await app.state.scheduler.wait_for_change(PROBE_INTERVAL)

    app.state.probe_task = asyncio.create_task(probe_loop())

    # ---- telegram bot ----------------------------------------------------
    # Long polling, not a webhook: a webhook needs a public HTTPS route into
    # the panel, while polling reaches out instead and leaves the panel
    # reachable only from where it already was.
    app.state.registration = RegistrationService(
        app.state.db, app.state.groups,
        app.state.settings_service.telegram_config)
    app.state.telegram = TelegramBot(
        app.state.settings_service.telegram_config, app.state.members,
        emby=app.state.emby, stats=app.state.stats, db=app.state.db,
        registration=app.state.registration, points=app.state.points,
        shop=app.state.shop, scheduler=app.state.scheduler,
        requests=app.state.requests, tmdb=app.state.tmdb,
        groups=app.state.groups,
        on_password_changed=lambda: app.state.cache.drop_prefix('panelauth:'),
        on_member_changed=_telegram_member_changed)
    app.state.telegram.start()

    # ---- plugins ---------------------------------------------------------
    # Everything that used to be a bespoke background loop is a plugin now.
    # The daily expiry reminder and ranking post in particular were hard-coded
    # here with their own day-keys; running both mechanisms at once would post
    # twice, so the loop is gone rather than merely disabled.
    app.state.plugin_ctx = PluginContext(
        members=app.state.members,
        emby=app.state.emby,
        telegram=app.state.telegram,
        stats=app.state.stats,
        db=app.state.db,
        settings=app.state.settings_service,
        store=store,
        points=app.state.points,
        shop=app.state.shop,
        scheduler=app.state.scheduler,
        requests=app.state.requests,
        tmdb=app.state.tmdb,
        intake_store=app.state.intake_store,
        intake_paths=app.state.intake_paths,
        intake_fs=FsReader(),
        intake_emby=app.state.emby,
        intake_downloaders=app.state.intake_downloaders,
    )
    # Carry the old loop's settings onto the new cards before the scheduler
    # starts, so an operator who had the ranking post switched on keeps it,
    # at the same hour and to the same chat.
    with contextlib.suppress(Exception):
        migrate_legacy_telegram_jobs(store)
    with contextlib.suppress(Exception):
        ensure_request_library_watch(store)
    app.state.plugins = register_builtin(
        PluginRegistry(store, app.state.db), app.state.plugin_ctx)
    # Handed over after registration rather than at construction: the bot
    # renders its keyboard from which points plugins are switched on, and the
    # registry does not exist until the plugins are registered against it.
    app.state.telegram.bind_plugins(app.state.plugins)
    app.state.plugins.start()

    async def _prime_intake() -> None:
        """Collect once at boot instead of leaving the page empty for a minute.

        Off the startup path: a slow or unreachable media server must delay a
        diagnostic page, never the panel itself.
        """
        with contextlib.suppress(Exception):
            await app.state.plugins.run_now("intake_pipeline", trigger="startup")

    app.state.intake_prime_task = asyncio.create_task(_prime_intake())

    async def _nodes_topic() -> Any:
        config = {n["name"]: n for n in app.state.settings_service.nodes_public()}
        out = []
        for state in app.state.scheduler.snapshot():
            row = dict(config.get(state["name"], {}))
            row.update(state)
            out.append(row)
        return out

    async def _members_topic() -> Any:
        """Compact member pulse for the user page's local updater.

        Not a full list payload: the page refetches the current query when
        this snapshot changes, so filters/selection/scroll survive.
        """
        rows = await asyncio.to_thread(app.state.members.list, None, None, None,
                                       None, 5000)
        return member_ops.live_pulse(rows)

    app.state.events = EventStream({
        "nodes": _nodes_topic,
        "dispatch": lambda: asyncio.to_thread(app.state.scheduler.dispatch_log, 20),
        "overview": lambda: asyncio.to_thread(app.state.stats.overview, 30),
        "latest": lambda: emby_latest(12),
        # Must be the *decorated* payload: the live push and the manual
        # refresh have to agree, otherwise the dashboard shows speeds only
        # when you hit refresh and looks frozen the rest of the time.
        "sessions": _sessions_with_speed,
        "pipeline": lambda: asyncio.to_thread(app.state.pipeline.snapshot),
        "tasks": lambda: asyncio.to_thread(app.state.tasks.snapshot),
        "mounts": lambda: asyncio.to_thread(app.state.mounts.snapshot),
        "playback": lambda: asyncio.to_thread(app.state.playback.recent, 30),
        # Served from the cached snapshot, so pushing it costs nothing beyond
        # the collection that already happened on the plugin timer.
        "intake": lambda: asyncio.to_thread(app.state.intake_store.get),
        "members": _members_topic,
    })


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Join background writers before releasing their shared database."""
    tasks = [getattr(app.state, name, None) for name in
             ("usage_task", "probe_task", "intake_prime_task")]
    tasks = [task for task in tasks if task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # Plugins may send Bot messages and both can write audit rows.
    for name in ("plugins", "telegram"):
        service = getattr(app.state, name, None)
        if service is not None:
            await service.stop()
    db = getattr(app.state, "db", None)
    if db is not None:
        db.close()


STATIC_DIR = FilePath(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/login", include_in_schema=False)
async def login_page(request: Request) -> HTMLResponse:
    if _session_user(request):
        return RedirectResponse("/", status_code=303)
    html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/api/auth/login")
async def auth_login(payload: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    user = await _check_login(username, password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    response = JSONResponse({"ok": True, "user": user})
    _issue_session(response, user)
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if token and hasattr(app.state, "cache"):
        app.state.cache.delete(f"panelsess:{token}")
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/whoami", dependencies=[Depends(_auth)])
async def whoami(user: str = Depends(_auth)) -> dict[str, str]:
    return {"user": user}


@app.get("/", include_in_schema=False)
async def root(request: Request,
               credentials: HTTPBasicCredentials | None = Depends(security)) -> HTMLResponse:  # noqa: B008
    user = None
    if credentials is not None:
        user = await _check_login(credentials.username, credentials.password)
    if not user:
        user = _session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    # Cache-busting: stamp static asset URLs with the deployed commit so a
    # release is visible on the next reload without a forced refresh.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    try:
        ver = str((await asyncio.to_thread(app.state.updater.version)).get("commit") or "")
    except Exception:  # noqa: BLE001 - version stamping must never break the page
        ver = ""
    if ver:
        for asset in ("app.css", "app.js", "intake.js", "nodepool.js", "ops.js",
                      "members.js", "workspace.js", "workspace.css", "hardglass.css",
                      "dialog.js"):
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    return HTMLResponse(html)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/stream", dependencies=[Depends(_auth)])
async def event_stream(request: Request, topics: str = "") -> StreamingResponse:
    """Server-sent events: push changes instead of polling every 30s.

    Polling was wrong in both directions -- a stream starting now stayed
    invisible for up to 30s, while an idle panel hammered Emby forever, and
    the periodic re-render wiped whatever the operator was typing.
    """
    wanted = [t.strip() for t in topics.split(",") if t.strip()]
    return StreamingResponse(
        safe_stream(app.state.events.iterate(wanted)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which would hold
            # every event until the buffer fills -- i.e. no live updates.
            "X-Accel-Buffering": "no",
        },
    )


def _agent_file(name: str) -> FileResponse:
    """Serve a node-agent file from the single copy in the repo."""
    if name not in {"loadprobe.py", "flowmeter.py", "meterd.py"}:
        raise HTTPException(404, "agent not found in this deployment")
    for candidate in (
        FilePath(__file__).resolve().parents[2] / "agent" / name,
        FilePath(__file__).resolve().parents[3] / "agent" / name,
    ):
        if candidate.is_file():
            return FileResponse(candidate, media_type="text/x-python")
    raise HTTPException(404, "agent not found in this deployment")


@app.get("/agent/loadprobe.py", include_in_schema=False)
async def agent_loadprobe() -> FileResponse:
    """Serve the node probe agent so the installer can fetch it from here."""
    return _agent_file("loadprobe.py")


@app.get("/agent/flowmeter.py", include_in_schema=False)
async def agent_flowmeter() -> FileResponse:
    return _agent_file("flowmeter.py")


@app.get("/agent/meterd.py", include_in_schema=False)
async def agent_meterd() -> FileResponse:
    return _agent_file("meterd.py")


# ---- settings --------------------------------------------------------------
@app.get("/api/settings", dependencies=[Depends(_auth)])
async def settings_overview() -> dict[str, Any]:
    """Everything the settings page renders, in one round trip."""
    service = app.state.settings_service
    return {
        "mock_mode": settings().mediadeck_mock,
        "emby": service.emby_public(),
        "dispatch": service.dispatch_config(),
        "playback": service.playback_config(),
        "integration": service.integration_public(),
        "membership": service.membership_config(),
        "image_cache": service.image_cache_config(),
        "nodes": service.nodes_public(),
    }


@app.get("/api/settings/emby", dependencies=[Depends(_auth)])
async def settings_emby_get() -> dict[str, Any]:
    return app.state.settings_service.emby_public()


@app.put("/api/settings/emby", dependencies=[Depends(_auth)])
async def settings_emby_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    saved = app.state.settings_service.save_emby(payload)
    # Cached identities, item paths and views belong to the previous server.
    # Invalidate only after persistence succeeds.
    app.state.cache.clear()
    app.state.playback.invalidate()
    return saved


@app.post("/api/settings/emby/test", dependencies=[Depends(_auth)])
async def settings_emby_test(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:  # noqa: B008
    """Validate a connection before saving it, so setup is not trial and error."""
    if settings().mediadeck_mock:
        return await app.state.emby.system_info()
    url, api_key, timeout, verify = app.state.settings_service.resolve_probe_target(payload)
    return await probe_emby(url, api_key, timeout, verify)


@app.get("/api/settings/dispatch", dependencies=[Depends(_auth)])
async def settings_dispatch_get() -> dict[str, Any]:
    return app.state.settings_service.dispatch_config()


@app.put("/api/settings/dispatch", dependencies=[Depends(_auth)])
async def settings_dispatch_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return app.state.settings_service.save_dispatch(payload)


# ---- streaming nodes -------------------------------------------------------
@app.get("/api/nodes", dependencies=[Depends(_auth)])
async def nodes() -> list[dict[str, Any]]:
    """Live health merged with stored config, so the UI has one source.

    The scheduler knows health; the settings store knows media roots and
    whether a signing key is set. Returning only one of them forces the UI to
    stitch two lists together and get them out of sync.
    """
    config = {n["name"]: n for n in app.state.settings_service.nodes_public()}
    merged = []
    for state in app.state.scheduler.snapshot():
        row = dict(config.get(state["name"], {}))
        row.update(state)
        merged.append(row)
    return merged


@app.post("/api/nodes", dependencies=[Depends(_auth)])
async def create_node(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return app.state.settings_service.add_node(payload)


@app.put("/api/nodes/{name}", dependencies=[Depends(_auth)])
async def update_node(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    try:
        return app.state.settings_service.update_node(name, payload)
    except KeyError:
        raise HTTPException(404, "unknown node") from None


@app.delete("/api/nodes/{name}", dependencies=[Depends(_auth)])
async def delete_node(name: str) -> dict[str, bool]:
    try:
        app.state.settings_service.delete_node(name)
    except KeyError:
        raise HTTPException(404, "unknown node") from None
    return {"deleted": True}


@app.post("/api/nodes/{name}/disable", dependencies=[Depends(_auth)])
async def disable_node(name: str) -> dict[str, bool]:
    if not app.state.scheduler.set_disabled(name, True):
        raise HTTPException(404, "unknown node")
    return {"disabled": True}


@app.post("/api/nodes/{name}/enable", dependencies=[Depends(_auth)])
async def enable_node(name: str) -> dict[str, bool]:
    if not app.state.scheduler.set_disabled(name, False):
        raise HTTPException(404, "unknown node")
    return {"disabled": False}


@app.put("/api/nodes/{name}/pool", dependencies=[Depends(_auth)])
async def update_node_pool(name: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                           user: str = Depends(_auth)) -> dict[str, Any]:
    """Edit a node's dispatch parameters, live.

    Applies through the settings service, which reconfigures the running
    scheduler in place -- an operator pulling a node out of rotation during an
    incident cannot be asked to restart the panel to make it take effect.
    """
    try:
        result = app.state.settings_service.update_node_pool(name, payload)
    except KeyError:
        raise HTTPException(404, "unknown node") from None
    changed = result["changed"]
    if changed:
        detail = ", ".join(f"{k}: {v[0]} -> {v[1]}" for k, v in changed.items())
        app.state.members.audit(user, "node.pool", name, detail[:400])
    return {**result["node"], "changed": changed}


@app.get("/api/nodes/pool", dependencies=[Depends(_auth)])
async def node_pool_overview() -> list[dict[str, Any]]:
    """Configured node parameters joined with live probe state.

    One payload rather than two: the page shows configuration and reality side
    by side, and fetching them separately makes them disagree for a moment on
    every refresh.
    """
    config = {n["name"]: n for n in app.state.settings_service.nodes_public()}
    live = {s["name"]: s for s in app.state.scheduler.snapshot()}
    total = sum(float(n.get("capacity") or 0)
                for n in config.values() if n.get("enabled"))
    out = []
    for name, node in config.items():
        state = live.get(name, {})
        capacity = float(node.get("capacity") or 0)
        out.append({
            **node,
            "ok": state.get("ok", False),
            "available": state.get("available", False),
            "active_streams": state.get("active_streams", 0),
            "egress_mbps": state.get("egress_mbps"),
            "egress_status": state.get('egress_status', 'unavailable'),
            "egress_sampled_at": state.get('egress_sampled_at'),
            "egress_time_basis": state.get('egress_time_basis'),
            "egress_window_seconds": state.get('egress_window_seconds'),
            "last_success_ts": state.get('last_success_ts'),
            "utilisation": state.get("utilisation", 0.0),
            "last_probe_ts": state.get("last_probe_ts", 0),
            "manually_disabled": state.get("manually_disabled", False),
            # Share of the fleet this node is expected to carry. Only enabled
            # nodes count, so a disabled node reads 0 rather than inflating
            # the denominator and making every share look too small.
            "share": (round(capacity / total, 4)
                      if total > 0 and node.get("enabled") else 0.0),
        })
    return out


@app.get("/api/nodes/{name}/history", dependencies=[Depends(_auth)])
async def node_history(name: str, limit: int = 240) -> list[dict[str, Any]]:
    try:
        return app.state.scheduler.history(name, limit)
    except KeyError:
        raise HTTPException(404, "unknown node") from None


@app.get("/api/dispatch/log", dependencies=[Depends(_auth)])
async def dispatch_log(limit: int = 100) -> list[dict[str, Any]]:
    return app.state.scheduler.dispatch_log(limit)


@app.get("/api/dispatch/pick", dependencies=[Depends(_auth)])
async def dispatch_pick(path: str = "") -> dict[str, Any]:
    """Dry-run of the 302 target selection (no redirect issued).

    Accepts a path so an operator can verify affinity: the same path must
    keep resolving to the same node while that node stays healthy.
    """
    chosen = app.state.scheduler.pick(record=False, context=path)
    if not chosen:
        raise HTTPException(503, "no available streaming node")
    return {"node": chosen.node.name, "base_url": chosen.node.base_url,
            "utilisation": round(chosen.utilisation(), 3),
            "policy": app.state.scheduler.policy}


@app.get("/stream/{path:path}")
async def stream_redirect(path: str) -> RedirectResponse:
    """The actual 302 edge: redirect a stream request to the chosen node."""
    chosen = app.state.scheduler.pick(context=path)
    if not chosen:
        raise HTTPException(503, "no available streaming node")
    return RedirectResponse(f"{chosen.node.base_url.rstrip('/')}/{path}", status_code=302)


# ---- pipeline --------------------------------------------------------------
@app.get("/api/pipeline", dependencies=[Depends(_auth)])
async def pipeline() -> dict[str, Any]:
    return await asyncio.to_thread(app.state.pipeline.snapshot)


@app.get("/api/intake", dependencies=[Depends(_auth)])
async def intake() -> dict[str, Any]:
    """Last collected intake snapshot, with its age.

    Deliberately serves the cached snapshot rather than collecting on demand:
    the collection walks thousands of files and tails a large log, and this
    page auto-refreshes.
    """
    return app.state.intake_store.get()


@app.post("/api/intake/refresh", dependencies=[Depends(_auth)])
async def intake_refresh() -> dict[str, Any]:
    """Collect now, for an operator who will not wait for the next tick."""
    result = await app.state.plugins.run_now("intake_pipeline", trigger="manual")
    return {"ok": bool(result.get("ok", True)), "result": result,
            "snapshot": app.state.intake_store.get()}


# ---- self-update -----------------------------------------------------------
@app.get("/api/update/version", dependencies=[Depends(_auth)])
async def update_version() -> dict[str, Any]:
    return await asyncio.to_thread(app.state.updater.version)


@app.get("/api/update/check", dependencies=[Depends(_auth)])
async def update_check() -> dict[str, Any]:
    return await asyncio.to_thread(app.state.updater.check)


@app.post("/api/update/apply", dependencies=[Depends(_auth)])
async def update_apply(target: str | None = Body(None, embed=True)) -> dict[str, Any]:
    result = await asyncio.to_thread(app.state.updater.update, target)
    if not result.get("started"):
        raise HTTPException(409, result.get("error", "update not started"))
    return result


@app.get("/api/mounts", dependencies=[Depends(_auth)])
async def mounts() -> dict[str, Any]:
    return await asyncio.to_thread(app.state.mounts.snapshot)


# ---- storage (rclone remotes + systemd mounts) -----------------------------
@app.get("/api/storage/remotes", dependencies=[Depends(_auth)])
async def storage_list_remotes() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_storage_call, app.state.storage.list_remotes)


@app.post("/api/storage/remotes", dependencies=[Depends(_auth)])
async def storage_add_remote(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return await asyncio.to_thread(
        _storage_call, app.state.storage.add_remote,
        payload.get("name") or "",
        payload.get("type") or "",
        payload.get("options") or {},
    )


@app.delete("/api/storage/remotes/{name}", dependencies=[Depends(_auth)])
async def storage_delete_remote(name: str) -> dict[str, bool]:
    return await asyncio.to_thread(_storage_call, app.state.storage.delete_remote, name)


@app.post("/api/storage/remotes/{name}/test", dependencies=[Depends(_auth)])
async def storage_test_remote(name: str) -> dict[str, Any]:
    return await asyncio.to_thread(_storage_call, app.state.storage.test_remote, name)


@app.get("/api/storage/mounts", dependencies=[Depends(_auth)])
async def storage_list_mounts() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_storage_call, app.state.storage.list_mounts)


@app.post("/api/storage/mounts", dependencies=[Depends(_auth)])
async def storage_create_mount(spec: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return await asyncio.to_thread(_storage_call, app.state.storage.create_mount, spec)


@app.post("/api/storage/mounts/{name}/start", dependencies=[Depends(_auth)])
async def storage_start_mount(name: str) -> dict[str, Any]:
    return await asyncio.to_thread(_storage_call, app.state.storage.start_mount, name)


@app.post("/api/storage/mounts/{name}/stop", dependencies=[Depends(_auth)])
async def storage_stop_mount(name: str) -> dict[str, Any]:
    return await asyncio.to_thread(_storage_call, app.state.storage.stop_mount, name)


@app.delete("/api/storage/mounts/{name}", dependencies=[Depends(_auth)])
async def storage_delete_mount(name: str) -> dict[str, bool]:
    return await asyncio.to_thread(_storage_call, app.state.storage.delete_mount, name)


@app.get("/api/tasks", dependencies=[Depends(_auth)])
async def tasks() -> dict[str, Any]:
    return await asyncio.to_thread(app.state.tasks.snapshot)


# ---- import lanes ----------------------------------------------------------
@app.post("/api/imports", dependencies=[Depends(_auth)])
async def imports_submit(
    kind: str = Body(...),
    source_ref: str = Body(...),
    category: str = Body(""),
) -> dict[str, Any]:
    try:
        job_kind = JobKind(kind)
    except ValueError:
        raise HTTPException(422, f"unknown kind: {kind}") from None
    try:
        job = app.state.imports.submit(job_kind, source_ref, category)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return job.to_dict()


@app.get("/api/imports", dependencies=[Depends(_auth)])
async def imports_list(state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return [j.to_dict() for j in app.state.imports.list(state, limit)]


@app.get("/api/imports/{job_id}", dependencies=[Depends(_auth)])
async def imports_get(job_id: str) -> dict[str, Any]:
    job = app.state.imports.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.to_dict()


@app.post("/api/imports/{job_id}/cancel", dependencies=[Depends(_auth)])
async def imports_cancel(job_id: str) -> dict[str, bool]:
    if not app.state.imports.cancel(job_id):
        raise HTTPException(409, "job not cancellable")
    return {"cancelled": True}


# ---- playback interception -------------------------------------------------
@app.get("/api/settings/playback", dependencies=[Depends(_auth)])
async def settings_playback_get() -> dict[str, Any]:
    return app.state.settings_service.playback_config()


@app.put("/api/settings/playback", dependencies=[Depends(_auth)])
async def settings_playback_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    saved = app.state.settings_service.save_playback(payload)
    # Path mapping changed -> previously resolved paths may be stale.
    app.state.playback.invalidate()
    return saved


@app.get("/api/playback/log", dependencies=[Depends(_auth)])
async def playback_log(limit: int = 100) -> list[dict[str, Any]]:
    return app.state.playback.recent(limit)


@app.get("/api/playback/preview", dependencies=[Depends(_auth)])
async def playback_preview(item_id: str) -> dict[str, Any]:
    """Dry-run one interception so the operator can confirm path mapping.

    A wrong media-root mapping yields 404s on the node that are painful to
    debug from client logs; this shows the resolved target first.
    """
    decision = await app.state.playback.route(
        item_id, f"emby/Videos/{item_id}/stream.mkv", {"Static": "true"}
    )
    return {
        "redirected": decision.redirected,
        "target": decision.target,
        "reason": decision.reason,
        "node": decision.node,
        "pool": decision.pool,
        "media_path": decision.media_path,
        "signed": decision.signed,
    }


# ---- integration / node provisioning ---------------------------------------
@app.get("/api/settings/integration", dependencies=[Depends(_auth)])
async def settings_integration_get() -> dict[str, Any]:
    # Public view: the TMDB key is a credential and never travels back out.
    return app.state.settings_service.integration_public()


@app.put("/api/settings/integration", dependencies=[Depends(_auth)])
async def settings_integration_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    saved = app.state.settings_service.save_integration(payload)
    app.state.playback.invalidate()
    app.state.cache.drop_prefix("rate:")
    return saved


@app.get("/api/integration/frontend", dependencies=[Depends(_auth)])
async def integration_frontend(response: Response, server: str = "caddy",
                               entry: str = "") -> dict[str, Any]:
    """Reverse-proxy rule that puts the panel on the real playback path.

    Answers "how does my existing Emby domain dispatch to nodes": the operator
    keeps one public Emby hostname and only stream requests reach the panel.
    """
    service = app.state.settings_service
    integration = service.integration_config()
    emby_public = integration["emby_public_url"] or service.emby_config()["url"]
    if entry:
        if server not in ENTRY_SERVERS:
            raise HTTPException(400, "external entries support Caddy or nginx templates")
        registered = next((e for e in integration["external_entries"] if e["id"] == entry), None)
        if registered is None:
            raise HTTPException(404, "entry not found")
        # Explicit admin-only export; ordinary settings never return the key.
        response.headers["Cache-Control"] = "private, no-store"
        return {"server": server, "config": friend_config(
            registered, integration["emby_public_url"], service.nodes(), server)}
    panel_public = integration["panel_public_url"] or "http://127.0.0.1:8300"
    return {
        "server": server,
        "config": emby_frontend_snippet(panel_public, emby_public, server,
                                         service.emby_config()["url"]),
    }


@app.post("/api/nodes/{name}/rotate-secret", dependencies=[Depends(_auth)])
async def node_rotate_secret(name: str) -> dict[str, Any]:
    """Issue a new signing key for one node, invalidating its issued links."""
    try:
        return app.state.settings_service.rotate_node_secret(name)
    except KeyError:
        raise HTTPException(404, "unknown node") from None


@app.post("/api/nodes/{name}/rotate-enroll", dependencies=[Depends(_auth)])
async def node_rotate_enroll(name: str) -> dict[str, Any]:
    """Invalidate the current install command; the old one-liner stops working."""
    try:
        app.state.settings_service.rotate_enroll_token(name)
    except KeyError:
        raise HTTPException(404, "unknown node") from None
    return await node_enroll_command(name)


@app.get("/api/nodes/{name}/enroll", dependencies=[Depends(_auth)])
async def node_enroll_command(name: str) -> dict[str, Any]:
    """The single command that turns a bare server into this node.

    Everything the node needs -- Drive identity, media roots, cache, signing
    key -- is already stored against the node, so the operator does not fill
    anything in on the target machine. The installer fetches that config from
    the panel using a one-shot enrollment token.
    """
    service = app.state.settings_service
    node = service.node(name)
    if node is None:
        raise HTTPException(404, "unknown node")
    panel = service.integration_config()["panel_public_url"]
    if not panel:
        raise HTTPException(
            409, "请先在「系统设置 → 接入方式」填写面板对外地址，节点需要用它回连"
        )
    token = service.node_enroll_token(name)
    enrolled = bool(node.first_seen_at)
    return {
        "node": name,
        "command": enroll_command(panel, token),
        "ready": bool(node.pools),
        "enrolled": enrolled,
        "pending": not enrolled,
        "first_seen_at": node.first_seen_at,
        "enrolled_host": node.enrolled_host,
        "warnings": (
            [] if node.pools else ["该节点尚未配置媒体根，安装后无法提供任何文件"]
        ),
    }


@app.get("/api/nodes/{name}/install", dependencies=[Depends(_auth)])
async def node_install_script(name: str) -> dict[str, Any]:
    """Render the installer for review (same content the one-liner fetches)."""
    service = app.state.settings_service
    node = service.node(name)
    if node is None:
        raise HTTPException(404, "unknown node")
    service.node_report_token(node.name)
    node = service.node(name) or node
    panel = service.integration_config()["panel_public_url"] or "http://127.0.0.1:8300"
    return {
        "node": name,
        "signing_enabled": bool(node.sign_secret),
        "script": install_script(node, panel),
    }


# ---- edge traffic ingestion ------------------------------------------------
def _meter_policy_snapshot() -> dict[str, Any]:
    """Authoritative deny set. Empty list is a real snapshot, not 'keep last'.

    Quota-exhausted tags are blocked under cutover. Manual suspended/pending
    and expired members stay blocked so a reset or extra-traffic grant cannot
    reopen them. Nodes apply this only on a successful ingest.
    """
    cutover = bool(app.state.settings_service.metering_config().get("cutover"))
    blocked: list[str] = []
    if cutover:
        for member in app.state.members.list(limit=5000):
            state = str(member.get("state") or "")
            stored = str(member.get("status") or "")
            if stored in ("suspended", "pending") or state in (
                    "exhausted", "suspended", "pending", "expired"):
                blocked.append(user_tag(member["emby_user_id"]))
    return app.state.metering.publish_policy(blocked)


def _tag_map() -> dict[str, str]:
    """Anonymised link tag -> member id.

    Rebuilt from the member list rather than stored: the tag is a pure
    function of the account id (the same function used when signing a link),
    so a stored copy could only ever drift out of step with it.
    """
    out: dict[str, str] = {}
    for member in app.state.members.list(limit=5000):
        uid = member.get("emby_user_id") or ""
        if uid:
            out[user_tag(uid)] = uid
    return out


def _edge_node_or_401(name: str, request: Request) -> Any:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    node = app.state.settings_service.node_by_report_token(name, token)
    if node is None:
        raise HTTPException(401, "invalid node report credential")
    return node


@app.get("/api/edge/{name}/cursors", include_in_schema=False)
def edge_cursors(name: str, request: Request) -> dict[str, Any]:
    """Where this node's logs have been consumed to.

    The panel owns the cursors because it is the party that must not double
    count; a node-side copy would diverge after any restore.
    """
    _edge_node_or_401(name, request)
    rows = app.state.db.query(
        "SELECT path, inode, offset, updated_at FROM edge_cursors WHERE node=?",
        (name,))
    return {"node": name, "cursors": [dict(r) for r in rows]}


@app.post("/api/edge/{name}/report", include_in_schema=False)
def edge_report(name: str, request: Request,
                payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    """Accept one batch of access-log lines from a node.

    Authenticated by the node's own long-lived credential rather than the
    panel admin login: this runs unattended on the node, and giving it an
    operator credential would put a far more powerful secret on every edge.
    """
    _edge_node_or_401(name, request)
    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise HTTPException(422, "lines must be a list")
    if len(lines) > MAX_LINES_PER_INGEST:
        raise HTTPException(413, "batch too large")

    ledger: TrafficLedger = app.state.ledger
    try:
        result = ledger.ingest_batch(name, payload, _tag_map())
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(422, "invalid log cursor or batch; refresh cursors") from None
    return {"ok": True, "node": name, "lines": len(lines),
            "events": result["rows"], "bytes": result["bytes"],
            "unknown_bytes": result["unknown_bytes"]}


# Preserve ingest -> policy publish -> sent/applied ordering across requests,
# now that their blocking database work runs in FastAPI's worker pool.
_measured_report_lock = threading.Lock()


@app.post("/api/edge/{name}/measured", include_in_schema=False)
def edge_measured(name: str, request: Request,
                  payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    """Idempotent measured-flow envelope from meterd."""
    _edge_node_or_401(name, request)
    if str(payload.get("node") or name) != name:
        raise HTTPException(422, "node mismatch")
    if payload.get("unit") not in (None, "", METER_UNIT) and payload.get("unit") != METER_UNIT:
        raise HTTPException(422, "unsupported unit")
    payload["node"] = name
    with _measured_report_lock:
        result = app.state.metering.ingest(payload)
        policy = _meter_policy_snapshot()
        app.state.metering.note_policy_sent(name, int(policy["rev"]))
        applied = payload.get("policy_applied_rev")
        if applied is not None:
            try:
                app.state.metering.note_policy_applied(name, int(applied))
            except (TypeError, ValueError):
                pass
    result["blocked_tags"] = policy["blocked_tags"]
    result["unblock_tags"] = []
    result["policy"] = policy
    result["policy_rev"] = policy["rev"]
    result["policy_sent_rev"] = policy["rev"]
    return result


@app.get("/api/metering", dependencies=[Depends(_auth)])
async def metering_status() -> dict[str, Any]:
    cfg = app.state.settings_service.metering_config()
    totals = await asyncio.to_thread(app.state.metering.totals)
    return {
        "config": cfg,
        "totals": totals,
        "cutover": cfg.get("cutover"),
        "status": cfg.get("status"),
    }


@app.post("/api/metering/cutover", dependencies=[Depends(_auth)])
async def metering_cutover(payload: dict[str, Any] = Body(...),  # noqa: B008
                           user: str = Depends(_auth)) -> dict[str, Any]:
    saved = app.state.settings_service.save_metering(payload)
    app.state.members.audit(user, "metering.cutover", "",
                            f"cutover={saved.get('cutover')} confirmed={saved.get('baseline_confirmed')}")
    return saved


@app.get("/api/edge/status", dependencies=[Depends(_auth)])
def edge_status(days: int = 30) -> dict[str, Any]:
    """Ledger health: coverage, per-node totals and unattributed bytes."""
    days = max(1, min(int(days or 30), 400))
    ledger: TrafficLedger = app.state.ledger
    return {
        **ledger.status(),
        "nodes": ledger.node_totals(days),
        "unattributed": ledger.unattributed(days),
        "days": days,
    }


@app.post("/api/edge/relink", dependencies=[Depends(_auth)])
async def edge_relink() -> dict[str, int]:
    """Attach tags to rows ingested before their member was known."""
    return {"updated": app.state.ledger.relink(_tag_map())}


@app.get("/api/nodes/{name}/report-token", dependencies=[Depends(_auth)])
async def node_report_token(name: str, rotate: bool = False) -> dict[str, Any]:
    """Issue (or rotate) the node's traffic-report credential.

    Returned once to the operator who is installing the reporter; it is never
    included in the node list, which is rendered on every settings page load.
    """
    try:
        service = app.state.settings_service
        value = (service.rotate_report_token(name) if rotate
                 else service.node_report_token(name))
    except KeyError:
        raise HTTPException(404, "unknown node") from None
    return {"node": name, "report_token": value}


@app.post("/api/enroll/{token}/report", include_in_schema=False)
async def enroll_report(token: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:  # noqa: B008
    """Public by design: the install token is the credential.

    The node reports the addresses it actually has, so the operator never has
    to type them. A wrong token is indistinguishable from an expired one.
    """
    try:
        return app.state.settings_service.apply_enroll_report(token, payload or {})
    except KeyError:
        raise HTTPException(404, "invalid or expired enrollment token") from None
    except ConfigError as exc:
        raise HTTPException(422, str(exc)) from None


@app.get("/api/enroll/{token}/script", include_in_schema=False)
async def enroll_script(token: str) -> PlainTextResponse:
    """Unauthenticated by design: the enrollment token *is* the credential.

    A bare server has no panel login, so the one-liner cannot use HTTP Basic.
    The token is per node, unguessable, and only yields that node's installer.
    """
    service = app.state.settings_service
    node = service.node_by_enroll_token(token)
    if node is None:
        raise HTTPException(404, "invalid or expired enrollment token")
    service.node_report_token(node.name)
    node = service.node(node.name) or node
    panel = service.integration_config()["panel_public_url"] or "http://127.0.0.1:8300"
    return PlainTextResponse(install_script(node, panel),
                             media_type="text/x-shellscript")


async def emby_video_stream(item_id: str, rest: str, request: Request) -> RedirectResponse:
    """Emby-compatible stream edge.

    A reverse proxy sends real client playback here, so this endpoint is
    public by necessity -- and therefore must not weaken Emby's own auth.
    The caller's Emby token is verified against Emby before any signed node
    URL is issued; otherwise the panel would become a way to fetch media
    without logging in at all.

    Unverified callers are passed through to Emby rather than rejected, so
    Emby applies its own decision and the fail-open contract still holds.
    """
    query = dict(request.query_params)
    entry = identify_entry(request.headers,
                           app.state.settings_service.integration_config()["external_entries"])
    response_headers = {"Cache-Control": "private, no-store",
                        "Vary": "X-Mediadeck-Entry, X-Mediadeck-Entry-Key"}

    # Access rules run before routing. They decide whether this caller may be
    # handed a signed node URL at all; refusing here rather than after routing
    # means a denied request never causes a node to be selected or a signature
    # to be minted.
    verdict = app.state.access.evaluate(
        user_agent=request.headers.get("user-agent", ""),
        remote_ip=request.client.host if request.client else "",
    )
    if not verdict["allowed"]:
        with contextlib.suppress(Exception):
            app.state.access.record_block(
                username="",
                user_agent=request.headers.get("user-agent", ""),
                remote_ip=request.client.host if request.client else "",
                reason=verdict["reason"], rule_id=verdict["rule_id"],
                item_id=item_id)
        raise HTTPException(403, "access denied by rule", headers=response_headers)

    # Preserve the exact incoming path: the fallback URL must point back at the
    # same Emby endpoint the client actually asked for, not a normalised guess.
    decision = await app.state.playback.route(
        item_id, request.url.path.lstrip("/"), query,
        caller_token=caller_token(request.headers, query),
        caller_device=caller_device(request.headers, query),
        require_auth=True,
        cache_scope=entry.cache_scope if entry else "direct",
        only_node=entry.node if entry and entry.pinned else "",
    )

    # Behind a front-door proxy, a "go to Emby instead" answer must not be a
    # redirect: the proxy matches that URL too, sends it back here, and the
    # client loops forever -- turning fail-open into total failure. Answer 204
    # instead so the proxy serves the origin itself and the client never sees
    # the extra hop. Standalone callers still get the plain redirect.
    if not decision.redirected and request.headers.get("x-mediadeck-proxy"):
        # nginx error_page can intercept 418; keep the existing 204 contract
        # for other front doors. Both serve fallback without a redirect loop.
        fallback_status = 418 if request.headers["x-mediadeck-proxy"] == "nginx" else 204
        return Response(status_code=fallback_status, headers={
            **response_headers,
            "X-Mediadeck-Fallback": decision.reason,
        })

    if not decision.target:
        raise HTTPException(409, "Emby origin not configured", headers=response_headers)
    if decision.redirected and entry:
        decision.target = entry_target(decision.target, decision.node or "", entry)
    return RedirectResponse(decision.target, status_code=302, headers={
        **response_headers,
        "X-Mediadeck-Node": decision.node or "",
        "X-Mediadeck-Decision": decision.reason,
    })


# Real client traffic uses several shapes for this path. Observed in this
# deployment's Emby log: /emby/videos/<id>/original.mkv (lowercase, by far the
# most common), /Videos/<id>/stream (no /emby prefix) and mixed casings.
# Starlette routes are case-sensitive, so registering only one shape means real
# playback silently never reaches the panel and dispatch appears to do nothing.
for _prefix in ("/emby/Videos", "/emby/videos", "/Videos", "/videos"):
    app.api_route(_prefix + "/{item_id}/{rest:path}", methods=["GET", "HEAD"],
                  include_in_schema=False)(
        emby_video_stream
    )


# ---- emby ------------------------------------------------------------------
@app.get("/api/emby/users", dependencies=[Depends(_auth)])
async def emby_users() -> list[dict[str, Any]]:
    return await app.state.emby.list_users()


@app.get("/api/emby/libraries", dependencies=[Depends(_auth)])
async def emby_libraries() -> list[dict[str, Any]]:
    # One item-count query per library makes this the slowest view in the
    # panel; pages auto-refresh, so cache it instead of re-running per render.
    return await app.state.cache.resolve(
        "emby:libraries", app.state.emby.libraries, ttl=120
    )


@app.get("/api/emby/latest", dependencies=[Depends(_auth)])
async def emby_latest(limit: int = 12) -> list[dict[str, Any]]:
    """Recently added titles for the dashboard poster wall.

    Cached for a minute: the dashboard auto-refreshes every 30s, and "what was
    added today" does not change between two consecutive renders. The artwork
    itself is served by the cached-image route, so a warm wall costs Emby
    nothing beyond this one list query.
    """
    limit = max(1, min(limit, 24))
    return await app.state.cache.resolve(
        f"emby:latest:{limit}",
        lambda: app.state.emby.latest_items(limit),
        ttl=60,
    )


async def _sessions_with_speed() -> list[dict[str, Any]]:
    """Active sessions annotated with live bandwidth.

    Shared by the REST endpoint and the SSE topic on purpose: when only the
    REST path decorated the payload, speeds appeared on manual refresh and
    vanished on every live push, which reads as "the number is frozen".

    Only node-measured TCP payload is a live rate. Missing attribution or
    origin traffic remains unknown; media bitrate is not a network measurement.
    """
    # Short TTL: sessions must still feel live, but a 30s auto-refresh plus
    # page switches should not hammer Emby.
    sessions = await app.state.cache.resolve(
        "emby:sessions", app.state.emby.active_sessions, ttl=5
    )
    speed_view = app.state.scheduler.user_speed_view()
    out = []
    for session in sessions:
        s = dict(session)
        tag = user_tag(str(s.get("UserId") or ""))
        sample = speed_view.get(tag) if tag else None
        if sample is not None and sample.get("bps") is not None:
            # Node rates are per user tag, not per Emby session row.
            s["SpeedScope"] = "user"
            s["SpeedBps"] = int(sample["bps"])
            s["SpeedSource"] = sample.get("source") or "node"
            s["SpeedReason"] = None
            s["SpeedCollectedAt"] = sample.get("collected_at")
            s["SpeedCoverage"] = sample.get("coverage")
        elif sample is not None:
            s["SpeedScope"] = "user"
            s["SpeedBps"] = None
            s["SpeedSource"] = "unknown"
            s["SpeedReason"] = sample.get("reason") or "unmeasured"
            s["SpeedCollectedAt"] = sample.get("collected_at")
            s["SpeedCoverage"] = sample.get("coverage")
        else:
            s['SpeedScope'] = 'user'
            s['SpeedBps'] = None
            s['SpeedSource'] = 'unknown'
            s['SpeedReason'] = 'no_measured_sample'
            s['SpeedCollectedAt'] = None
            s['SpeedCoverage'] = 'none'
        s['SpeedTimeBasis'] = sample.get('time_basis') if sample else None
        s['SpeedWindowSeconds'] = sample.get('window_seconds') if sample else None
        s['SpeedNodes'] = sample.get('nodes', []) if sample else []
        s['SpeedAccountSessions'] = sum(1 for other in sessions if other.get('UserId') == s.get('UserId'))
        s["SpeedMBps"] = (
            round(s["SpeedBps"] / 1048576, 1) if s["SpeedBps"] is not None else None)
        out.append(s)
    return out


@app.get("/api/emby/sessions", dependencies=[Depends(_auth)])
async def emby_sessions() -> list[dict[str, Any]]:
    return await _sessions_with_speed()


@app.post("/api/emby/users", dependencies=[Depends(_auth)])
async def emby_create_user(name: str = Body(..., embed=True, min_length=1, max_length=60)) -> dict[str, Any]:
    return await app.state.emby.create_user(name)


@app.post("/api/emby/users/{user_id}/disable", dependencies=[Depends(_auth)])
async def emby_disable_user(user_id: str) -> dict[str, bool]:
    if not await app.state.emby.set_user_disabled(user_id, True):
        raise HTTPException(404, "unknown user")
    return {"disabled": True}


@app.post("/api/emby/users/{user_id}/enable", dependencies=[Depends(_auth)])
async def emby_enable_user(user_id: str) -> dict[str, bool]:
    if not await app.state.emby.set_user_disabled(user_id, False):
        raise HTTPException(404, "unknown user")
    return {"disabled": False}


@app.post("/api/emby/users/{user_id}/password", dependencies=[Depends(_auth)])
async def emby_set_password(user_id: str, new_password: str = Body(..., embed=True, min_length=6)) -> dict[str, bool]:
    if not await app.state.emby.set_user_password(user_id, new_password):
        raise HTTPException(404, "unknown user")
    app.state.cache.drop_prefix("panelauth:")
    return {"ok": True}


@app.post("/api/emby/users/{user_id}/policy", dependencies=[Depends(_auth)])
async def emby_apply_policy(user_id: str, policy: dict[str, Any] = Body(...)) -> dict[str, bool]:  # noqa: B008
    allowed = {"IsDisabled", "EnableRemoteAccess", "SimultaneousStreamLimit",
               "RemoteClientBitrateLimit", "InvalidLoginAttemptCount",
               "IsHidden", "EnableLiveTvAccess", "EnableContentDownloading"}
    patch = {k: v for k, v in policy.items() if k in allowed}
    if not patch:
        raise HTTPException(422, "no allowed policy fields in body")
    if not await app.state.emby.apply_policy(user_id, patch):
        raise HTTPException(404, "unknown user")
    return {"ok": True}


async def _reissue_rate_caps(
    *,
    user_id: str | None = None,
    group_id: str | None = None,
    reason: str = "",
    enforce: bool | None = None,
    kick: bool = True,
    report_failures: bool = False,
) -> dict[str, Any]:
    """Drop cached signatures and stop playback so a new cap is picked up.

    The signed URL carries ``r=`` for up to six hours. Changing the number in
    the panel does nothing until the client asks for a new URL, which is why
    a 15 MB/s save looked ignored: every in-flight link was still ``r=0``.
    """
    app.state.cache.drop_prefix("rate:")
    if enforce is None:
        enforce = bool(
            app.state.settings_service.membership_config().get(
                "enforcement_enabled"))
    uids: set[str] = set()
    if user_id:
        uids.add(str(user_id))
    elif group_id:
        for member in app.state.members.list(group_id=group_id, limit=5000):
            overrides = member.get("overrides") or {}
            if "bandwidth_limit_kbps" in overrides:
                continue
            uid = member.get("emby_user_id")
            if uid:
                uids.add(str(uid))
    errors = []
    if enforce:
        for uid in uids:
            try:
                result = await app.state.enforcement.enforce_now(uid, reason)
                if result.get('ok') is False or result.get('remote_ok') is False:
                    errors.append({'target': uid, 'stage': 'enforce', 'error': '远端策略未确认', 'retryable': True})
            except Exception:  # noqa: BLE001 - preserve committed local edits and report remote uncertainty
                errors.append({'target': uid, 'stage': 'enforce', 'error': '远端策略未确认', 'retryable': True})
    if kick and uids:
        try:
            if report_failures:
                await app.state.enforcement.terminate_users(uids, reason, strict=True)
            else:
                await app.state.enforcement.terminate_users(uids, reason)
        except Exception:  # noqa: BLE001 - the local change has already committed
            errors.append({'target': user_id or group_id or '', 'stage': 'terminate',
                           'error': '旧播放会话终止未确认', 'retryable': True})
    return member_ops.action_result(
        local_ok=True, remote_ok=(not errors) if uids and (enforce or kick) else None,
        errors=errors, error=errors[0]['error'] if errors else '', retryable=bool(errors))


async def _telegram_member_changed(user_id: str, previous_bandwidth: int | None) -> dict[str, Any]:
    try:
        return await _apply_telegram_member_change(user_id, previous_bandwidth)
    finally:
        _invalidate_member_snapshot()


async def _apply_telegram_member_change(user_id: str, previous_bandwidth: int | None) -> dict[str, Any]:
    """Bot's committed entitlement change uses the same policy/rate rules as Web."""
    member = app.state.members.get(user_id)
    if not member:
        return member_ops.action_result(local_ok=True, remote_ok=False,
                                        error='本地成员状态已变化', retryable=True)
    enabled = bool(app.state.settings_service.membership_config().get('enforcement_enabled'))
    if member.get('bandwidth_limit_kbps') != previous_bandwidth:
        return await _reissue_rate_caps(user_id=user_id, reason='Telegram 成员限速已更新',
                                       enforce=enabled, kick=True, report_failures=True)
    if enabled:
        return await app.state.enforcement.enforce_now(user_id, 'Telegram 成员权益已更新')
    return member_ops.action_result(local_ok=True, remote_ok=None)


# ---- user groups -----------------------------------------------------------
@app.get("/api/groups", dependencies=[Depends(_auth)])
async def groups_list() -> list[dict[str, Any]]:
    return await asyncio.to_thread(app.state.groups.list)


@app.post("/api/groups", dependencies=[Depends(_auth)])
async def groups_create(payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    group = app.state.groups.create(payload)
    app.state.members.audit(user, "group.create", group["id"], group["name"])
    return group


@app.put("/api/groups/{group_id}", dependencies=[Depends(_auth)])
async def groups_update(group_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    before = app.state.groups.get(group_id) or {}
    group = app.state.groups.update(group_id, payload)
    app.state.members.audit(user, "group.update", group_id, group["name"])
    if before.get("bandwidth_limit_kbps") != group.get("bandwidth_limit_kbps"):
        await _reissue_rate_caps(
            group_id=group_id, reason="用户组限速已更新")
    return group


@app.delete("/api/groups/{group_id}", dependencies=[Depends(_auth)])
async def groups_delete(group_id: str, user: str = Depends(_auth)) -> dict[str, bool]:
    app.state.groups.delete(group_id)
    app.state.members.audit(user, "group.delete", group_id)
    return {"deleted": True}


# ---- members ---------------------------------------------------------------
def _invalidate_member_snapshot() -> None:
    # Versioning also disarms a read already in flight when a write completes.
    app.state.member_snapshot_revision += 1
    app.state.cache.delete('members:emby')


async def _member_emby_snapshot() -> tuple[dict[str, Any] | None, str | None]:
    """Share a short-lived upstream observation across pages and SSE clients.

    Local member data is never cached. Audited changes invalidate the remote
    snapshot; TTL also observes changes made directly in Emby. A failed refresh
    reports unknown rather than recycling stale presence/policy as fact.
    """
    revision = await asyncio.to_thread(
        app.state.db.one, "SELECT MAX(id) AS id FROM audit_log WHERE "
        "action LIKE 'member.%' OR action LIKE 'group.%' OR action LIKE 'enforce.%' "
        "OR action='telegram.prouser'")
    version = (id(app.state.emby), revision['id'], app.state.member_snapshot_revision)
    async with app.state.member_snapshot_lock:
        cached = app.state.cache.get('members:emby')
        if cached is not None and cached[0] == version:
            return cached[1]
        try:
            users = {u['Id']: u for u in await app.state.emby.list_users()}
            value = (users, None)
        except Exception as exc:  # noqa: BLE001 - a failed upstream is optional
            value = (None, member_ops.redact(exc))
        app.state.cache.set('members:emby', (version, value), ttl=5 if value[1] else 30)
        return value


@app.middleware('http')
async def _invalidate_member_observation(request: Request, call_next):
    # Also invalidate direct Emby writes, including partial remote failures.
    changing = request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.url.path.startswith(
        ('/api/members', '/api/groups', '/api/emby/users', '/api/enforcement'))
    try:
        return await call_next(request)
    finally:
        if changing and hasattr(app.state, 'cache'):
            _invalidate_member_snapshot()


@app.get("/api/members", dependencies=[Depends(_auth)])
async def members_list(status: str | None = None, group_id: str | None = None,
                       role: str | None = None, search: str | None = None,
                       limit: int = 500, register_via: str | None = None,
                       inviter_id: str | None = None,
                       page: int | None = None, page_size: int | None = None,
                       offset: int | None = None, sort: str | None = None,
                       order: str | None = None, tg: str | None = None,
                       expiring: str | None = None,
                       emby_status: str | None = None,
                       sync_status: str | None = None) -> dict[str, Any]:
    """Members plus the Emby accounts that are not enrolled yet.

    Showing both in one payload is deliberate: the operator needs to see who is
    *not* being metered, which is exactly the population that costs money
    silently. unmanaged is computed against every member id, not the current
    page, so pagination cannot mis-label an enrolled account as unmanaged.
    """
    paged = page is not None or offset is not None
    fetch_limit = None if paged else max(1, min(int(limit or 500), 5000))
    members = await asyncio.to_thread(
        app.state.members.list, status=status, group_id=group_id,
        role=role, search=search, limit=fetch_limit,
        register_via=register_via, inviter_id=inviter_id)
    hours = {}
    with contextlib.suppress(Exception):
        hours = await asyncio.to_thread(app.state.stats.hours_this_month)
    balances = {}
    with contextlib.suppress(Exception):
        balances = await asyncio.to_thread(app.state.points.balances)
    edge = {}
    with contextlib.suppress(Exception):
        edge = await asyncio.to_thread(app.state.ledger.summary_for_users)
    for member in members:
        member["watch_hours"] = hours.get(member["emby_user_id"], 0.0)
        member["points"] = balances.get(member["emby_user_id"], 0)
        member["edge"] = edge.get(member["emby_user_id"],
                                  {"bytes_7d": 0, "bytes_30d": 0,
                                   "bytes_total": 0})
    known = await asyncio.to_thread(member_ops.known_member_ids, app.state.members)
    emby_users, unmanaged_error = await _member_emby_snapshot()
    if emby_users:
        for member in members:
            user = emby_users.get(member["emby_user_id"]) or {}
            stamp = user.get("LastActivityDate") or ""
            if stamp:
                member["last_activity"] = str(stamp)
    members = member_ops.attach_observation(
        members, emby_users, emby_error=unmanaged_error)
    members = member_ops.apply_list_filters(
        members, tg=tg, expiring=expiring, emby_status=emby_status,
        sync_status=sync_status)
    members = member_ops.sort_rows(members, sort, order)
    counts = member_ops.counts_for(members)
    total = len(members)
    unmanaged: list[dict[str, Any]] = []
    if emby_users is not None:
        for u in emby_users.values():
            if u["Id"] in known:
                continue
            policy = u.get("Policy") or {}
            unmanaged.append({
                "emby_user_id": u["Id"],
                "username": u.get("Name"),
                "is_admin": bool(policy.get("IsAdministrator")),
                "disabled": bool(policy.get("IsDisabled")),
            })
        if search:
            needle = search.lower()
            unmanaged = [u for u in unmanaged
                         if needle in (u["username"] or "").lower()]
    if not paged:
        truncated = total >= fetch_limit
        return {"members": members, "unmanaged": unmanaged[:500],
                "unmanaged_total": len(unmanaged),
                "unmanaged_error": unmanaged_error, "truncated": truncated,
                "limit": fetch_limit, "total": total, "counts": counts}
    page_size_n = max(1, min(int(page_size or 50), 200))
    if offset is not None:
        off = max(0, int(offset))
        page_n = off // page_size_n + 1
        page_rows = members[off:off + page_size_n]
    else:
        page_rows, page_n, off = member_ops.paginate(
            members, page=int(page or 1), page_size=page_size_n)
    return {
        "members": page_rows, "total": total, "page": page_n,
        "page_size": page_size_n, "offset": off, "counts": counts,
        "unmanaged": unmanaged[:500], "unmanaged_total": len(unmanaged),
        "unmanaged_error": unmanaged_error, "truncated": False,
        "limit": page_size_n,
    }


@app.get("/api/members-activity", dependencies=[Depends(_auth)])
async def members_activity() -> dict[str, Any]:
    """Member id -> last activity, straight from Emby.

    Emby already tracks this per account, so asking it is both cheaper and
    more accurate than inferring "active" from a traffic total -- which is
    exactly the inference that made an idle-looking account indistinguishable
    from an unmeasured one.
    """
    out: dict[str, str] = {}
    try:
        for user in await app.state.emby.list_users():
            stamp = user.get("LastActivityDate") or ""
            if stamp:
                out[str(user.get("Id"))] = str(stamp)
    except Exception as exc:  # noqa: BLE001 - the column is optional
        return {"available": False, "reason": str(exc)[:200], "activity": {}}
    return {"available": True, "activity": out}


@app.post("/api/members/enroll-defaults", dependencies=[Depends(_auth)])
async def members_enroll_defaults(user: str = Depends(_auth)) -> dict[str, Any]:
    """Put every unmanaged, non-admin Emby account into the default group."""
    users = [u for u in await app.state.emby.list_users()
             if not (u.get("Policy") or {}).get("IsAdministrator")]
    enrolled = app.state.members.enroll_defaults(users, actor=user)
    return {"enrolled": enrolled}


@app.get("/api/members/emby-sync", dependencies=[Depends(_auth)])
async def members_emby_sync_preview() -> dict[str, Any]:
    """Which member rows disagree with Emby right now. Read-only."""
    users = await app.state.emby.list_users()
    return app.state.members.sync_emby(users, apply=False)


@app.post("/api/members/emby-sync", dependencies=[Depends(_auth)])
async def members_emby_sync(payload: dict[str, Any] = Body(default={}),  # noqa: B008
                            user: str = Depends(_auth)) -> dict[str, Any]:
    """Flag/unflag members against Emby, optionally enrolling new accounts.

    Never deletes: an orphan keeps its ledger until the operator purges it.
    """
    users = await app.state.emby.list_users()
    return app.state.members.sync_emby(
        users, apply=True, enroll_new=bool(payload.get("enroll_new")), actor=user)


@app.post("/api/members/purge-orphans", dependencies=[Depends(_auth)])
async def members_purge_orphans(payload: dict[str, Any] = Body(...),  # noqa: B008
                                user: str = Depends(_auth)) -> dict[str, Any]:
    """Remove member rows already confirmed missing from Emby.

    The ids must be named explicitly: a blanket "delete all orphans" button is
    exactly how one bad Emby poll turns into mass data loss.
    """
    ids = payload.get("emby_user_ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(422, "need emby_user_ids")
    removed = app.state.members.purge_orphans([str(i) for i in ids], actor=user)
    return {"removed": removed}


@app.post("/api/members/{user_id}/roles", dependencies=[Depends(_auth)])
async def members_roles(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        return app.state.members.set_roles(user_id, payload.get("roles"), actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None


@app.get("/api/members/{user_id}", dependencies=[Depends(_auth)])
async def members_get(user_id: str, days: int = 30) -> dict[str, Any]:
    detail = await asyncio.to_thread(app.state.members.detail, user_id)
    if not detail:
        raise HTTPException(404, "unknown member")
    emby_users, emby_error = await _member_emby_snapshot()
    detail['member'] = member_ops.attach_observation(
        [detail['member']], emby_users, emby_error=emby_error)[0]
    days = max(1, min(int(days or 30), 400))
    stats = app.state.stats.member_detail(user_id, days)
    series = stats.get("series") or []
    usage = {
        "days": days,
        "bytes": sum(int(p.get("bytes") or 0) for p in series),
        "hours": round(sum(float(p.get("hours") or 0) for p in series), 2),
        "plays": sum(int(p.get("plays") or 0) for p in series),
        "series": series,
    }
    plays = list(stats.get("recent_plays") or [])[:20]
    sessions = []
    with contextlib.suppress(Exception):
        sessions = await app.state.emby.sessions_for_user(user_id)
    edge_detail = {}
    with contextlib.suppress(Exception):
        edge_detail = app.state.ledger.member_detail(user_id, days)
    return {
        **detail,
        "edge": edge_detail,
        "points": app.state.points.balance(user_id),
        "points_ledger": app.state.points.ledger(user_id, 20),
        "requests": app.state.requests.for_user(user_id, limit=10),
        "request_remaining": app.state.requests.remaining(user_id),
        "usage": usage,
        "watch": stats.get('watch'),
        "plays": plays,
        "series": series,
        "recent_plays": plays,
        "active_sessions": [{
            "id": s.get("Id"),
            "client": s.get("Client"),
            "device": s.get("DeviceName"),
            "item": (s.get("NowPlayingItem") or {}).get("Name"),
            "paused": bool((s.get("PlayState") or {}).get("IsPaused")),
        } for s in sessions],
    }


@app.put("/api/members/{user_id}/overrides", dependencies=[Depends(_auth)])
async def members_overrides(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                            user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        before = app.state.members.get(user_id) or {}
        member = app.state.members.set_overrides(user_id, payload, actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from None
    rate_changed = (
        before.get("bandwidth_limit_kbps") != member.get("bandwidth_limit_kbps"))
    if rate_changed or app.state.settings_service.membership_config()["enforcement_enabled"]:
        await _reissue_rate_caps(
            user_id=user_id, reason="成员限速已更新",
            enforce=app.state.settings_service.membership_config()[
                "enforcement_enabled"],
            kick=rate_changed)
    return member


@app.put("/api/members/{user_id}", dependencies=[Depends(_auth)])
async def members_upsert(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                         user: str = Depends(_auth)) -> dict[str, Any]:
    username = str(payload.get("username") or "")
    if not username:
        with contextlib.suppress(Exception):
            for u in await app.state.emby.list_users():
                if u["Id"] == user_id:
                    username = u.get("Name") or ""
                    break
    before = app.state.members.get(user_id)
    member = app.state.members.upsert(user_id, username, payload, actor=user)
    rate_changed = bool(
        before and before.get("bandwidth_limit_kbps") != member.get(
            "bandwidth_limit_kbps"))
    if rate_changed:
        await _reissue_rate_caps(user_id=user_id, reason="成员限速已更新")
    elif app.state.settings_service.membership_config()["enforcement_enabled"]:
        remote = await app.state.enforcement.enforce_now(user_id, "member updated")
        return member_ops.merge_action(member, remote)
    return member_ops.merge_action(member, member_ops.action_result(
        local_ok=True, remote_ok=None))


@app.get("/api/members/{user_id}/delete-preview", dependencies=[Depends(_auth)])
async def members_delete_preview(user_id: str, cascade: bool = False) -> dict[str, Any]:
    """Exactly who a delete would remove, before the operator commits to it."""
    try:
        return app.state.members.delete_preview(user_id, cascade=cascade)
    except KeyError:
        raise HTTPException(404, "unknown member") from None


@app.delete("/api/members/{user_id}", dependencies=[Depends(_auth)])
async def members_delete(user_id: str, request: Request, delete_emby: bool = True,
                         cascade: bool = False,
                         user: str = Depends(_auth)) -> dict[str, Any]:
    """Delete a member. Default is that member only.

    Cascade of the direct inviter is a separate explicit action: cascade=true
    requires confirm_ids matching the preview objects exactly. Emby is deleted
    first; a remote failure keeps the local row so the operator can retry.
    """
    payload: dict[str, Any] = {}
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                payload = body
    if "cascade" in payload:
        cascade = bool(payload["cascade"])
    if "delete_emby" in payload:
        delete_emby = bool(payload["delete_emby"])
    try:
        return await member_ops.execute_delete(
            app.state.members, app.state.emby, user_id, actor=user,
            cascade=cascade, delete_emby=delete_emby,
            confirm_ids=payload.get("confirm_ids"))
    except KeyError:
        raise HTTPException(404, "unknown member") from None


@app.get("/api/members/{user_id}/group-preview", dependencies=[Depends(_auth)])
async def members_group_preview(user_id: str, group_id: str) -> dict[str, Any]:
    try:
        return member_ops.group_preview(app.state.members, user_id, group_id)
    except KeyError:
        raise HTTPException(404, "unknown member") from None


@app.post("/api/members/{user_id}/group", dependencies=[Depends(_auth)])
async def members_change_group(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                               user: str = Depends(_auth)) -> dict[str, Any]:
    group_id = str(payload.get("group_id") or "")
    if not group_id:
        raise HTTPException(400, "缺少 group_id")
    member = app.state.members.get(user_id)
    if not member:
        raise HTTPException(404, "unknown member")
    previous_rate = member.get("bandwidth_limit_kbps")
    try:
        member = app.state.members.upsert(
            user_id, member.get("username") or "",
            {"group_id": group_id,
             "expiry_policy": payload.get("expiry_policy") or "keep",
             **({"expires_at": payload["expires_at"]}
                if "expires_at" in payload and payload.get("expiry_policy") == "set"
                else {})},
            actor=user)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from None
    _meter_policy_snapshot()
    if previous_rate != member.get("bandwidth_limit_kbps"):
        await _reissue_rate_caps(user_id=user_id, reason="用户组限速已更新", enforce=False)
    if app.state.settings_service.membership_config()["enforcement_enabled"]:
        remote = await app.state.enforcement.enforce_now(user_id, "group changed")
        return member_ops.merge_action(member, remote)
    return member_ops.merge_action(member, member_ops.action_result(
        local_ok=True, remote_ok=None))


@app.get("/api/members/{user_id}/renew-preview", dependencies=[Depends(_auth)])
async def members_renew_preview(user_id: str, days: int | None = None) -> dict[str, Any]:
    try:
        return member_ops.renew_preview(app.state.members, user_id, days)
    except KeyError:
        raise HTTPException(404, "unknown member") from None


@app.post("/api/members/{user_id}/renew", dependencies=[Depends(_auth)])
async def members_renew(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        member = app.state.members.renew(user_id, payload.get("days"), actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    if app.state.settings_service.membership_config()["enforcement_enabled"]:
        remote = await app.state.enforcement.enforce_now(user_id, "renewed")
        return member_ops.merge_action(member, remote)
    return member_ops.merge_action(member, member_ops.action_result(
        local_ok=True, remote_ok=None))


@app.post("/api/members/{user_id}/retry-remote", dependencies=[Depends(_auth)])
async def members_retry_remote(user_id: str, user: str = Depends(_auth)
                               ) -> dict[str, Any]:
    member = app.state.members.get(user_id)
    if not member:
        raise HTTPException(404, "unknown member")
    action = str(member.get("last_remote_action") or "enforce")
    if action == "delete_emby":
        return await member_ops.execute_delete(
            app.state.members, app.state.emby, user_id, actor=user,
            cascade=False, delete_emby=True, confirm_ids=None)
    remote = await app.state.enforcement.enforce_now(user_id, "retry")
    fresh = app.state.members.get(user_id) or member
    return member_ops.merge_action(fresh, remote)


@app.get("/api/access/rules", dependencies=[Depends(_auth)])
async def access_rules_list() -> list[dict[str, Any]]:
    return app.state.access.list()


@app.post("/api/access/rules", dependencies=[Depends(_auth)])
async def access_rule_add(payload: dict[str, Any] = Body(...),  # noqa: B008
                          user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        created = app.state.access.add(
            str(payload.get("kind") or ""),
            str(payload.get("pattern") or ""),
            str(payload.get("action") or "deny"),
            str(payload.get("note") or ""),
            bool(payload.get("enabled", True)))
    except ValueError as exc:
        # Validation happens at save time, where a person is present to read the
        # error, rather than at match time on the playback path.
        raise HTTPException(400, str(exc)) from None
    app.state.members.audit(user, "access.rule.add", "",
                            f"{created['kind']} {created['action']}")
    return created


@app.delete("/api/access/rules/{rule_id}", dependencies=[Depends(_auth)])
async def access_rule_remove(rule_id: int, user: str = Depends(_auth)) -> dict[str, bool]:
    if not app.state.access.remove(rule_id):
        raise HTTPException(404, "rule not found")
    app.state.members.audit(user, "access.rule.remove", str(rule_id), "")
    return {"removed": True}


@app.post("/api/access/rules/{rule_id}/enabled", dependencies=[Depends(_auth)])
async def access_rule_toggle(rule_id: int, payload: dict[str, Any] = Body(...),  # noqa: B008
                             user: str = Depends(_auth)) -> dict[str, bool]:
    enabled = bool(payload.get("enabled", True))
    if not app.state.access.set_enabled(rule_id, enabled):
        raise HTTPException(404, "rule not found")
    app.state.members.audit(user, "access.rule.toggle", str(rule_id),
                            f"enabled={enabled}")
    return {"enabled": enabled}


@app.get("/api/access/blocks", dependencies=[Depends(_auth)])
async def access_blocks(limit: int = 100) -> list[dict[str, Any]]:
    """Refused requests. A block that leaves no trace is indistinguishable
    from a broken node, and the operator debugs the wrong thing."""
    return app.state.access.blocks(limit)


@app.get("/api/sharing", dependencies=[Depends(_auth)])
async def sharing_findings(limit: int = 50) -> dict[str, Any]:
    """Accounts seen playing from more than one network at once.

    Reported, never acted on: the cost of being wrong is locking out a paying
    member over a VPN reconnect, so the judgement stays with a person.
    """
    return {
        "items": app.state.sharing.recent(limit),
        "status": app.state.sharing.status(),
    }


@app.post("/api/members/bulk", dependencies=[Depends(_auth)])
async def members_bulk(payload: dict[str, Any] = Body(...),  # noqa: B008
                       user: str = Depends(_auth)) -> dict[str, Any]:
    """Apply one action to many members, reporting per-member outcomes.

    Partial success is the normal case: one member may have been deleted in
    another tab while the operator was ticking boxes. Failing the whole batch
    for that would make the operator redo work that already succeeded, so each
    id is attempted independently and the failures are named.

    Deliberately excludes deletion. A mis-click on a checkbox column is easy,
    and a bulk delete is the one action with no way back.
    """
    action = str(payload.get("action") or "").strip()
    ids = payload.get("user_ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "user_ids 不能为空")
    if len(ids) > 500:
        raise HTTPException(400, "单次最多处理 500 个成员")
    # A member is one operation, even if a client submitted duplicate rows.
    ids = list(dict.fromkeys(str(raw) for raw in ids))

    allowed = {"renew", "suspend", "activate", "reset-traffic"}
    if action not in allowed:
        raise HTTPException(400, f"action 必须是 {'/'.join(sorted(allowed))} 之一")

    days = payload.get("days")
    if action == "renew":
        try:
            days = int(days) if days is not None else None
        except (TypeError, ValueError):
            raise HTTPException(400, "days 必须是整数") from None
        if days is not None and not 1 <= days <= 3650:
            raise HTTPException(400, "续期天数必须在 1–3650 之间")

    ok: list[str] = []
    failed: list[dict[str, str]] = []
    for raw in ids:
        user_id = str(raw)
        try:
            if action == "renew":
                app.state.members.renew(user_id, days, actor=user)
            elif action == "suspend":
                app.state.members.set_status(user_id, "suspended", actor=user)
            elif action == "activate":
                app.state.members.set_status(user_id, "active", actor=user)
            else:
                app.state.members.reset_traffic(user_id, actor=user)
            ok.append(user_id)
        except KeyError:
            failed.append({"user_id": user_id, "error": "成员不存在"})
        except Exception as exc:  # noqa: BLE001 - reported per member, not raised
            failed.append({"user_id": user_id, "error": str(exc)})

    app.state.members.audit(
        user, f"member.bulk.{action}", "",
        f"requested={len(ids)} ok={len(ok)} failed={len(failed)}")

    # Enforcement runs once after the batch rather than per member: pushing the
    # same policy change 200 times would hammer Emby for no extra correctness.
    remote_failed: list[dict[str, str]] = []
    if ok and app.state.settings_service.membership_config()["enforcement_enabled"]:
        for user_id in ok:
            remote = await app.state.enforcement.enforce_now(
                user_id, f"bulk {action}")
            if remote.get("ok") is False:
                remote_failed.append({
                    "user_id": user_id,
                    "error": str(remote.get("error") or "enforcement failed"),
                })

    return {"action": action, "requested": len(ids),
            "ok": len(ok), "failed": failed, "remote_failed": remote_failed,
            "ok_flag": not failed and not remote_failed}


@app.post("/api/members/{user_id}/reset-traffic", dependencies=[Depends(_auth)])
async def members_reset_traffic(user_id: str, user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        member = app.state.members.reset_traffic(user_id, actor=user)
        _meter_policy_snapshot()
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    if app.state.settings_service.membership_config()["enforcement_enabled"]:
        remote = await app.state.enforcement.enforce_now(user_id, "traffic reset")
        return member_ops.merge_action(member, remote)
    return member_ops.merge_action(member, member_ops.action_result(
        local_ok=True, remote_ok=None))


@app.post("/api/members/{user_id}/status", dependencies=[Depends(_auth)])
async def members_status(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                         user: str = Depends(_auth)) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    try:
        member = app.state.members.set_status(user_id, status, actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    if app.state.settings_service.membership_config()["enforcement_enabled"]:
        remote = await app.state.enforcement.enforce_now(user_id, f"status={status}")
        if status in ("suspended", "pending"):
            with contextlib.suppress(Exception):
                await app.state.enforcement.terminate_sessions(user_id, "账号已停用")
        return member_ops.merge_action(member, remote)
    return member_ops.merge_action(member, member_ops.action_result(
        local_ok=True, remote_ok=None))


async def _reset_member_password(user_id: str, payload: dict[str, Any],
                                 actor: str) -> dict[str, Any]:
    """Set or randomise a member's Emby password.

    Returned in cleartext exactly once, because the operator has to relay it;
    it is never stored by the panel.
    """
    password = str(payload.get("password") or "") or random_password()
    if len(password) < 6:
        raise HTTPException(422, "密码至少 6 位")
    if not await app.state.emby.set_user_password(user_id, password):
        raise HTTPException(404, "unknown user")
    app.state.cache.drop_prefix("panelauth:")
    app.state.members.audit(actor, "member.password", user_id, "password changed")
    return {"ok": True, "password": password}


@app.post("/api/members/{user_id}/password", dependencies=[Depends(_auth)])
async def members_password(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                           user: str = Depends(_auth)) -> dict[str, Any]:
    return await _reset_member_password(user_id, payload, user)


@app.post("/api/members/{user_id}/actions/reset-password", dependencies=[Depends(_auth)])
async def members_reset_password(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                                 user: str = Depends(_auth)) -> dict[str, Any]:
    return await _reset_member_password(user_id, payload, user)


async def _kick_member(user_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    reason = str(payload.get("reason") or "管理员结束了此次播放")
    stopped = await app.state.enforcement.terminate_sessions(user_id, reason)
    app.state.members.audit(actor, "member.kick", user_id, f"{stopped} session(s)")
    return {"stopped": stopped}


@app.post("/api/members/{user_id}/kick", dependencies=[Depends(_auth)])
async def members_kick(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                       user: str = Depends(_auth)) -> dict[str, Any]:
    return await _kick_member(user_id, payload, user)


@app.post("/api/members/{user_id}/actions/kick", dependencies=[Depends(_auth)])
async def members_action_kick(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                              user: str = Depends(_auth)) -> dict[str, Any]:
    return await _kick_member(user_id, payload, user)


@app.get("/api/members/{user_id}/devices", dependencies=[Depends(_auth)])
async def members_devices(user_id: str) -> list[dict[str, Any]]:
    return app.state.members.devices(user_id)


async def _set_device_blocked(user_id: str, device_id: str, blocked: bool,
                              actor: str) -> dict[str, Any]:
    try:
        row = app.state.members.set_device_blocked(
            user_id, device_id, blocked, actor=actor)
    except KeyError:
        raise HTTPException(404, "unknown device") from None
    return {"ok": True, "blocked": bool(row.get("blocked")), "device": row}


@app.post("/api/members/{user_id}/devices/{device_id}/block", dependencies=[Depends(_auth)])
async def members_block_device(user_id: str, device_id: str,
                               payload: dict[str, Any] = Body(default={}),  # noqa: B008
                               user: str = Depends(_auth)) -> dict[str, Any]:
    # Dedicated /unblock is the canonical clear; this still accepts blocked=false
    # so the existing drawer button keeps working.
    return await _set_device_blocked(
        user_id, device_id, bool(payload.get("blocked", True)), user)


@app.post("/api/members/{user_id}/devices/{device_id}/unblock", dependencies=[Depends(_auth)])
async def members_unblock_device(user_id: str, device_id: str,
                                 user: str = Depends(_auth)) -> dict[str, Any]:
    return await _set_device_blocked(user_id, device_id, False, user)


@app.delete("/api/members/{user_id}/devices/{device_id}", dependencies=[Depends(_auth)])
async def members_forget_device(user_id: str, device_id: str,
                                user: str = Depends(_auth)) -> dict[str, bool]:
    changed = app.state.db.execute(
        "DELETE FROM devices WHERE emby_user_id=? AND device_id=?",
        (user_id, device_id))
    if not changed:
        raise HTTPException(404, "unknown device")
    app.state.members.audit(user, "device.forget", user_id, device_id)
    return {"deleted": True}


# ---- enforcement -----------------------------------------------------------
@app.get("/api/enforcement/preview", dependencies=[Depends(_auth)])
async def enforcement_preview(user_id: str | None = None) -> dict[str, Any]:
    """Dry-run: exactly what would be written to Emby, and to whom."""
    return await app.state.enforcement.reconcile(apply=False, user_id=user_id)


@app.post("/api/enforcement/apply", dependencies=[Depends(_auth)])
async def enforcement_apply(payload: dict[str, Any] = Body(default={}),  # noqa: B008
                            user: str = Depends(_auth)) -> dict[str, Any]:
    result = await app.state.enforcement.reconcile(
        apply=True, user_id=payload.get("user_id"), force=bool(payload.get("force")))
    app.state.members.audit(user, "enforce.manual", payload.get("user_id") or "*",
                            f"applied={result.get('applied')}")
    return result


# ---- statistics ------------------------------------------------------------
@app.get("/api/stats/overview", dependencies=[Depends(_auth)])
async def stats_overview(days: int = 30) -> dict[str, Any]:
    return await asyncio.to_thread(app.state.stats.overview, days)


@app.get("/api/stats/daily", dependencies=[Depends(_auth)])
async def stats_daily(days: int = 30) -> list[dict[str, Any]]:
    return await asyncio.to_thread(app.state.stats.daily_series, days)


@app.get("/api/stats/top-users", dependencies=[Depends(_auth)])
async def stats_top_users(days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    return await asyncio.to_thread(app.state.stats.top_users, days, limit)


@app.get("/api/stats/top-titles", dependencies=[Depends(_auth)])
async def stats_top_titles(days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    return await asyncio.to_thread(app.state.stats.top_titles, days, limit)


@app.get("/api/stats/clients", dependencies=[Depends(_auth)])
async def stats_clients(days: int = 30) -> list[dict[str, Any]]:
    return await asyncio.to_thread(app.state.stats.client_breakdown, days)


@app.get("/api/stats/nodes", dependencies=[Depends(_auth)])
async def stats_nodes(days: int = 30) -> list[dict[str, Any]]:
    return await asyncio.to_thread(app.state.stats.node_breakdown, days)


@app.get("/api/stats/play-methods", dependencies=[Depends(_auth)])
async def stats_play_methods(days: int = 30) -> dict[str, Any]:
    return await asyncio.to_thread(app.state.stats.play_method_breakdown, days)


@app.get("/api/audit", dependencies=[Depends(_auth)])
async def audit_log(limit: int = 100, offset: int = 0, subject: str | None = None,
                    actor: str | None = None, action: str | None = None
                    ) -> dict[str, Any]:
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    items = app.state.members.audit_log(
        limit, offset=offset, subject=subject, actor=actor, action=action)
    total = app.state.members.audit_count(
        subject=subject, actor=actor, action=action)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/usage/status", dependencies=[Depends(_auth)])
async def usage_status() -> dict[str, Any]:
    return {
        **app.state.usage.status(),
        "membership": app.state.settings_service.membership_config(),
    }


# ---- image cache -----------------------------------------------------------
@app.get("/api/settings/image-cache", dependencies=[Depends(_auth)])
async def image_cache_get() -> dict[str, Any]:
    return {**app.state.settings_service.image_cache_config(),
            "stats": app.state.images.stats()}


@app.put("/api/settings/image-cache", dependencies=[Depends(_auth)])
async def image_cache_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    saved = app.state.settings_service.save_image_cache(payload)
    # Rebuild rather than mutate: the budget and age bounds are constructor
    # arguments, and a stale sweeper would keep enforcing the old numbers.
    app.state.images = ImageCache(
        settings().data_dir / "imagecache",
        max_bytes=saved["max_bytes"],
        max_age_seconds=saved["max_age_days"] * 86400,
    )
    return {**saved, "stats": app.state.images.stats()}


@app.post("/api/settings/image-cache/clear", dependencies=[Depends(_auth)])
async def image_cache_clear(user: str = Depends(_auth)) -> dict[str, Any]:
    removed = app.state.images.clear()
    app.state.members.audit(user, "imagecache.clear", "", f"{removed} entries")
    return {"removed": removed}


@app.post("/api/settings/image-cache/sweep", dependencies=[Depends(_auth)])
async def image_cache_sweep() -> dict[str, Any]:
    return app.state.images.sweep(force=True)


@app.get("/api/settings/telegram", dependencies=[Depends(_auth)])
async def telegram_get() -> dict[str, Any]:
    # Never the raw token: it is a bearer credential, and anyone holding it can
    # read every message the bot receives and post as it.
    return {**app.state.settings_service.telegram_public(),
            "membership_schedule": app.state.plugins.card('group_membership'),
            "status": app.state.telegram.status()}


@app.post("/api/settings/telegram", dependencies=[Depends(_auth)])
async def telegram_save(payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    if 'membership_rules' in payload:
        fingerprint = app.state.telegram.membership.fingerprint()
        rules = await app.state.telegram.membership.prepare_rules(payload['membership_rules'])
        if fingerprint != app.state.telegram.membership.fingerprint():
            raise ConflictError('关联规则在核实期间已变化，请重新保存')
        payload = {**payload, 'membership_rules': rules}
    saved = app.state.settings_service.save_telegram(payload, membership_verified='membership_rules' in payload)
    # The audit trail records that the token changed, never what it changed to.
    app.state.members.audit(
        user, "settings.telegram", "",
        f"enabled={saved['enabled']} token_set={saved['bot_token_set']}")
    if 'membership_rules' in payload:
        rules = saved['membership_rules']
        app.state.members.audit(user, 'settings.telegram.membership', rules['generation'],
            f"gate={rules['gate_enabled']} delete={rules['delete_enabled']} "
            + 'chats=' + ','.join(t['chat_id'] for t in rules['targets'] if t['enabled']))
    # Allowlist / identity changes must reinstall command scopes; the poll
    # loop picks this up on the next pass without a process restart.
    app.state.telegram.invalidate_commands()
    return {**saved, "status": app.state.telegram.status()}


@app.post("/api/settings/telegram/verify", dependencies=[Depends(_auth)])
async def telegram_verify() -> dict[str, Any]:
    """Ask Telegram who the bot is. Proves the token without printing it."""
    return await app.state.telegram.verify()


@app.get("/api/telegram/requests", dependencies=[Depends(_auth)])
async def telegram_requests() -> list[dict[str, Any]]:
    """Retired Web queue; review takes place only in the authorized group."""
    raise HTTPException(410, 'Web 关联审批已移除，请在绑定群审核 TG 换绑')


@app.post("/api/telegram/requests/{request_id}/review", dependencies=[Depends(_auth)])
async def telegram_request_review(request_id: int,
                                  payload: dict[str, Any] = Body(default={}),  # noqa: B008
                                  user: str = Depends(_auth)) -> dict[str, Any]:
    raise HTTPException(410, 'Web 关联审批已移除，请在绑定群审核 TG 换绑')


@app.post("/api/telegram/group-audit", dependencies=[Depends(_auth)])
async def telegram_group_audit() -> dict[str, Any]:
    """Which linked members have left the required group.

    Reported, never enforced: leaving a chat is not the same as stopping
    paying, and suspending on that basis is a person's call.
    """
    return await app.state.telegram.audit_group_membership()


@app.post('/api/telegram/membership/verify', dependencies=[Depends(_auth)])
async def telegram_membership_verify(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return await app.state.telegram.membership.prepare_rules(
        {**payload, 'gate_enabled': False, 'delete_enabled': False}, force_verify=True)


@app.get('/api/telegram/membership', dependencies=[Depends(_auth)])
async def telegram_membership_status() -> dict[str, Any]:
    service = app.state.telegram.membership
    return {'scan': service.status(), 'last_event': service._load('last_event'),
            'rules': service.rules(), 'schedule': app.state.plugins.card('group_membership')}


@app.post('/api/telegram/membership/scan', dependencies=[Depends(_auth)])
async def telegram_membership_scan(user: str = Depends(_auth)) -> dict[str, Any]:
    result = app.state.telegram.membership.start_scan('manual')
    app.state.members.audit(user, 'telegram.membership.scan', result['id'],
                            '检测已绑定TG会员；按当前删除开关执行即时复核')
    return result


@app.post("/api/telegram/rankings/send", dependencies=[Depends(_auth)])
async def telegram_send_rankings(payload: dict[str, Any] = Body(default={}),  # noqa: B008
                                 user: str = Depends(_auth)) -> dict[str, bool]:
    # The stored target lives on the ranking plugin's card now, not in the
    # Telegram settings: one place to configure it, so "send one now" and the
    # scheduled post can never disagree about where it goes.
    stored = ""
    with contextlib.suppress(Exception):
        stored = str(app.state.plugins.config("rankings_post").get("chat_id") or "")
    chat = str(payload.get("chat_id") or stored or "").strip()
    if not chat:
        raise HTTPException(400, "未配置排行榜推送目标")
    days = int(payload.get("days") or 1)
    ok = await app.state.telegram.broadcast_rankings(chat, days)
    app.state.members.audit(user, "telegram.rankings", "", f"days={days} ok={ok}")
    return {"sent": ok}


@app.post("/api/members/{user_id}/telegram/unbind", dependencies=[Depends(_auth)])
async def telegram_unbind(user_id: str, user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        return app.state.members.unbind_telegram(user_id, actor=user)
    except KeyError:
        raise HTTPException(404, "member not found") from None


# ---- registration channels --------------------------------------------------
# Three ways in, each requiring something the operator issued: a pre-authorised
# Telegram id, an invite a member spent a slot on, or a card. The panel is
# where all three are minted and audited.

@app.get("/api/redeem", dependencies=[Depends(_auth)])
async def redeem_list(status: str | None = None, batch: str | None = None,
                      limit: int = 500) -> dict[str, Any]:
    return {
        "codes": app.state.registration.list_redeem(
            status=status, batch=batch, limit=limit),
        "stats": app.state.registration.redeem_stats(),
        "batches": app.state.registration.redeem_batches(),
    }


@app.post("/api/redeem/generate", dependencies=[Depends(_auth)])
async def redeem_generate(payload: dict[str, Any] = Body(...),  # noqa: B008
                          user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        # Passed through as given: `or 1` here would turn an explicit count of
        # 0 into a card the operator never asked for. Validation belongs to
        # the service, which rejects it.
        issued = app.state.registration.generate_redeem(
            group_id=str(payload.get("group_id") or ""),
            days=payload.get("days"),
            count=payload.get("count"),
            batch=str(payload.get("batch") or ""),
            note=str(payload.get("note") or ""))
    except (TypeError, ValueError):
        raise HTTPException(400, "天数与数量必须是整数") from None
    # The codes themselves are the product and go back to the operator who
    # asked; the audit trail records only how many, so a log reader cannot
    # harvest unsold cards.
    app.state.members.audit(
        user, "redeem.generate", "",
        f"count={len(issued)} group={payload.get('group_id')} "
        f"days={payload.get('days')} batch={issued[0]['batch'] if issued else ''}")
    return {"codes": issued, "count": len(issued)}


@app.post("/api/redeem/{code_value}/revoke", dependencies=[Depends(_auth)])
async def redeem_revoke(code_value: str,
                        user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        row = app.state.registration.revoke_redeem(code_value)
    except KeyError:
        raise HTTPException(404, "unknown code") from None
    app.state.members.audit(user, "redeem.revoke", "", f"code={row.get('masked') or ''}")
    return row


@app.get("/api/redeem/export.csv", dependencies=[Depends(_auth)])
async def redeem_export(batch: str | None = None,
                        status: str | None = None) -> Response:
    body = app.state.registration.export_redeem_csv(batch=batch, status=status)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        content=body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="redeem-{stamp}.csv"'})


@app.get("/api/registration/grants", dependencies=[Depends(_auth)])
async def grants_list() -> list[dict[str, Any]]:
    return app.state.registration.list_grants()


@app.post("/api/registration/grants", dependencies=[Depends(_auth)])
async def grants_add(payload: dict[str, Any] = Body(...),  # noqa: B008
                     user: str = Depends(_auth)) -> dict[str, Any]:
    tg_id = str(payload.get("tg_user_id") or "").strip()
    row = app.state.registration.grant_admin(tg_id, granted_by=user)
    app.state.members.audit(user, "registration.grant", "", f"tg={tg_id}")
    return row


@app.delete("/api/registration/grants/{tg_user_id}", dependencies=[Depends(_auth)])
async def grants_remove(tg_user_id: str,
                        user: str = Depends(_auth)) -> dict[str, bool]:
    try:
        app.state.registration.revoke_grant(tg_user_id)
    except KeyError:
        raise HTTPException(404, "unknown grant") from None
    app.state.members.audit(user, "registration.grant.revoke", "",
                            f"tg={tg_user_id}")
    return {"revoked": True}


@app.get("/api/members/{user_id}/invites", dependencies=[Depends(_auth)])
async def member_invites(user_id: str) -> dict[str, Any]:
    if not app.state.members.get(user_id):
        raise HTTPException(404, "unknown member")
    return {
        "quota": app.state.registration.invite_quota(user_id),
        "invites": app.state.registration.list_invites(user_id),
        "invitees": [{
            "emby_user_id": m["emby_user_id"],
            "username": m.get("username") or "",
            "state": m.get("state"),
            "register_at": m.get("register_at"),
        } for m in app.state.members.invitees_of(user_id)],
    }


@app.post("/api/members/{user_id}/invite-quota", dependencies=[Depends(_auth)])
async def member_invite_quota(user_id: str,
                              payload: dict[str, Any] = Body(...),  # noqa: B008
                              user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        delta = int(payload.get("delta") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "数量必须是整数") from None
    try:
        after = app.state.registration.adjust_quota(user_id, delta)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    app.state.members.audit(user, "member.invite_quota", user_id,
                            f"delta={delta} now={after}")
    return {"quota": after}


# ---- plugins (automation cards) --------------------------------------------
# One mechanism for every scheduled job. The panel renders a card straight from
# each plugin's declaration, so adding a feature is adding a file rather than
# adding an endpoint and a page.
def _plugin_or_404(plugin_id: str) -> Any:
    registry = getattr(app.state, "plugins", None)
    if registry is None or registry.get(plugin_id) is None:
        raise HTTPException(404, "unknown plugin")
    return registry


@app.get("/api/plugins", dependencies=[Depends(_auth)])
async def plugins_list(category: str | None = None) -> list[dict[str, Any]]:
    return app.state.plugins.cards(category)


@app.get("/api/plugins/{plugin_id}", dependencies=[Depends(_auth)])
async def plugin_get(plugin_id: str) -> dict[str, Any]:
    return _plugin_or_404(plugin_id).card(plugin_id)


@app.post("/api/plugins/{plugin_id}", dependencies=[Depends(_auth)])
async def plugin_save(plugin_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                      user: str = Depends(_auth)) -> dict[str, Any]:
    registry = _plugin_or_404(plugin_id)
    enabled = payload.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(422, "enabled 必须是布尔值")
    config = payload.get("config")
    if config is not None and not isinstance(config, dict):
        raise HTTPException(400, "config 必须是对象")
    try:
        card = registry.save(
            plugin_id,
            enabled,
            config)
    except ValueError as exc:
        # Field.coerce raises with the operator-facing reason already in
        # Chinese; passing it through is the whole point of validating there.
        raise HTTPException(400, str(exc)) from None
    app.state.members.audit(
        user, "plugin.save", plugin_id,
        f"enabled={card['enabled']}")
    return card


@app.post("/api/plugins/{plugin_id}/run", dependencies=[Depends(_auth)])
async def plugin_run(plugin_id: str, user: str = Depends(_auth)) -> dict[str, Any]:
    """Run one plugin now, regardless of its switch or schedule.

    Enabled-state is ignored on purpose: the button exists so a plugin can be
    tried *before* it is switched on. A failing run answers 200 with
    ``ok: false`` rather than an error status -- the failure is the result the
    operator asked for, and it is recorded on the card either way.
    """
    registry = _plugin_or_404(plugin_id)
    result = await registry.run_now(plugin_id, trigger="manual")
    app.state.members.audit(user, "plugin.run", plugin_id,
                            f"ok={result.get('ok')}")
    return {**result, "card": registry.card(plugin_id)}


@app.get("/api/plugins/{plugin_id}/history", dependencies=[Depends(_auth)])
async def plugin_history(plugin_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return _plugin_or_404(plugin_id).history(plugin_id, limit)


# ---- points -----------------------------------------------------------------
# The ledger is the product here: a balance with no rows behind it is a number
# nobody can argue with, which is the wrong property for something members
# earn and spend.
@app.get("/api/points/top", dependencies=[Depends(_auth)])
async def points_top(limit: int = 20) -> list[dict[str, Any]]:
    return app.state.points.top(limit)


@app.get("/api/points/{user_id}", dependencies=[Depends(_auth)])
async def points_get(user_id: str, limit: int = 20) -> dict[str, Any]:
    if app.state.members.get(user_id) is None:
        raise HTTPException(404, "unknown member")
    return {
        "emby_user_id": user_id,
        "balance": app.state.points.balance(user_id),
        "ledger": app.state.points.ledger(user_id, limit),
    }


@app.post("/api/points/{user_id}/adjust", dependencies=[Depends(_auth)])
async def points_adjust(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    """Operator credit or debit. Audited, because it creates value by hand."""
    if app.state.members.get(user_id) is None:
        raise HTTPException(404, "unknown member")
    try:
        delta = int(payload.get("delta"))
    except (TypeError, ValueError):
        raise HTTPException(400, "delta 必须是整数") from None
    reason = str(payload.get("reason") or "").strip()[:100]
    try:
        balance = app.state.points.add(
            user_id, delta, "admin.adjust", ref=reason, actor=user)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    app.state.members.audit(user, "points.adjust", user_id,
                            f"delta={delta} reason={reason}")
    return {"emby_user_id": user_id, "balance": balance,
            "ledger": app.state.points.ledger(user_id, 20)}


# ---- shop -------------------------------------------------------------------
@app.get("/api/shop/items", dependencies=[Depends(_auth)])
async def shop_items(enabled_only: bool = False) -> list[dict[str, Any]]:
    return app.state.shop.items(enabled_only=enabled_only)


@app.post("/api/shop/items", dependencies=[Depends(_auth)])
async def shop_item_create(payload: dict[str, Any] = Body(...),  # noqa: B008
                           user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        return app.state.shop.create(payload, actor=user)
    except ShopError as exc:
        raise HTTPException(400, str(exc)) from None


@app.put("/api/shop/items/{item_id}", dependencies=[Depends(_auth)])
async def shop_item_update(item_id: int, payload: dict[str, Any] = Body(...),  # noqa: B008
                           user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        return app.state.shop.update(item_id, payload, actor=user)
    except KeyError:
        raise HTTPException(404, "unknown item") from None
    except ShopError as exc:
        raise HTTPException(400, str(exc)) from None


@app.delete("/api/shop/items/{item_id}", dependencies=[Depends(_auth)])
async def shop_item_delete(item_id: int,
                           user: str = Depends(_auth)) -> dict[str, Any]:
    if not app.state.shop.delete(item_id, actor=user):
        raise HTTPException(404, "unknown item")
    return {"ok": True}


@app.get("/api/shop/orders", dependencies=[Depends(_auth)])
async def shop_orders(user_id: str | None = None,
                      limit: int = 50) -> list[dict[str, Any]]:
    return app.state.shop.orders(user_id=user_id, limit=limit)


# ---- media requests ---------------------------------------------------------
@app.get("/api/requests", dependencies=[Depends(_auth)])
async def requests_list(status: str | None = None, limit: int = 100, offset: int = 0,
                        media_type: str | None = None, search: str = '') -> list[dict[str, Any]]:
    try:
        return app.state.requests.list(status=parse_status(status), limit=limit, offset=offset,
                                       media_type=media_type, search=search)
    except (RequestError, ConfigError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/requests/stats", dependencies=[Depends(_auth)])
async def requests_stats() -> dict[str, Any]:
    return app.state.requests.stats()


@app.get("/api/requests/{request_id}", dependencies=[Depends(_auth)])
async def requests_detail(request_id: int, offset: int = 0) -> dict[str, Any]:
    row = app.state.requests.get(request_id)
    if not row:
        raise HTTPException(404, '求片记录不存在')
    return dict(row, events=app.state.requests.events(request_id, internal=True, limit=21, offset=offset),
                notifications=app.state.requests.notification_status(request_id))


@app.post("/api/requests/{request_id}/claim", dependencies=[Depends(_auth)])
@app.post("/api/requests/{request_id}/resolve", dependencies=[Depends(_auth)])
async def requests_legacy_disabled(request_id: int) -> dict[str, Any]:
    raise HTTPException(410, '旧接单/二次处理接口已停用，请刷新后直接接受或拒绝待处理请求')


def _request_revision(payload: dict[str, Any]) -> int:
    revision = payload.get('revision')
    if type(revision) is not int or revision < 1:
        raise HTTPException(400, '需要当前工单 revision，请刷新')
    return revision


@app.post("/api/requests/{request_id}/accept", dependencies=[Depends(_auth)])
async def requests_accept(request_id: int, payload: dict[str, Any] = Body(...),  # noqa: B008
                          user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        result = app.state.requests.finish(request_id, user, 'accepted',
                    revision=_request_revision(payload), panel_admin=True)
    except RequestError as exc:
        raise HTTPException(409, str(exc)) from None
    await _notify_request_resolved(result['request'])
    return result


@app.post("/api/requests/{request_id}/reject", dependencies=[Depends(_auth)])
async def requests_reject(request_id: int, payload: dict[str, Any] = Body(...),  # noqa: B008
                          user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        result = app.state.requests.finish(request_id, user, 'rejected', note=str(payload.get('note') or ''),
                    revision=_request_revision(payload), panel_admin=True)
    except RequestError as exc:
        raise HTTPException(409, str(exc)) from None
    await _notify_request_resolved(result['request'])
    return result


@app.post("/api/requests/{request_id}/messages", dependencies=[Depends(_auth)])
async def requests_message(request_id: int, payload: dict[str, Any] = Body(...),  # noqa: B008
                           user: str = Depends(_auth)) -> dict[str, Any]:
    key = str(payload.get('key') or '')
    if not key or len(key) > 100 or type(payload.get('internal', False)) is not bool:
        raise HTTPException(400, '需要消息幂等 key 和布尔 internal')
    try:
        row = app.state.requests.message(request_id, user, str(payload.get('body') or ''),
            staff=True, internal=payload.get('internal', False), revision=_request_revision(payload),
            key=f'web:{request_id}:{user}:{key}', panel_admin=True)
    except RequestError as exc:
        raise HTTPException(409, str(exc)) from None
    await _notify_request_resolved(row)
    return row


@app.post("/api/requests/{request_id}/correct", dependencies=[Depends(_auth)])
async def requests_correct(request_id: int, payload: dict[str, Any] = Body(...),  # noqa: B008
                           user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        row = app.state.requests.correct(request_id, user, str(payload.get('status') or ''),
                str(payload.get('reason') or ''), _request_revision(payload), panel_admin=True)
    except RequestError as exc:
        raise HTTPException(409, str(exc)) from None
    await _notify_request_resolved(row)
    return row


@app.post("/api/requests/{request_id}/refund", dependencies=[Depends(_auth)])
async def requests_refund(request_id: int, payload: dict[str, Any] = Body(...),  # noqa: B008
                          user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        return app.state.requests.refund(request_id, user, str(payload.get('reason') or ''), panel_admin=True)
    except RequestError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/requests/{request_id}/notifications/retry", dependencies=[Depends(_auth)])
async def requests_retry(request_id: int) -> dict[str, Any]:
    if not app.state.requests.get(request_id):
        raise HTTPException(404, '求片记录不存在')
    app.state.requests.retry_notifications(request_id)
    await _notify_request_resolved(app.state.requests.get(request_id))
    return {'ok': True, 'notifications': app.state.requests.notification_status(request_id)}


async def _notify_request_resolved(request: dict[str, Any]) -> None:
    # The durable outbox remains pending when transport fails; no repeated mutation.
    with contextlib.suppress(Exception):
        await app.state.telegram.notify_request_resolved(request)


@app.get("/api/settings/membership", dependencies=[Depends(_auth)])
async def membership_get() -> dict[str, Any]:
    return app.state.settings_service.membership_config()


@app.put("/api/settings/membership", dependencies=[Depends(_auth)])
async def membership_save(payload: dict[str, Any] = Body(...),  # noqa: B008
                          user: str = Depends(_auth)) -> dict[str, Any]:
    saved = app.state.settings_service.save_membership(payload)
    app.state.members.audit(user, "settings.membership", "",
                            f"enforcement={saved['enforcement_enabled']}")
    return saved


@app.get("/emby/Items/{item_id}/Images/{image_type}", include_in_schema=False)
async def cached_image(item_id: str, image_type: str, request: Request) -> Response:
    """Serve Emby artwork from local disk.

    A library grid fires dozens of poster requests and Emby re-derives each one
    every time. Point the front door here and repeat views become disk reads,
    freeing Emby's CPU exactly when the UI needs to feel instant.

    Unknown image types fall through to Emby rather than erroring: this sits on
    a public path, so it must never be the reason artwork disappears.
    """
    origin = app.state.settings_service.emby_config().get("url", "").rstrip("/")
    query = dict(request.query_params)
    passthrough = f"{origin}/emby/Items/{item_id}/Images/{image_type}"
    if query:
        passthrough = f"{passthrough}?{urlencode(query)}"

    cfg = app.state.settings_service.image_cache_config()
    if not cfg["enabled"] or image_type not in ALLOWED_IMAGE_TYPES or not origin:
        return RedirectResponse(passthrough, status_code=302)

    # Item IDs are unique within a server, not across separately configured
    # origins. Keep old cache entries for normal expiry, but never reuse them
    # for a different server's item with the same ID.
    key = _digest(origin + "\n" + app.state.images.key(item_id, image_type, query))

    async def produce() -> tuple[bytes, str, str] | None:
        emby_cfg = app.state.settings_service.emby_config()
        headers = {"X-Emby-Token": emby_cfg.get("api_key", "")}
        async with httpx.AsyncClient(
                timeout=20, verify=bool(emby_cfg.get("verify_ssl", True))) as client:
            r = await client.get(passthrough, headers=headers)
            if r.status_code != 200 or not r.content:
                return None
            return (r.content,
                    r.headers.get("content-type", "image/jpeg"),
                    r.headers.get("etag", ""))

    result = await app.state.images.fetch(key, produce)
    if not result:
        # Cache miss and upstream had nothing: let Emby answer directly so a
        # transient failure never becomes a permanently broken image.
        return RedirectResponse(passthrough, status_code=302)

    data, content_type, etag = result
    # Honour conditional requests: browsers revalidate artwork constantly, and
    # a 304 avoids re-sending megabytes of posters on every page load.
    if etag and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    headers = {
        "Cache-Control": "public, max-age=604800",
        "X-Mediadeck-Cache": "hit",
    }
    if etag:
        headers["ETag"] = etag
    return Response(content=data, media_type=content_type, headers=headers)
