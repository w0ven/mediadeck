"""Fixed-upstream reverse-proxy configuration for one registered friend entry.

Both engines describe the same contract, so they share one validation pass:
every upstream is an administrator-configured HTTPS origin, ``/_n/<node>/s/``
strips exactly its own prefix, an unmatched ``/_n/`` is refused rather than
guessed, and the entry credential is attached only on the Emby hop.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.config import StreamNode
from app.core.errors import ConfigError
from app.modules.entries import ID_RE, NODE_RE, https_origin

KEY_ALPHABET = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
SERVERS = ("caddy", "nginx")
# Long-lived media responses: a client may hold one connection for a whole
# episode, so the default 60s upstream read timeout would cut playback.
READ_TIMEOUT = "600s"


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
    return _Plan(entry_id=entry["id"], key=key,
                 origin=origin, origin_host=urlsplit(origin).netloc,
                 emby=emby, emby_host=emby_parsed.netloc,
                 emby_hostname=emby_parsed.hostname or "",
                 nodes=tuple(upstreams))


def friend_config(entry: dict[str, str], emby_url: str, nodes: list[StreamNode],
                  server: str = "caddy") -> str:
    if server not in SERVERS:
        raise ConfigError("反代类型仅支持 Caddy 或 nginx")
    plan = _plan(entry, emby_url, nodes)
    return _caddy(plan) if server == "caddy" else _nginx(plan)


def friend_caddy(entry: dict[str, str], emby_url: str, nodes: list[StreamNode]) -> str:
    return friend_config(entry, emby_url, nodes, "caddy")


def _caddy(plan: _Plan) -> str:
    blocks = [f"""    handle /_n/{node.name}/s/* {{
        uri strip_prefix /_n/{node.name}
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
""" for node in plan.nodes]
    return f"""# Private file: contains this entry's proxy credential. Store with mode 600.
# Core Caddy only: no cache handler or dynamic upstream. Range, query and
# WebSocket upgrades pass through. Never log request headers or response bodies.
{plan.origin} {{
    header Cache-Control "private, no-store"
    header -X-Mediadeck-Entry-Key

{"".join(blocks)}    handle /_n/* {{
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
"""


def _nginx(plan: _Plan) -> str:
    """Same contract for nginx.

    ``location ^~ /_n/<node>/s/`` plus a ``proxy_pass`` that ends in ``/s/``
    performs the prefix strip with a literal upstream, so nginx resolves the
    host at config load and no ``resolver`` is required. ``^~`` also stops a
    later regex location from stealing the media route. Percent-encoded media
    paths survive because both sides compare the decoded form: the node signs
    and verifies ``$uri``.
    """
    listen = ""
    if ":" in plan.origin_host:
        listen = f"    # 入口端口为 {plan.origin_host.rsplit(':', 1)[1]}，按需调整下面的 listen\n"
    blocks = [f"""    location ^~ /_n/{node.name}/s/ {{
        proxy_pass {node.origin}/s/;
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
""" for node in plan.nodes]
    return f"""# Private file: contains this entry's proxy credential. Store with mode 600.
# Drop into a context that is already inside http{{}} (for example
# /etc/nginx/conf.d/). No proxy_cache anywhere: signed URLs expire, and a
# cached 302 or media body would serve one viewer's link to another.
map $http_upgrade $mediadeck_connection {{
    default upgrade;
    ''      close;
}}

server {{
{listen}    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name {plan.origin_host.rsplit(':', 1)[0] if ':' in plan.origin_host else plan.origin_host};

    # ssl_certificate     /etc/letsencrypt/live/<域名>/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/<域名>/privkey.pem;

    client_max_body_size 0;
    add_header Cache-Control "private, no-store" always;

{"".join(blocks)}    # Unknown node names are refused rather than guessed: this must never
    # become an open proxy that any URL can steer.
    location ^~ /_n/ {{
        return 404;
    }}

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
        proxy_set_header Connection $mediadeck_connection;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout {READ_TIMEOUT};
        proxy_send_timeout {READ_TIMEOUT};
    }}
}}
"""
