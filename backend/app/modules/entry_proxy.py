"""Fixed-upstream Caddy configuration for one registered friend proxy."""
from __future__ import annotations

from urllib.parse import urlsplit

from app.core.config import StreamNode
from app.core.errors import ConfigError
from app.modules.entries import ID_RE, NODE_RE, https_origin


def friend_caddy(entry: dict[str, str], emby_url: str, nodes: list[StreamNode]) -> str:
    origin = https_origin(entry["origin"])
    emby = https_origin(emby_url)
    if not ID_RE.fullmatch(entry["id"]):
        raise ConfigError("入口 ID 无效")
    # Only panel-generated URL-safe keys can be embedded in a Caddyfile.
    key = entry.get("proxy_key", "")
    if len(key) < 40 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                            for c in key):
        raise ConfigError("入口凭据无效，请轮换后重新导出")
    blocks = []
    for node in nodes:
        if not NODE_RE.fullmatch(node.name):
            raise ConfigError("节点名称不能用于反代路径")
        if not node.pools:
            continue
        if any(not pool.url_prefix.startswith("/s/") for pool in node.pools):
            raise ConfigError("外部入口要求节点的媒体 URL 前缀位于 /s/ 下")
        upstream = https_origin(node.base_url)
        host = urlsplit(upstream)
        # Include disabled nodes too: an already-issued signed URL should
        # remain usable while the scheduler stops assigning new sessions.
        blocks.append(f"""    handle /_n/{node.name}/s/* {{
        uri strip_prefix /_n/{node.name}
        reverse_proxy {upstream} {{
            header_up Host {host.netloc}
            header_up -X-Mediadeck-Entry
            header_up -X-Mediadeck-Entry-Key
            header_up -Authorization
            header_up -X-Emby-Token
            header_up -X-MediaBrowser-Token
            header_up -X-Emby-Authorization
            header_up -Cookie
            header_down -X-Mediadeck-Entry-Key
            transport http {{
                tls_server_name {host.hostname}
            }}
        }}
    }}
""")
    if not blocks:
        raise ConfigError("请先配置至少一个带媒体根的 HTTPS 节点")
    emby_host = urlsplit(emby)
    node_blocks = "\n".join(blocks)
    return f"""# Private file: contains this entry's proxy credential. Store with mode 600.
# Core Caddy only: no cache handler or dynamic upstream. Range, query and
# WebSocket upgrades pass through. Never log request headers or response bodies.
{origin} {{
    header Cache-Control "private, no-store"
    header -X-Mediadeck-Entry-Key

{node_blocks}
    handle /_n/* {{
        respond 404
    }}

    handle {{
        reverse_proxy {emby} {{
            header_up Host {emby_host.netloc}
            header_up X-Mediadeck-Entry {entry["id"]}
            header_up X-Mediadeck-Entry-Key {key}
            header_up X-Forwarded-Host {urlsplit(origin).netloc}
            header_down -X-Mediadeck-Entry-Key
            transport http {{
                tls_server_name {emby_host.hostname}
            }}
        }}
    }}
}}
"""
