"""Settings service — the configuration surface of the product.

Scope rule, learned the hard way: a setting lives with the thing it describes.

Global settings are the ones that are true for the whole deployment: which
Emby server to drive, how the front door reaches the panel, and how playback
requests are dispatched.

Everything else belongs to a *node*: which Drive identity it mounts, where its
cache is, which media roots it mirrors, and which key signs its URLs.  Two
nodes routinely differ in all four, so a global "strip this prefix" or a single
global signing key cannot express a real fleet -- and silently breaks it: with
one global prefix, a server with both ``/media`` and ``/media-gd3`` roots has
its entire second library 404 on every node.

Secrets are never returned to the browser in cleartext.  The API exposes a
masked preview plus a ``configured`` flag, and accepts the sentinel ``KEEP``
value on save to mean "leave the stored secret untouched".
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from typing import Any

from app.core.config import NodePool, Settings, StreamNode, demo_nodes
from app.core.errors import ConfigError, ConflictError
from app.core.store import SettingsStore
from app.modules.entries import validate_entries
from app.modules.signing import MAX_TTL, MIN_TTL, generate_secret

SECRET_UNCHANGED = "__KEEP__"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MAX_NODES = 64
MAX_POOLS = 12
# Used until the node calls home. `.invalid` is reserved and never resolves.
PENDING_BASE_URL = "https://pending.invalid"
PENDING_PROBE_URL = "http://127.0.0.1:9800/load"

# Playback interception is opt-in: it changes where clients fetch bytes from,
# so it must never switch itself on during an upgrade.
PLAYBACK_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "direct_only": True,
}

# How the operator's existing Emby entrypoint reaches this panel. Needed to
# generate copy-pasteable front-door and node config instead of prose.
INTEGRATION_DEFAULTS: dict[str, Any] = {
    "panel_public_url": "",
    "emby_public_url": "",
    "external_entries": [],
    # Optional. Media requests work without it -- a request then carries the
    # TMDB id and no title, which is still actionable. See modules/tmdb.py.
    "tmdb_api_key": "",
    "tmdb_language": "zh-CN",
}

# Telegram bot. Off until a token is stored: an enabled bot with no token would
# just spin a polling loop that can never succeed.
TELEGRAM_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "bot_token": "",
    # v0.19: the single registration_enabled switch is gone. It could only be
    # open to everyone who found the bot or closed to everyone including the
    # people the operator wanted in. Registration is now gated by *who vouched
    # for you*, one switch per channel:
    #   admin_grant — the operator pre-authorised this Telegram id
    #   invite      — an existing member spent one of their slots
    #   redeem      — a card the operator generated
    # All three default on because none of them is open to the public: every
    # one still requires something the operator issued.
    "allow_admin_grant": True,
    "allow_invite": True,
    "allow_redeem": True,
    "register_days": 30,
    "default_group_id": "",
    # 0 = unlimited. A cap is the only thing standing between a leaked bot link
    # and an unbounded number of accounts.
    "max_users": 0,
    # Chat id or @name a user must belong to before registering. Empty = open.
    "require_group": "",
    # Groups the bot may answer in. Independent of require_group: an empty
    # list is not "every group", it is none. Numeric chat ids preferred;
    # @public handles are accepted for the same operator convenience.
    "group_interaction_chats": [],
    # Shown to a new member alongside their credentials; without it they have
    # a username and password and nowhere to use them.
    "emby_public_url": "",
    # NOTE: the daily ranking post and the expiry reminder used to be
    # configured here (rankings_* / notify_expiring*). They are plugins now
    # ("排行推送" / "到期提醒" on the automation page), and their settings live
    # on those cards. Two switches for one post is how it goes out twice;
    # migrate_legacy_telegram_jobs() moves any stored values across on upgrade.
}

# Artwork cache. Enabled by default because it is pure win: posters never
# change for a given item+size, so re-deriving them on every scroll only burns
# Emby CPU at the moment the UI most needs to feel instant.
IMAGE_CACHE_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "max_gib": 4,
    "max_age_days": 30,
}

# Membership enforcement. Off by default: this server has hundreds of accounts
# created before the panel existed, and switching enforcement on must be a
# deliberate act rather than something an upgrade does silently.
MEMBERSHIP_DEFAULTS: dict[str, Any] = {
    "enforcement_enabled": False,
    "sample_interval_seconds": 15,
    "retention_days": 400,
}

# Measured quota is opt-in. An upgrade must not start blocking accounts
# against a ledger that was never confirmed as the billing baseline.
METERING_DEFAULTS: dict[str, Any] = {
    "cutover": False,
    "baseline_confirmed": False,
    "report_interval_seconds": 15,
}


def mask_secret(value: str) -> str:
    """Show enough to recognise a key, never enough to use it."""
    value = value or ""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


def _entries_revision(entries: list[dict[str, Any]]) -> str:
    # Includes rotations invisible in public rows. A digest cannot authenticate
    # as an entry or recover the independently generated high-entropy keys.
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def _require_http_url(value: str, field: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        raise ConfigError(f"{field} 不能为空")
    if not value.startswith(("http://", "https://")):
        raise ConfigError(f"{field} 必须以 http:// 或 https:// 开头")
    return value


def _abs_path(value: str, field: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value.startswith("/"):
        raise ConfigError(f"{field} 必须是绝对路径（以 / 开头）")
    return value


def parse_group_interaction_chats(value: Any) -> list[str]:
    """Normalise the group-interaction allowlist.

    Empty means no groups, never every group. A string is split on commas
    or whitespace so an operator can paste one chat id. Numeric ids are
    kept as decimal strings; @handles stay as @handles.
    """
    if value is None or value == "":
        return []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        items = [value]
    elif isinstance(value, str):
        items = [part for part in re.split(r"[\s,]+", value) if part]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ConfigError("群交互白名单必须是聊天 ID 列表")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item is None or item is False:
            continue
        if isinstance(item, bool):
            raise ConfigError("群交互白名单条目无效")
        if isinstance(item, (int, float)):
            if int(item) != item:
                raise ConfigError("群交互白名单条目必须是整数聊天 ID 或 @群用户名")
            token = str(int(item))
        else:
            token = str(item).strip()
        if not token:
            continue
        if token.startswith("@"):
            handle = token[1:]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", handle):
                raise ConfigError("群交互白名单 @群用户名格式不正确")
            token = "@" + handle
        elif re.fullmatch(r"-?\d{1,20}", token):
            token = str(int(token))
        else:
            raise ConfigError("群交互白名单条目必须是整数聊天 ID 或 @群用户名")
        if token not in seen:
            seen.add(token)
            out.append(token)
        if len(out) > 64:
            raise ConfigError("群交互白名单最多 64 个群")
    return out


class SettingsService:
    def __init__(self, store: SettingsStore, scheduler: Any = None) -> None:
        self._store = store
        self._scheduler = scheduler
        self._integration_lock = threading.RLock()

    def bind_scheduler(self, scheduler: Any) -> None:
        self._scheduler = scheduler

    # -- bootstrap -----------------------------------------------------------
    def bootstrap_from_env(self, cfg: Settings) -> bool:
        """Seed the store from .env on first run only."""
        if self._store.loaded_from_disk:
            return False
        api_key = (cfg.emby_api_key or "").strip()
        url = (cfg.emby_url or "").strip().rstrip("/")
        self._store.set_section("emby", {
            "enabled": bool(api_key and url),
            "url": url,
            "api_key": api_key,
            "verify_ssl": True,
            "timeout_seconds": 15,
        }, persist=False)
        self._store.set_section("dispatch", {
            "policy": "affinity",
            "load_threshold": 0.8,
        }, persist=False)
        self._store.set_section("playback", dict(PLAYBACK_DEFAULTS), persist=False)
        self._store.set_section("integration", dict(INTEGRATION_DEFAULTS), persist=False)
        self._store.set_section("image_cache", dict(IMAGE_CACHE_DEFAULTS), persist=False)
        self._store.set_section("membership", dict(MEMBERSHIP_DEFAULTS), persist=False)
        self._store.set_section("metering", dict(METERING_DEFAULTS), persist=False)
        seed = cfg.nodes() or (demo_nodes() if cfg.mediadeck_mock else [])
        self._store.set("nodes", [n.model_dump() for n in seed], persist=False)
        self._store.save()
        return True

    # -- emby ----------------------------------------------------------------
    def emby_config(self) -> dict[str, Any]:
        section = self._store.section("emby")
        return {
            "enabled": bool(section.get("enabled")),
            "url": section.get("url") or "",
            "api_key": section.get("api_key") or "",
            "verify_ssl": bool(section.get("verify_ssl", True)),
            "timeout_seconds": section.get("timeout_seconds") or 15,
        }

    def emby_public(self) -> dict[str, Any]:
        cfg = self.emby_config()
        return {
            "enabled": cfg["enabled"],
            "url": cfg["url"],
            "api_key_masked": mask_secret(cfg["api_key"]),
            "api_key_set": bool(cfg["api_key"]),
            "verify_ssl": cfg["verify_ssl"],
            "timeout_seconds": cfg["timeout_seconds"],
        }

    def save_emby(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.emby_config()
        url = _require_http_url(payload.get("url", current["url"]), "Emby 地址")
        api_key = payload.get("api_key", SECRET_UNCHANGED)
        if api_key == SECRET_UNCHANGED or api_key is None:
            api_key = current["api_key"]
        api_key = str(api_key).strip()
        enabled = bool(payload.get("enabled", current["enabled"]))
        if enabled and not api_key:
            raise ConfigError("启用 Emby 集成前必须填写 API Key")
        try:
            timeout = float(payload.get("timeout_seconds", current["timeout_seconds"]))
        except (TypeError, ValueError):
            raise ConfigError("超时时间必须是数字") from None
        if not 1 <= timeout <= 120:
            raise ConfigError("超时时间必须在 1–120 秒之间")
        self._store.set_section("emby", {
            "enabled": enabled, "url": url, "api_key": api_key,
            "verify_ssl": bool(payload.get("verify_ssl", current["verify_ssl"])),
            "timeout_seconds": timeout,
        })
        return self.emby_public()

    def resolve_probe_target(self, payload: dict[str, Any]) -> tuple[str, str, float, bool]:
        current = self.emby_config()
        url = _require_http_url(payload.get("url") or current["url"], "Emby 地址")
        api_key = payload.get("api_key", SECRET_UNCHANGED)
        if api_key == SECRET_UNCHANGED or api_key is None or api_key == "":
            api_key = current["api_key"]
        timeout = float(payload.get("timeout_seconds") or current["timeout_seconds"])
        verify = bool(payload.get("verify_ssl", current["verify_ssl"]))
        return url, str(api_key).strip(), timeout, verify

    # -- dispatch policy -----------------------------------------------------
    def dispatch_config(self) -> dict[str, Any]:
        section = self._store.section("dispatch")
        policy = section.get("policy")
        if policy not in ("affinity", "least-load"):
            policy = "affinity"
        try:
            threshold = float(section.get("load_threshold", 0.8))
        except (TypeError, ValueError):
            threshold = 0.8
        return {"policy": policy, "load_threshold": threshold}

    def save_dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.dispatch_config()
        policy = payload.get("policy", current["policy"])
        if policy not in ("affinity", "least-load"):
            raise ConfigError("调度策略必须是 affinity 或 least-load")
        try:
            threshold = float(payload.get("load_threshold", current["load_threshold"]))
        except (TypeError, ValueError):
            raise ConfigError("负载阈值必须是数字") from None
        if not 0 < threshold <= 100:
            raise ConfigError("负载阈值必须大于 0")
        self._store.set_section("dispatch", {"policy": policy, "load_threshold": threshold})
        if self._scheduler:
            self._scheduler.set_policy(policy, threshold)
        return self.dispatch_config()

    # -- playback ------------------------------------------------------------
    def playback_config(self) -> dict[str, Any]:
        section = self._store.section("playback")
        cfg = dict(PLAYBACK_DEFAULTS)
        for key in cfg:
            if key in section:
                cfg[key] = bool(section[key])
        return cfg

    def save_playback(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.playback_config()
        enabled = bool(payload.get("enabled", current["enabled"]))
        if enabled:
            nodes = self.nodes()
            if not nodes:
                raise ConfigError("启用播放分流前必须至少配置一个推流节点")
            if not any(n.pools for n in nodes):
                raise ConfigError("启用前请先给节点配置媒体根映射（节点详情 → 媒体根）")
        self._store.set_section("playback", {
            "enabled": enabled,
            "direct_only": bool(payload.get("direct_only", current["direct_only"])),
        })
        return self.playback_config()

    # -- integration ---------------------------------------------------------
    def integration_config(self) -> dict[str, Any]:
        """Full config, credential included. For server-side callers only."""
        section = self._store.section("integration")
        cfg = dict(INTEGRATION_DEFAULTS)
        for key in cfg:
            if key in section:
                cfg[key] = (section[key] if key == "external_entries"
                            else str(section[key] or ""))
        return cfg

    def integration_public(self) -> dict[str, Any]:
        """What the browser may see: the key masked, never the key."""
        cfg = self.integration_config()
        return {
            "panel_public_url": cfg["panel_public_url"],
            "emby_public_url": cfg["emby_public_url"],
            "tmdb_language": cfg["tmdb_language"],
            "tmdb_api_key_masked": mask_secret(cfg["tmdb_api_key"]),
            "tmdb_api_key_set": bool(cfg["tmdb_api_key"]),
            "external_entries_revision": _entries_revision(cfg["external_entries"]),
            "external_entries": [
                {"id": e["id"], "origin": e["origin"],
                 "stream_origin": e.get("stream_origin") or "",
                 "node": e.get("node") or "",
                 "proxy_key_set": bool(e.get("proxy_key"))}
                for e in cfg["external_entries"]
            ],
        }

    def save_integration(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Compare and replace indivisibly, including saves of other fields.
        # Runtime settings are owned by one panel process.
        with self._integration_lock:
            return self._save_integration(payload)

    def _save_integration(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.integration_config()
        if ("external_entries" in payload and "external_entries_revision" in payload
                and payload["external_entries_revision"] != _entries_revision(
                    current["external_entries"])):
            raise ConflictError("入口已被其他页面修改，请刷新后重新操作")
        panel = str(payload.get("panel_public_url", current["panel_public_url"]) or "").strip()
        emby = str(payload.get("emby_public_url", current["emby_public_url"]) or "").strip()
        if panel:
            panel = _require_http_url(panel, "面板对外地址")
        if emby:
            emby = _require_http_url(emby, "Emby 对外地址")
        # Same contract as every other stored credential: a form that only
        # changed the language must not blank the key, so an unsent or
        # sentinel value keeps what is already there.
        api_creds = payload.get("tmdb_api_key", SECRET_UNCHANGED)
        if api_creds == SECRET_UNCHANGED or api_creds is None:
            api_creds = current["tmdb_api_key"]
        api_creds = str(api_creds).strip()
        language = str(payload.get("tmdb_language",
                                   current["tmdb_language"]) or "").strip()
        if not language:
            language = str(INTEGRATION_DEFAULTS["tmdb_language"])
        if len(language) > 16:
            raise ConfigError("TMDB 语言代码过长")
        entries = validate_entries(
            payload.get("external_entries", current["external_entries"]),
            current["external_entries"], emby,
            node_names={n.name for n in self.nodes()},
        )
        self._store.set_section("integration", {
            "panel_public_url": panel, "emby_public_url": emby,
            "tmdb_api_key": api_creds, "tmdb_language": language,
            "external_entries": entries,
        })
        return self.integration_public()

    # -- image cache ---------------------------------------------------------
    def image_cache_config(self) -> dict[str, Any]:
        section = self._store.section("image_cache")
        cfg = dict(IMAGE_CACHE_DEFAULTS)
        for key in cfg:
            if key in section:
                cfg[key] = section[key]
        cfg["enabled"] = bool(cfg["enabled"])
        try:
            cfg["max_gib"] = max(1, int(cfg["max_gib"]))
        except (TypeError, ValueError):
            cfg["max_gib"] = IMAGE_CACHE_DEFAULTS["max_gib"]
        try:
            cfg["max_age_days"] = max(1, int(cfg["max_age_days"]))
        except (TypeError, ValueError):
            cfg["max_age_days"] = IMAGE_CACHE_DEFAULTS["max_age_days"]
        cfg["max_bytes"] = cfg["max_gib"] * 1024 ** 3
        return cfg

    def save_image_cache(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.image_cache_config()
        try:
            max_gib = int(payload.get("max_gib", current["max_gib"]))
            max_age = int(payload.get("max_age_days", current["max_age_days"]))
        except (TypeError, ValueError):
            raise ConfigError("缓存容量与保留天数必须是整数") from None
        if not 1 <= max_gib <= 2048:
            raise ConfigError("缓存容量必须在 1–2048 GiB 之间")
        if not 1 <= max_age <= 3650:
            raise ConfigError("缓存保留天数必须在 1–3650 之间")
        self._store.set_section("image_cache", {
            "enabled": bool(payload.get("enabled", current["enabled"])),
            "max_gib": max_gib,
            "max_age_days": max_age,
        })
        return self.image_cache_config()

    # -- membership ----------------------------------------------------------
    def membership_config(self) -> dict[str, Any]:
        section = self._store.section("membership")
        cfg = dict(MEMBERSHIP_DEFAULTS)
        for key in cfg:
            if key in section:
                cfg[key] = section[key]
        cfg["enforcement_enabled"] = bool(cfg["enforcement_enabled"])
        try:
            cfg["sample_interval_seconds"] = max(5, int(cfg["sample_interval_seconds"]))
        except (TypeError, ValueError):
            cfg["sample_interval_seconds"] = 15
        try:
            cfg["retention_days"] = max(30, int(cfg["retention_days"]))
        except (TypeError, ValueError):
            cfg["retention_days"] = 400
        return cfg

    def save_membership(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.membership_config()
        try:
            interval = int(payload.get(
                "sample_interval_seconds", current["sample_interval_seconds"]))
            retention = int(payload.get("retention_days", current["retention_days"]))
        except (TypeError, ValueError):
            raise ConfigError("采样间隔与保留天数必须是整数") from None
        # Below ~5s the sampler spends more time talking to Emby than measuring;
        # above 60s a user can burn a lot of quota between samples.
        if not 5 <= interval <= 60:
            raise ConfigError("采样间隔必须在 5–60 秒之间")
        if not 30 <= retention <= 3650:
            raise ConfigError("数据保留天数必须在 30–3650 之间")
        self._store.set_section("membership", {
            "enforcement_enabled": bool(payload.get(
                "enforcement_enabled", current["enforcement_enabled"])),
            "sample_interval_seconds": interval,
            "retention_days": retention,
        })
        return self.membership_config()

    def metering_config(self) -> dict[str, Any]:
        section = self._store.section("metering")
        cfg = dict(METERING_DEFAULTS)
        for key in cfg:
            if key in section:
                cfg[key] = section[key]
        cfg["cutover"] = bool(cfg["cutover"])
        cfg["baseline_confirmed"] = bool(cfg["baseline_confirmed"])
        try:
            cfg["report_interval_seconds"] = max(5, int(cfg["report_interval_seconds"]))
        except (TypeError, ValueError):
            cfg["report_interval_seconds"] = 15
        # Cutover requires an explicit baseline confirmation. A lone True
        # in a hand-edited document must not start blocking accounts.
        if cfg["cutover"] and not cfg["baseline_confirmed"]:
            cfg["cutover"] = False
        cfg["status"] = (
            "active" if cfg["cutover"] and cfg["baseline_confirmed"]
            else "not_enabled"
        )
        return cfg

    def save_metering(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.metering_config()
        confirm = bool(payload.get("baseline_confirmed", current["baseline_confirmed"]))
        want_cutover = bool(payload.get("cutover", current["cutover"]))
        if want_cutover and not confirm:
            raise ConfigError("启用实测配额前必须确认计费基线（baseline_confirmed）")
        try:
            interval = int(payload.get(
                "report_interval_seconds", current["report_interval_seconds"]))
        except (TypeError, ValueError):
            raise ConfigError("上报间隔必须是整数") from None
        if not 5 <= interval <= 120:
            raise ConfigError("上报间隔必须在 5–120 秒之间")
        self._store.set_section("metering", {
            "cutover": want_cutover,
            "baseline_confirmed": confirm,
            "report_interval_seconds": interval,
        })
        return self.metering_config()

    # -- telegram -------------------------------------------------------------
    def telegram_config(self) -> dict[str, Any]:
        """Full config including the token. Server-side callers only."""
        section = self._store.section("telegram")
        cfg = dict(TELEGRAM_DEFAULTS)
        for key in cfg:
            if key in section:
                cfg[key] = section[key]
        cfg["bot_token"] = str(cfg["bot_token"] or "").strip()
        cfg["enabled"] = bool(cfg["enabled"]) and bool(cfg["bot_token"])
        # Upgrade path: a stored registration_enabled=False meant "nobody may
        # register", and that intent has to survive the switch being split in
        # three. Absent (never configured) means take the defaults, not off --
        # a fresh install has no stored value and should not read as closed.
        if "registration_enabled" in section and not section.get(
                "registration_enabled"):
            for channel in ("allow_admin_grant", "allow_invite", "allow_redeem"):
                if channel not in section:
                    cfg[channel] = False
        # No channel can outlive the bot: with polling stopped nobody could
        # reach it anyway, and reporting a door as open would mislead.
        for channel in ("allow_admin_grant", "allow_invite", "allow_redeem"):
            cfg[channel] = bool(cfg[channel]) and cfg["enabled"]
        cfg["registration_open"] = any(
            cfg[c] for c in ("allow_admin_grant", "allow_invite", "allow_redeem"))
        for key, low, high, fallback in (
            ("register_days", 0, 3650, 30),
        ):
            try:
                cfg[key] = max(low, min(high, int(cfg[key])))
            except (TypeError, ValueError):
                cfg[key] = fallback
        try:
            cfg["max_users"] = max(0, int(cfg["max_users"]))
        except (TypeError, ValueError):
            cfg["max_users"] = 0
        for key in ("default_group_id", "require_group", "emby_public_url"):
            cfg[key] = str(cfg[key] or "").strip()
        try:
            cfg["group_interaction_chats"] = parse_group_interaction_chats(
                cfg.get("group_interaction_chats"))
        except ConfigError:
            cfg["group_interaction_chats"] = []
        return cfg

    def telegram_public(self) -> dict[str, Any]:
        """Same config with the token reduced to a recognisable stub.

        A bot token is a bearer credential: anyone holding it can read every
        message the bot receives and post as it. It must never travel back to a
        browser, so the UI gets a preview and a boolean instead.
        """
        cfg = self.telegram_config()
        public = {k: v for k, v in cfg.items() if k != "bot_token"}
        public["bot_token_masked"] = mask_secret(cfg["bot_token"])
        public["bot_token_set"] = bool(cfg["bot_token"])
        return public

    def save_telegram(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.telegram_config()
        token = payload.get("bot_token", SECRET_UNCHANGED)
        # The sentinel means "the field was not retyped", which is what an edit
        # form sends when the operator only flipped a checkbox.
        if token == SECRET_UNCHANGED or token is None:
            token = current["bot_token"]
        token = str(token).strip()
        if token and ":" not in token:
            raise ConfigError("Bot Token 格式不正确，应形如 <数字ID>:<字符串>")
        enabled = bool(payload.get("enabled", current["enabled"]))
        if enabled and not token:
            raise ConfigError("启用 Telegram 机器人前必须填写 Bot Token")

        try:
            reg_days = int(payload.get("register_days", current["register_days"]))
            max_users = int(payload.get("max_users", current["max_users"]))
        except (TypeError, ValueError):
            raise ConfigError("天数与名额必须是整数") from None

        # 0 means "never expires", which is a real choice; negative is not.
        if not 0 <= reg_days <= 3650:
            raise ConfigError("注册赠送天数必须在 0–3650 之间")
        if max_users < 0:
            raise ConfigError("注册名额不能为负数")

        # Each channel is stored as the operator set it, including while the
        # bot is off: telegram_config() masks them behind `enabled`, so the
        # settings survive a temporary shutdown instead of being silently
        # rewritten to False and lost.
        channels = {}
        for channel in ("allow_admin_grant", "allow_invite", "allow_redeem"):
            stored = self._store.section("telegram").get(
                channel, TELEGRAM_DEFAULTS[channel])
            channels[channel] = bool(payload.get(channel, stored))
        if any(channels.values()) and not enabled:
            raise ConfigError("机器人未启用时无法开放注册通道")

        emby_url = str(payload.get("emby_public_url",
                                   current["emby_public_url"]) or "").strip()
        if emby_url:
            emby_url = _require_http_url(emby_url, "Emby 对外地址")

        if "group_interaction_chats" in payload:
            chats = parse_group_interaction_chats(payload.get("group_interaction_chats"))
        else:
            chats = list(current.get("group_interaction_chats") or [])

        self._store.set_section("telegram", {
            "enabled": enabled,
            "bot_token": token,
            **channels,
            "register_days": reg_days,
            "default_group_id": str(payload.get(
                "default_group_id", current["default_group_id"]) or "").strip(),
            "max_users": max_users,
            "require_group": str(payload.get(
                "require_group", current["require_group"]) or "").strip(),
            "group_interaction_chats": chats,
            "emby_public_url": emby_url,
        })
        return self.telegram_public()

    # -- nodes ---------------------------------------------------------------
    def nodes(self) -> list[StreamNode]:
        raw = self._store.get("nodes", []) or []
        out: list[StreamNode] = []
        for item in raw:
            try:
                out.append(StreamNode(**item))
            except (TypeError, ValueError):
                continue
        return out

    def node(self, name: str) -> StreamNode | None:
        return next((n for n in self.nodes() if n.name == name), None)

    @staticmethod
    def node_public(node: StreamNode) -> dict[str, Any]:
        """Node view for the UI: never leaks the signing key or enroll token."""
        data = node.model_dump()
        secret = data.pop("sign_secret", "") or ""
        rclone_conf = data.pop("rclone_conf", "") or ""
        data.pop("enroll_token", None)
        report_creds = data.pop("report_token", "") or ""
        data["report_token_set"] = bool(report_creds)
        data["sign_secret_set"] = bool(secret)
        data["sign_secret_masked"] = mask_secret(secret)
        # The Drive config contains OAuth tokens; only report whether it is set.
        data["rclone_conf_set"] = bool(rclone_conf.strip())
        data["legacy_config"] = bool(rclone_conf.strip()) and not list(node.mount_ids or [])
        data["enrolled"] = bool(node.first_seen_at)
        data["pending"] = (node.base_url or "").rstrip("/") == PENDING_BASE_URL.rstrip("/")
        return data

    def nodes_public(self) -> list[dict[str, Any]]:
        return [self.node_public(n) for n in self.nodes()]

    def _persist_nodes(self, nodes: list[StreamNode]) -> None:
        self._store.set("nodes", [n.model_dump() for n in nodes])
        if self._scheduler:
            self._scheduler.reconfigure(nodes)

    @staticmethod
    def _validate_pools(raw: Any, existing: list[NodePool]) -> list[NodePool]:
        if raw is None:
            return existing
        if not isinstance(raw, list):
            raise ConfigError("媒体根必须是列表")
        if len(raw) > MAX_POOLS:
            raise ConfigError(f"媒体根数量不能超过 {MAX_POOLS}")
        out: list[NodePool] = []
        seen_prefix: set[str] = set()
        seen_name: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ConfigError("媒体根格式错误")
            name = str(item.get("name") or "").strip()
            if not NAME_RE.match(name):
                raise ConfigError("媒体根名称只能包含字母、数字、点、下划线和连字符")
            if name in seen_name:
                raise ConfigError(f"媒体根名称重复: {name}")
            emby_prefix = _abs_path(str(item.get("emby_prefix") or ""), "Emby 路径前缀")
            if emby_prefix in seen_prefix:
                raise ConfigError(f"Emby 路径前缀重复: {emby_prefix}")
            url_prefix = str(item.get("url_prefix") or "").strip().rstrip("/")
            if not url_prefix.startswith("/"):
                raise ConfigError("节点 URL 前缀必须以 / 开头")
            node_path = str(item.get("node_path") or "").strip().rstrip("/")
            if node_path and not node_path.startswith("/"):
                raise ConfigError("节点本地路径必须是绝对路径")
            seen_name.add(name)
            seen_prefix.add(emby_prefix)
            out.append(NodePool(
                name=name, emby_prefix=emby_prefix, url_prefix=url_prefix,
                node_path=node_path,
                rclone_remote=str(item.get("rclone_remote") or "").strip(),
            ))
        return out

    def _validate_node(self, payload: dict[str, Any],
                       existing: StreamNode | None = None) -> StreamNode:
        base = existing.model_dump() if existing else {}
        name = str(payload.get("name", base.get("name", ""))).strip()
        if not NAME_RE.match(name):
            raise ConfigError("节点名称只能包含字母、数字、点、下划线和连字符（1–40 字符）")
        # A newly named node does not yet know its public address; the installer
        # reports it. Empty URLs become a reserved placeholder rather than a
        # validation error, so the operator is not asked to invent them.
        raw_base = payload.get("base_url", base.get("base_url", ""))
        raw_probe = payload.get("probe_url", base.get("probe_url", ""))
        if not str(raw_base or "").strip():
            raw_base = PENDING_BASE_URL
        if not str(raw_probe or "").strip():
            raw_probe = PENDING_PROBE_URL
        base_url = _require_http_url(raw_base, "节点地址")
        probe_url = _require_http_url(raw_probe, "探针地址")
        try:
            capacity = float(payload.get("capacity", base.get("capacity", 100)))
        except (TypeError, ValueError):
            raise ConfigError("并发容量必须是数字") from None
        if not 1 <= capacity <= 100000:
            raise ConfigError("并发容量必须在 1–100000 之间")

        pools = self._validate_pools(
            payload.get("pools"),
            [NodePool(**p) for p in (base.get("pools") or [])],
        )

        secret = payload.get("sign_secret", SECRET_UNCHANGED)
        if secret == SECRET_UNCHANGED or secret is None:
            secret = base.get("sign_secret", "")
        secret = str(secret).strip()

        try:
            ttl = int(payload.get("sign_ttl_seconds", base.get("sign_ttl_seconds", 21600)))
        except (TypeError, ValueError):
            raise ConfigError("链接有效期必须是数字") from None
        if not MIN_TTL <= ttl <= MAX_TTL:
            raise ConfigError(f"链接有效期必须在 {MIN_TTL}–{MAX_TTL} 秒之间")

        return StreamNode(
            name=name, base_url=base_url, probe_url=probe_url, capacity=capacity,
            enabled=bool(payload.get("enabled", base.get("enabled", True))),
            pools=pools,
            sign_secret=secret,
            sign_ttl_seconds=ttl,
            sign_arg_digest=str(payload.get(
                "sign_arg_digest", base.get("sign_arg_digest", "md5")) or "md5").strip(),
            sign_arg_expires=str(payload.get(
                "sign_arg_expires", base.get("sign_arg_expires", "expires")) or "expires").strip(),
            cache_dir=str(payload.get(
                "cache_dir", base.get("cache_dir", "/var/cache/mediadeck"))).strip(),
            cache_size=str(payload.get("cache_size", base.get("cache_size", "500G"))).strip(),
            # Drive identity travels with the node: without it the installer
            # would still need `rclone config` run by hand on the target,
            # which is exactly the manual step one-command enrollment removes.
            rclone_conf=str(payload.get(
                "rclone_conf", base.get("rclone_conf", "")) or ""),
            enroll_token=str(base.get("enroll_token", "")),
            report_token=str(base.get("report_token", "")),
            mount_ids=[str(x) for x in (
                payload.get("mount_ids", base.get("mount_ids") or []) or [])
                if str(x).strip()],
            first_seen_at=base.get("first_seen_at"),
            enrolled_host=str(payload.get(
                "enrolled_host", base.get("enrolled_host", "")) or "")[:120],
        )

    def add_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        nodes = self.nodes()
        if len(nodes) >= MAX_NODES:
            raise ConfigError(f"节点数量已达上限（{MAX_NODES}）")
        node = self._validate_node(payload)
        if any(n.name == node.name for n in nodes):
            raise ConfigError(f"节点名称已存在: {node.name}")
        # A node with no signing key would hand out permanent public links;
        # generate one up front so the installer can bake it in.
        if not node.sign_secret:
            node.sign_secret = generate_secret()
        node.enroll_token = secrets.token_urlsafe(24)
        nodes.append(node)
        self._persist_nodes(nodes)
        return self.node_public(node)

    def update_node(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        updated = self._validate_node(payload, existing=nodes[index])
        if updated.name != name and any(n.name == updated.name for n in nodes):
            raise ConfigError(f"节点名称已存在: {updated.name}")
        nodes[index] = updated
        self._persist_nodes(nodes)
        return self.node_public(updated)

    def delete_node(self, name: str) -> bool:
        nodes = self.nodes()
        remaining = [n for n in nodes if n.name != name]
        if len(remaining) == len(nodes):
            raise KeyError(name)
        self._persist_nodes(remaining)
        return True

    def update_node_pool(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Edit only the dispatch-relevant fields of one node.

        Deliberately narrower than :meth:`update_node`, which rebuilds the
        whole node from a payload and therefore silently resets anything the
        caller omitted. A pool editor that sends four fields must not be able
        to blank a node's media roots or signing key, so this touches exactly
        the four and leaves the rest of the record alone.

        Returns (public node, changed fields) so the caller can audit what
        actually changed rather than what was submitted.
        """
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        node = nodes[index]
        before = node.model_dump()
        changed: dict[str, Any] = {}

        if "enabled" in payload:
            value = bool(payload["enabled"])
            if value != node.enabled:
                changed["enabled"] = [node.enabled, value]
            node.enabled = value

        for field, label, low, high in (
            ("capacity", "并发容量", 1, 100000),
            ("bandwidth_mbps", "带宽上限", 0, 1000000),
        ):
            if field not in payload:
                continue
            try:
                value = float(payload[field])
            except (TypeError, ValueError):
                raise ConfigError(f"{label}必须是数字") from None
            if not low <= value <= high:
                raise ConfigError(f"{label}必须在 {low}–{high} 之间")
            if value != float(getattr(node, field)):
                changed[field] = [getattr(node, field), value]
            setattr(node, field, value)

        if "weight" in payload:
            # Weight is presented in the UI as a node's share of the fleet.
            # It is stored as capacity because that is what the scheduler
            # actually divides by -- keeping a second number that only the UI
            # understands would let the two disagree.
            try:
                value = float(payload["weight"])
            except (TypeError, ValueError):
                raise ConfigError("权重必须是数字") from None
            if not 1 <= value <= 100000:
                raise ConfigError("权重必须在 1–100000 之间")
            if value != float(node.capacity):
                changed["capacity"] = [node.capacity, value]
            node.capacity = value

        nodes[index] = node
        self._persist_nodes(nodes)
        after = node.model_dump()
        assert before.keys() == after.keys()
        return {"node": self.node_public(node), "changed": changed}

    # -- edge traffic reporting ----------------------------------------------
    def node_by_report_token(self, name: str, token: str) -> StreamNode | None:
        """Match a node by name *and* credential.

        Compared with ``compare_digest`` and only against the named node, so a
        valid credential for one node cannot be used to post another's
        traffic.
        """
        token = (token or "").strip()
        if not token:
            return None
        node = self.node(name)
        if node is None or not node.report_token:
            return None
        if not secrets.compare_digest(node.report_token, token):
            return None
        return node

    def node_report_token(self, name: str) -> str:
        """Return (creating if needed) the node's traffic-report credential."""
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        if not nodes[index].report_token:
            nodes[index].report_token = secrets.token_urlsafe(24)
            self._persist_nodes(nodes)
        return nodes[index].report_token

    def rotate_report_token(self, name: str) -> str:
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        nodes[index].report_token = secrets.token_urlsafe(24)
        self._persist_nodes(nodes)
        return nodes[index].report_token

    def rotate_node_secret(self, name: str) -> dict[str, Any]:
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        nodes[index].sign_secret = generate_secret()
        self._persist_nodes(nodes)
        return self.node_public(nodes[index])

    # -- enrollment ----------------------------------------------------------
    def node_by_enroll_token(self, token: str) -> StreamNode | None:
        token = (token or "").strip()
        if not token:
            return None
        for node in self.nodes():
            if node.enroll_token and secrets.compare_digest(node.enroll_token, token):
                return node
        return None

    def node_enroll_token(self, name: str) -> str:
        """Return (creating if needed) the one-shot install token for a node."""
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        if not nodes[index].enroll_token:
            nodes[index].enroll_token = secrets.token_urlsafe(24)
            self._persist_nodes(nodes)
        return nodes[index].enroll_token

    def rotate_enroll_token(self, name: str) -> dict[str, Any]:
        """Invalidate the current install command and issue a new token."""
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        nodes[index].enroll_token = secrets.token_urlsafe(24)
        self._persist_nodes(nodes)
        return self.node_public(nodes[index])

    def apply_enroll_report(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        """A node calling home after install: record its addresses and mark enrolled."""
        import time
        nodes = self.nodes()
        index = next(
            (i for i, n in enumerate(nodes)
             if n.enroll_token and secrets.compare_digest(n.enroll_token, token)),
            None,
        )
        if index is None:
            raise KeyError(token)
        node = nodes[index]
        patch: dict[str, Any] = {}
        if payload.get("base_url"):
            patch["base_url"] = payload["base_url"]
        if payload.get("probe_url"):
            patch["probe_url"] = payload["probe_url"]
        if payload.get("capacity") is not None:
            patch["capacity"] = payload["capacity"]
        host = str(payload.get("host") or payload.get("enrolled_host") or "")[:120]
        if host:
            patch["enrolled_host"] = host
        updated = self._validate_node(patch, existing=node)
        if not updated.first_seen_at:
            updated.first_seen_at = int(time.time())
        if host:
            updated.enrolled_host = host
        nodes[index] = updated
        self._persist_nodes(nodes)
        return self.node_public(updated)
