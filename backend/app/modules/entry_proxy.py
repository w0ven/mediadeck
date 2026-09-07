"""Fixed-upstream reverse-proxy configuration for one registered friend entry.

Both engines describe the same contract, so they share one validation pass:
every upstream is an administrator-configured HTTPS origin, ``/_n/<node>/s/``
strips exactly its own prefix, an unmatched ``/_n/`` is refused rather than
guessed, and the entry credential is attached only on the Emby hop.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

from app.core.config import StreamNode
from app.core.errors import ConfigError
from app.modules.entries import ID_RE, NODE_RE, https_origin

KEY_ALPHABET = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
SERVERS = ("caddy", "nginx")
# Long-lived media responses: a client may hold one connection for a whole
# episode, so the default 60s upstream read timeout would cut playback.
READ_TIMEOUT = "600s"
# Inspect the raw URI before either proxy normalises dots or encoded slashes.
DOT_SEGMENT = r"^/_n/[^?]*(/|%2[fF])([.]|%2[eE]){1,2}(/|%2[fF]|[?]|$)"
# Recognise aliases in the raw request too: nginx may remove the namespace
# altogether while decoding a traversal before choosing its fallback location.
RESERVED_RAW = r"^(/|%2[fF])+(_|%5[fF])(n|%6[eE])(/|%2[fF]|[?]|$)"


@dataclass(frozen=True)
class _Upstream:
    name: str
    origin: str
    host: str
    hostname: str


@dataclass(frozen=True)
class _Plan:
    """Everything both templates need, validated once."""

    entry_id: str
    key: str
    origin: str
    origin_host: str
    emby: str
    emby_host: str
    emby_hostname: str
    nodes: tuple[_Upstream, ...]
    # Pinned mode: stream_origin proxies exactly ``pinned`` (one of ``nodes``).
    stream_origin: str = ""
    pinned: _Upstream | None = None


def _plan(entry: dict[str, str], emby_url: str, nodes: list[StreamNode]) -> _Plan:
    origin = https_origin(entry["origin"])
    emby = https_origin(emby_url)
    if not ID_RE.fullmatch(entry["id"]):
        raise ConfigError("入口 ID 无效")
    # Only panel-generated URL-safe keys may be embedded in a config file.
    key = entry.get("proxy_key", "")
    if len(key) < 40 or any(c not in KEY_ALPHABET for c in key):
        raise ConfigError("入口凭据无效，请轮换后重新导出")

    upstreams = []
    for node in nodes:
        if not NODE_RE.fullmatch(node.name):
            raise ConfigError("节点名称不能用于反代路径")
        if not node.pools:
            continue
        if any(not pool.url_prefix.startswith("/s/") for pool in node.pools):
            raise ConfigError("外部入口要求节点的媒体 URL 前缀位于 /s/ 下")
        upstream = https_origin(node.base_url)
        parsed = urlsplit(upstream)
        # Include disabled nodes too: an already-issued signed URL should
        # remain usable while the scheduler stops assigning new sessions.
        upstreams.append(_Upstream(node.name, upstream, parsed.netloc, parsed.hostname or ""))
    if not upstreams:
        raise ConfigError("请先配置至少一个带媒体根的 HTTPS 节点")

    emby_parsed = urlsplit(emby)
    stream_origin = entry.get("stream_origin") or ""
    pinned = None
    if stream_origin:
        stream_origin = https_origin(stream_origin)
        pinned = next((u for u in upstreams if u.name == entry.get("node")), None)
        if pinned is None:
            raise ConfigError("固定节点不在节点池中，或没有媒体根")
    return _Plan(entry_id=entry["id"], key=key,
                 origin=origin, origin_host=urlsplit(origin).netloc,
                 emby=emby, emby_host=emby_parsed.netloc,
                 emby_hostname=emby_parsed.hostname or "",
                 nodes=tuple(upstreams),
                 stream_origin=stream_origin, pinned=pinned)


def friend_config(entry: dict[str, str], emby_url: str, nodes: list[StreamNode],
                  server: str = "caddy") -> str:
    if server not in SERVERS:
        raise ConfigError("反代类型仅支持 Caddy 或 nginx")
    plan = _plan(entry, emby_url, nodes)
    if plan.pinned is not None:
        return _caddy_pinned(plan) if server == "caddy" else _nginx_pinned(plan)
    return _caddy(plan) if server == "caddy" else _nginx(plan)


# ---------------------------------------------------------------- pinned mode
# For a CDN that can rewrite headers but cannot route by path: two hostnames,
# each with exactly one fixed upstream. The main hostname carries the entry
# credential to Emby; the stream hostname forwards /s/... untouched to ONE
# node, which still verifies the signature. Nothing under /_n/ exists here.


def _caddy_pinned(plan: _Plan) -> str:
    node = plan.pinned
    assert node is not None
    return f"""# Private file: contains this entry's proxy credential. Store with mode 600.
# Pinned mode: the stream hostname proxies exactly one node. No path routing
# is needed on the CDN side. No cache handler anywhere.

# --- main hostname: everything goes to Emby with this entry's credential ---
{plan.origin} {{
    header Cache-Control "private, no-store"
    header -X-Mediadeck-Entry-Key

    reverse_proxy {plan.emby} {{
        header_up Host {plan.emby_host}
        header_up X-Mediadeck-Entry {plan.entry_id}
        header_up X-Mediadeck-Entry-Key {plan.key}
        header_up X-Forwarded-Host {plan.origin_host}
        header_down -X-Mediadeck-Entry-Key
        transport http {{
            tls_server_name {plan.emby_hostname}
            read_timeout {READ_TIMEOUT}
        }}
    }}
}}

# --- stream hostname: /s/... goes to the pinned node, path untouched -------
{plan.stream_origin} {{
    header Cache-Control "private, no-store"

    @media path /s/*
    handle @media {{
        reverse_proxy {node.origin} {{
            header_up Host {node.host}
            header_up -X-Mediadeck-Entry
            header_up -X-Mediadeck-Entry-Key
            header_up -Authorization
            header_up -X-Emby-Token
            header_up -X-MediaBrowser-Token
            header_up -X-Emby-Authorization
            header_up -Cookie
            transport http {{
                tls_server_name {node.hostname}
                read_timeout {READ_TIMEOUT}
            }}
        }}
    }}

    # The stream hostname serves media only; anything else is refused.
    handle {{
        respond 404
    }}
}}
"""


def _nginx_pinned(plan: _Plan) -> str:
    node = plan.pinned
    assert node is not None
    ns = _namespace(plan)
    origin = urlsplit(plan.origin)
    stream = urlsplit(plan.stream_origin)
    port = origin.port or 443
    sport = stream.port or 443
    node_port = "" if urlsplit(node.origin).port else ":443"
    return f"""# Private file: contains this entry's proxy credential. Store with mode 600.
# Pinned mode: the stream hostname proxies exactly one node. Drop into a
# context already inside http{{}}. No proxy_cache anywhere.
# REQUIRES nginx >= 1.25.1 and installed TLS certificates for BOTH hostnames
# BEFORE nginx -t. Adjust the four certificate paths below.
upstream {ns}_node {{
    server {node.host}{node_port};
}}
map $http_upgrade ${ns}_connection {{
    default upgrade;
    ''      close;
}}

# --- main hostname: everything goes to Emby with this entry's credential ---
server {{
    listen {port} ssl;
    listen [::]:{port} ssl;
    http2 on;
    server_name {origin.hostname};

    ssl_certificate     /etc/letsencrypt/live/{origin.hostname}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{origin.hostname}/privkey.pem;

    access_log off;
    client_max_body_size 0;
    add_header Cache-Control "private, no-store" always;

    location / {{
        proxy_pass {plan.emby};
        proxy_set_header Host {plan.emby_host};
        proxy_ssl_server_name on;
        proxy_ssl_name {plan.emby_hostname};
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_set_header X-Mediadeck-Entry {plan.entry_id};
        proxy_set_header X-Mediadeck-Entry-Key {plan.key};
        proxy_set_header X-Forwarded-Host {plan.origin_host};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_hide_header X-Mediadeck-Entry-Key;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection ${ns}_connection;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout {READ_TIMEOUT};
        proxy_send_timeout {READ_TIMEOUT};
    }}
}}

# --- stream hostname: /s/... goes to the pinned node, path untouched -------
server {{
    listen {sport} ssl;
    listen [::]:{sport} ssl;
    http2 on;
    server_name {stream.hostname};

    ssl_certificate     /etc/letsencrypt/live/{stream.hostname}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{stream.hostname}/privkey.pem;

    access_log off;
    merge_slashes off;
    large_client_header_buffers 4 32k;
    add_header Cache-Control "private, no-store" always;

    location ^~ /s/ {{
        # $request_uri keeps the exact percent-encoded path the node signed.
        proxy_pass https://{ns}_node$request_uri;
        proxy_set_header Host {node.host};
        proxy_ssl_server_name on;
        proxy_ssl_name {node.hostname};
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_set_header Authorization "";
        proxy_set_header X-Emby-Token "";
        proxy_set_header X-MediaBrowser-Token "";
        proxy_set_header X-Emby-Authorization "";
        proxy_set_header Cookie "";
        proxy_set_header X-Mediadeck-Entry "";
        proxy_set_header X-Mediadeck-Entry-Key "";
        proxy_http_version 1.1;
        proxy_set_header Range $http_range;
        proxy_set_header If-Range $http_if_range;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout {READ_TIMEOUT};
        proxy_send_timeout {READ_TIMEOUT};
    }}

    # The stream hostname serves media only; anything else is refused.
    location / {{
        return 404;
    }}
}}
"""


def friend_caddy(entry: dict[str, str], emby_url: str, nodes: list[StreamNode]) -> str:
    return friend_config(entry, emby_url, nodes, "caddy")


def _namespace(plan: _Plan) -> str:
    # nginx variables are case insensitive; IDs differing by case/punctuation
    # must still be isolated when multiple exported files share http{}.
    return "md_" + sha256(f"{plan.entry_id}:{plan.origin}".encode()).hexdigest()[:24]


def _caddy(plan: _Plan) -> str:
    blocks = []
    for index, node in enumerate(plan.nodes):
        pattern = node.name.replace(".", "[.]")
        # path_regexp preserves RawPath, unlike strip_prefix which cleans //.
        blocks.append(f"""    @node_{index} expression `{{http.request.orig_uri}}.matches('^/_n/{pattern}/s/')`
    handle @node_{index} {{
        uri path_regexp ^/_n/{pattern} ""
        reverse_proxy {node.origin} {{
            header_up Host {node.host}
            header_up -X-Mediadeck-Entry
            header_up -X-Mediadeck-Entry-Key
            header_up -Authorization
            header_up -X-Emby-Token
            header_up -X-MediaBrowser-Token
            header_up -X-Emby-Authorization
            header_up -Cookie
            header_down -X-Mediadeck-Entry-Key
            transport http {{
                tls_server_name {node.hostname}
                read_timeout {READ_TIMEOUT}
            }}
        }}
    }}
""")
    return f"""# Private file: contains this entry's proxy credential. Store with mode 600.
# Core Caddy only: no cache handler or dynamic upstream. Range, query and
# WebSocket upgrades pass through. Never log request headers or response bodies.
{plan.origin} {{
    header Cache-Control "private, no-store"
    header -X-Mediadeck-Entry-Key

    route {{
    @dot_segment expression `{{http.request.orig_uri}}.matches('{DOT_SEGMENT}')`
    respond @dot_segment 404

{"".join(blocks)}    @reserved expression `{{http.request.orig_uri}}.matches('{RESERVED_RAW}') || {{http.request.uri.path}}.matches('^/+_n(/|$)')`
    handle @reserved {{
        respond 404
    }}

    handle {{
        reverse_proxy {plan.emby} {{
            header_up Host {plan.emby_host}
            header_up X-Mediadeck-Entry {plan.entry_id}
            header_up X-Mediadeck-Entry-Key {plan.key}
            header_up X-Forwarded-Host {plan.origin_host}
            header_down -X-Mediadeck-Entry-Key
            transport http {{
                tls_server_name {plan.emby_hostname}
                read_timeout {READ_TIMEOUT}
            }}
        }}
    }}
    }}
}}
"""


def _nginx(plan: _Plan) -> str:
    """Forward the raw suffix, never nginx's decoded/normalised location URI.

    A variable URI stops proxy_pass re-escaping the path. Named upstreams are
    fixed at config load: no runtime DNS and no client-selected destination.
    """
    ns = _namespace(plan)
    origin = urlsplit(plan.origin)
    port = origin.port or 443
    maps, blocks = [], []
    for index, node in enumerate(plan.nodes):
        var = f"{ns}_{index}"
        pattern = node.name.replace(".", "[.]")
        maps.append(f"""upstream {var} {{
    server {node.host}{'' if urlsplit(node.origin).port else ':443'};
}}
map $request_uri ${var}_uri {{
    default "";
    ~^/_n/{pattern}(/s/.*)$ $1;
}}
""")
        blocks.append(f"""    location ^~ /_n/{node.name}/s/ {{
        if (${var}_uri = "") {{ return 404; }}
        proxy_pass https://{var}${var}_uri;
        proxy_set_header Host {node.host};
        proxy_ssl_server_name on;
        proxy_ssl_name {node.hostname};
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        # The node authenticates the signed URL itself; forwarding a viewer
        # credential to it would widen this hop for no reason.
        proxy_set_header Authorization "";
        proxy_set_header X-Emby-Token "";
        proxy_set_header X-MediaBrowser-Token "";
        proxy_set_header X-Emby-Authorization "";
        proxy_set_header Cookie "";
        proxy_set_header X-Mediadeck-Entry "";
        proxy_set_header X-Mediadeck-Entry-Key "";
        proxy_hide_header X-Mediadeck-Entry-Key;
        proxy_http_version 1.1;
        proxy_set_header Range $http_range;
        proxy_set_header If-Range $http_if_range;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout {READ_TIMEOUT};
        proxy_send_timeout {READ_TIMEOUT};
    }}
""")
    return f"""# Private file: contains this entry's proxy credential. Store with mode 600.
# Drop into a context that is already inside http{{}} (for example
# /etc/nginx/conf.d/). No proxy_cache anywhere: signed URLs expire, and a
# cached 302 or media body would serve one viewer's link to another.
# REQUIRES nginx >= 1.25.1 and an installed TLS certificate BEFORE nginx -t.
# Provision the certificate and adjust BOTH active certificate paths below.
# Test with nginx -t before reloading; this file alone cannot obtain a cert.
{"".join(maps)}map $http_upgrade ${ns}_connection {{
    default upgrade;
    ''      close;
}}

map $request_uri ${ns}_reserved {{
    default 0;
    ~{RESERVED_RAW} 1;
}}
map $request_uri ${ns}_dot_segment {{
    default 0;
    "~{DOT_SEGMENT}" 1;
}}

server {{
    listen {port} ssl;
    listen [::]:{port} ssl;
    http2 on;
    server_name {origin.hostname};

    ssl_certificate     /etc/letsencrypt/live/{origin.hostname}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{origin.hostname}/privkey.pem;

    access_log off;
    merge_slashes off;
    large_client_header_buffers 4 32k;
    client_max_body_size 0;
    add_header Cache-Control "private, no-store" always;
    if (${ns}_dot_segment) {{ return 404; }}

{"".join(blocks)}    # Unknown node names are refused rather than guessed: this must never
    # become an open proxy that any URL can steer.
    location = /_n {{ return 404; }}
    location ^~ /_n/ {{
        return 404;
    }}

    location / {{
        if (${ns}_reserved) {{ return 404; }}
        proxy_pass {plan.emby};
        proxy_set_header Host {plan.emby_host};
        proxy_ssl_server_name on;
        proxy_ssl_name {plan.emby_hostname};
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_set_header X-Mediadeck-Entry {plan.entry_id};
        proxy_set_header X-Mediadeck-Entry-Key {plan.key};
        proxy_set_header X-Forwarded-Host {plan.origin_host};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_hide_header X-Mediadeck-Entry-Key;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection ${ns}_connection;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout {READ_TIMEOUT};
        proxy_send_timeout {READ_TIMEOUT};
    }}
}}
"""
