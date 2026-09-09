"""Node provisioning — turn a bare server into a streaming node.

Design rule: the operator should not type configuration on the target machine.
Everything a node needs (Drive identity, media roots, cache, signing key) is
already stored against that node in the panel, so enrollment is genuinely one
command: the installer fetches its own config using a one-shot token.

A node needs five things, and each has a detail that silently breaks playback:

1. **rclone remote + mount** — byte-identical files at a known root. The remote
   config is pushed from the panel; requiring `rclone config` on the node would
   defeat the whole point of one-command install.
2. **VFS cache on a big disk** — streaming without a cache re-reads the cloud
   on every seek.
3. **nginx with secure_link** — signed, expiring URLs and working byte ranges.
4. **Direct TLS, DNS-only** — video must not traverse the CDN proxy.
5. **loadprobe agent** — otherwise the scheduler cannot see load and the node
   is never selected.

Nothing here executes anything or contacts a remote host: it emits text the
operator reviews and runs.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any
from urllib.parse import urlparse

from app.modules.signing import validate_arg_names

LOADPROBE_PORT = 9800
METERD_PORT = 9801


def _config_name(value: str) -> str:
    # These are syntax (filenames, unit names), not opaque argument values.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", value):
        raise ValueError("invalid node or pool name")
    return value


def _comment(value: Any) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _systemd_quote(value: Any) -> str:
    # Exec* is not a shell: systemd performs its own C escapes, environment
    # substitution and % specifiers even inside quoted arguments.
    value = str(value).replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + value.replace("%", "%%").replace("$", "$$") + '"'


def _nginx_quote(value: Any) -> str:
    value = str(value)
    if "\x00" in value:
        raise ValueError("NUL is not valid nginx configuration")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def _delimiter(preferred: str, content: str) -> str:
    # Quoted heredocs preserve opaque bytes; the delimiter must not be one
    # of those bytes' lines. Do not escape/rewrite the configuration itself.
    lines = set(content.splitlines())
    while preferred in lines:
        preferred += "_"
    return preferred


def _host_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.hostname or ""


def enroll_command(panel_url: str, token: str) -> str:
    """The single line an operator pastes on a fresh server."""
    base = panel_url.rstrip("/")
    # Token is also passed as env so the piped script can report home even
    # though bash sees stdin as $0, not the original URL.
    return (f"curl -fsSL {shlex.quote(f'{base}/api/enroll/{token}/script')} | "
            f"sudo env MEDIADECK_ENROLL_TOKEN={shlex.quote(token)} bash")


def signing_config(node: Any, *, metering: bool = False) -> str:
    """Private probe configuration; does not enable a collector or scheduler."""
    validate_arg_names(node.sign_arg_digest, node.sign_arg_expires)
    return json.dumps({"version": 2, "secret": str(node.sign_secret or ""),
                       "arg_digest": node.sign_arg_digest, "arg_expires": node.sign_arg_expires,
                       "metering": metering, "metering_port": METERD_PORT}, ensure_ascii=False)


def nginx_signing_guard() -> str:
    """Replace (do not add beside) an existing media auth_request directive."""
    return """set $md_verify_path $uri;
        set $md_verify_method $request_method;
        set $md_u $arg_u;
        set $md_lip $server_addr;
        set $md_lp $server_port;
        set $md_a $remote_addr;
        set $md_p $remote_port;
        set $md_cid $pid.$connection;
        auth_request /_mediadeck/verify;"""


def nginx_signing_endpoint() -> str:
    """One fail-closed signature gate; optional meter failures stay inside probe.

    No error_page-to-allow here. The endpoint is internal and receives only
    nginx-captured metadata, not client-supplied authentication assertions.
    """
    return f"""location = /_mediadeck/verify {{
        internal;
        proxy_pass http://127.0.0.1:{LOADPROBE_PORT}/verify;
        proxy_pass_request_body off;
        proxy_pass_request_headers off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Mediadeck-Target $request_uri;
        proxy_set_header X-Mediadeck-Path $md_verify_path;
        proxy_set_header X-Mediadeck-Method $md_verify_method;
        proxy_set_header X-Mediadeck-Local-Addr $md_lip;
        proxy_set_header X-Mediadeck-Local-Port $md_lp;
        proxy_set_header X-Mediadeck-Remote-Addr $md_a;
        proxy_set_header X-Mediadeck-Remote-Port $md_p;
        proxy_set_header X-Mediadeck-Connection $md_cid;
        proxy_connect_timeout 300ms;
        proxy_read_timeout 2s;
        proxy_intercept_errors off;
        access_log off;
    }}"""


def nginx_site(node: Any) -> str:
    """nginx media vhost with v2-only HMAC verification on the existing probe.

    This is a fresh-install template, not permission to replace production
    aliases. For existing sites apply only the guard/internal endpoint.
    """
    _config_name(node.name)
    host = _host_of(node.base_url) or f"{node.name}.example.com"
    secret = str(getattr(node, "sign_secret", "") or "")
    validate_arg_names(node.sign_arg_digest, node.sign_arg_expires)
    literals: list[str] = []

    if secret:
        secure = """
    # v2 HMAC only. The secret lives in the probe's private signing.json.
    # v1 links are intentionally rejected; there is no MD5 fallback.
    merge_slashes off;
"""
        guard = ""
    else:
        secure = """
    # WARNING: signing disabled. Every URL handed to a client is a permanent
    # public download link. Set a signing key for this node in the panel.
"""
        guard = ""

    locations = []
    for index, pool in enumerate(node.pools or []):
        _config_name(pool.name)
        node_path = str(pool.node_path or "").rstrip("/")
        if not node_path:
            continue
        literals.append(f"geo $mediadeck_pool_path_{index} {{ default {_nginx_quote(node_path)}; }}")
        locations.append(f"""
    location {_nginx_quote(str(pool.url_prefix).rstrip('/') + '/')} {{{guard}
        alias $mediadeck_pool_path_{index}/;

        # Per-user cap from the signed r argument (bytes/second, 0 =
        # uncapped). limit_rate is per request. HTTP/1.1 stops one TCP
        # connection from multiplexing N full-rate Ranges (that was the
        # 15 x N burst). Players still open a couple of sockets for seek
        # overlap and a second allowed stream; 1 rejected those as 503 and
        # clients showed "file corrupt". 4 covers two streams + seek.
        # No burst window.
        limit_conn mediadeck_peruser 4;
        limit_rate $mediadeck_rate;

        # Real transfer bytes per user for the panel's live-speed display.
        access_log /var/log/nginx/mediadeck-speed.log mediadeck_speed;

        # Synchronous register-then-allow. mirror is fire-and-forget and
        # cannot promise the nft element exists before the first payload.
        # 403 from meterd (blocked / mixed identity) rejects this request,
        # including a six-hour-old signed URL. 5xx from meterd fail-open
        # inside the subrequest so a down agent does not invent new bans.
        #
        # Capture the parent 4-tuple + signed u HERE. nginx 1.22 does not
        # expand variables in an auth_request query string, and a literal
        # "?..." also breaks `location = /_mediadeck/register`.
        set $md_u $arg_u;
        set $md_lip $server_addr;
        set $md_lp $server_port;
        set $md_a $remote_addr;
        set $md_p $remote_port;
        set $md_cid $pid.$connection;
        # Known deny is an nginx map file written by meterd. It survives a
        # dead HTTP process, so 502 fail-open cannot admit an exhausted tag.
        if ($md_denied) {{ return 403; }}
        {nginx_signing_guard() if secret else 'auth_request /_mediadeck/register;'}
        # Speed attribution is independent of billing register.
        mirror /_mediadeck/announce;

        # Emby clients seek constantly; byte ranges are mandatory.
        add_header Accept-Ranges bytes;
        add_header X-Mediadeck-Node "{node.name}" always;
        add_header X-Mediadeck-Pool "{pool.name}" always;
        autoindex off;                              # never list the library
    }}""")

    literal_config = "\n".join(literals)
    return f"""# /etc/nginx/sites-available/mediadeck-{node.name}
# Media delivery for streaming node "{node.name}". Managed by mediadeck.
{literal_config}

# Effective rate. The signed r argument is the member's cap in bytes/second;
# "0" means the operator chose uncapped. A link with no r at all predates the
# rate rollout and gets a conservative safety cap instead.
map $arg_r $mediadeck_rate {{
    ""      15728640;
    default $arg_r;
}}

# Cap is per member, not per TCP connection. Empty u (unsigned / unresolved)
# falls back to the client address so those sockets do not share one bucket.
map $arg_u $mediadeck_user_key {{
    ""      $remote_addr;
    default $arg_u;
}}

# One line per completed request: unix-time, peer address, anonymised user
# tag, rate cap, bytes, request seconds. The loadprobe agent uses the address
# to map live sockets to a member, because a request is only logged when it
# *ends* -- most playback requests run for minutes, so completed lines alone
# cannot show what a viewer is doing right now. Tags are hashes, so the log
# never names an account.
log_format mediadeck_speed
    '$msec a=$remote_addr p=$remote_port u=$arg_u r=$arg_r '
    '$bytes_sent $request_time';

# Known exhausted / denied tags. Default 0 = unknown, fail-open for new
# decisions. Meterd rewrites the include atomically; nginx reloads it.
map $md_u $md_denied {{
    default 0;
    include /var/lib/mediadeck/deny.map;
}}

limit_conn_zone $mediadeck_user_key zone=mediadeck_peruser:10m;

server {{
    listen 80;
    listen [::]:80;
    server_name {_nginx_quote(host)};
    location /.well-known/acme-challenge/ {{ root /var/www/html; }}
    location / {{ return 301 https://$host$request_uri; }}
}}

server {{
    # HTTP/1.1 on purpose. HTTP/2 multiplexes many Range requests onto one
    # TCP connection; nginx limit_rate then applies per stream, so a 15 MB/s
    # cap becomes 15 x N. Players already speak Range over HTTP/1.1.
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {_nginx_quote(host)};

    # Direct TLS. This hostname must be DNS-only (grey cloud): video traffic
    # must not go through the CDN proxy.
    ssl_certificate     {_nginx_quote(f'/etc/letsencrypt/live/{host}/fullchain.pem')};
    ssl_certificate_key {_nginx_quote(f'/etc/letsencrypt/live/{host}/privkey.pem')};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;

    # Media is large and already compressed; buffering wastes RAM and adds
    # latency to seeks.
    gzip off;
    sendfile on;
    tcp_nopush on;
    aio threads;
    directio 8m;
    output_buffers 4 512k;
    client_max_body_size 0;
    keepalive_timeout 300s;
    send_timeout 300s;

    add_header Cache-Control "no-store" always;
{secure}
{"".join(locations) or "    # NOTE: no media roots configured for this node yet."}

    {nginx_signing_endpoint() if secret else ''}

    # Optional unsigned-mode 4-tuple registration; NEVER a signature gate.
    location = /_mediadeck/register {{
        internal;
        proxy_pass http://127.0.0.1:{METERD_PORT}/register?lip=$md_lip&lp=$md_lp&a=$md_a&p=$md_p&u=$md_u&cid=$md_cid;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Mediadeck-Utag $md_u;
        proxy_connect_timeout 300ms;
        proxy_read_timeout 500ms;
        proxy_intercept_errors on;
        error_page 502 503 504 =200 /_mediadeck/allow;
        access_log off;
    }}

    location = /_mediadeck/allow {{
        internal;
        access_log off;
        return 204;
    }}

    # Optional speed-map ping; must not be treated as the billing register.
    location = /_mediadeck/announce {{
        internal;
        proxy_pass http://127.0.0.1:{LOADPROBE_PORT}/announce?a=$md_a&p=$md_p&u=$md_u;
        proxy_connect_timeout 300ms;
        proxy_read_timeout 500ms;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        access_log off;
    }}

    location = /healthz {{
        access_log off;
        return 200 "ok\\n";
    }}
}}
"""


def rclone_mount_unit(node: Any, pool: Any) -> str:
    _config_name(node.name)
    _config_name(pool.name)
    remote = _systemd_quote(pool.rclone_remote or "")
    node_path = _systemd_quote(str(pool.node_path or "").rstrip("/"))
    cache_path = _systemd_quote(f"{node.cache_dir}/{pool.name}")
    unit = f"mediadeck-mount-{node.name}-{pool.name}"
    return f"""# /etc/systemd/system/{unit}.service
[Unit]
Description=mediadeck media mount ({node.name}/{pool.name})
After=network-online.target
Wants=network-online.target
# nginx must not serve before the files are there, or it caches 404s for the
# whole library.
Before=nginx.service

[Service]
Type=notify
ExecStartPre=/bin/mkdir -p {node_path} {cache_path}
ExecStart=/usr/bin/rclone mount {remote} {node_path} \\
    --config /root/.config/rclone/rclone.conf \\
    --allow-other \\
    --read-only \\
    --dir-cache-time 72h \\
    --poll-interval 15s \\
    --vfs-cache-mode full \\
    --vfs-cache-max-size {_systemd_quote(node.cache_size)} \\
    --vfs-cache-max-age 168h \\
    --vfs-read-chunk-size 32M \\
    --vfs-read-chunk-size-limit 1G \\
    --vfs-read-ahead 256M \\
    --buffer-size 64M \\
    --cache-dir {cache_path} \\
    --umask 022 \\
    --log-level INFO
ExecStop=/bin/fusermount3 -uz {node_path}
Restart=on-failure
RestartSec=10
# A stale FUSE mount wedges every reader in uninterruptible sleep.
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def loadprobe_unit(node: Any) -> str:
    _config_name(node.name)
    return f"""# /etc/systemd/system/mediadeck-loadprobe.service
# Reports stream count and egress to the panel; without it the scheduler
# cannot see this node's load and will never dispatch to it.
[Unit]
Description=mediadeck node load probe ({node.name})
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/mediadeck-agent/loadprobe.py --port {LOADPROBE_PORT} --signing-config /etc/mediadeck/signing.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def meterd_unit(node: Any, panel_url: str) -> str:
    """systemd unit for the measured-flow agent. Generated text only."""
    _config_name(node.name)
    panel = (panel_url or "").rstrip("/")
    return f"""# /etc/systemd/system/mediadeck-meterd.service
# Synchronous register (nginx auth_request) + measured envelope reports.
# --enable-nft is the production switch; importing the module never
# touches host tables.
[Unit]
Description=mediadeck measured-flow agent ({node.name})
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/mediadeck-agent/meterd.py \\
    --panel {_systemd_quote(panel)} \\
    --node {_systemd_quote(node.name)} \\
    --token-file /etc/mediadeck/report.token \\
    --persist /var/lib/mediadeck/flowmeter.db \\
    --deny-map /var/lib/mediadeck/deny.map \\
    --nginx-bin /usr/sbin/nginx \\
    --nginx-conf /etc/nginx/nginx.conf \\
    --bind 127.0.0.1 --port {METERD_PORT} \\
    --enable-nft
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install_script(node: Any, panel_url: str) -> str:
    """One-shot installer, fully parameterised from stored node config."""
    _config_name(node.name)
    host = _host_of(node.base_url) or f"{node.name}.example.com"
    panel = panel_url.rstrip("/")
    pools = list(node.pools or [])
    secret = str(getattr(node, "sign_secret", "") or "")
    rclone_conf = str(getattr(node, "rclone_conf", "") or "")
    report_token = str(getattr(node, "report_token", "") or "")
    nginx_config = nginx_site(node)
    probe_config = loadprobe_unit(node)
    meter_config = meterd_unit(node, panel)
    nginx_end = _delimiter("MEDIADECK_NGINX_EOF", nginx_config)
    probe_end = _delimiter("MEDIADECK_PROBE_EOF", probe_config)
    meter_end = _delimiter("MEDIADECK_METERD_EOF", meter_config)
    token_end = _delimiter("MEDIADECK_TOKEN_EOF", report_token)
    # This installer already provisions meterd; standalone probe config
    # generation defaults to metering=False and never installs a collector.
    sign_config = signing_config(node, metering=True)
    sign_end = _delimiter("MEDIADECK_SIGNING_EOF", sign_config)

    sign_note = ("已启用签名：nginx 校验有效期，过期链接自动失效"
                 if secret else
                 "⚠ 未启用签名：任何人拿到链接都能永久下载")

    roots = "\n".join(
        f"#   {_comment(p.emby_prefix)}  ->  {_comment(p.node_path)}   (URL {_comment(p.url_prefix)}/, remote {_comment(p.rclone_remote)})"
        for p in pools) or "#   (未配置媒体根 — 安装后该节点无法提供任何文件)"

    # rclone.conf pushed from the panel: requiring `rclone config` on the node
    # would defeat one-command install.
    if rclone_conf:
        rclone_end = _delimiter("MEDIADECK_RCLONE_EOF", rclone_conf)
        rclone_step = f"""
echo "==> 3/6 写入 rclone 配置（来自面板）"
mkdir -p /root/.config/rclone
install -m 600 /dev/stdin /root/.config/rclone/rclone.conf <<'{rclone_end}'
{rclone_conf}
{rclone_end}
echo "    已写入 $(rclone listremotes --config /root/.config/rclone/rclone.conf | wc -l) 个 remote"
"""
    else:
        needed = sorted({str(p.rclone_remote).split(":")[0] for p in pools if p.rclone_remote})
        rclone_step = f"""
echo "==> 3/6 检查 rclone 配置"
# 面板未保存 rclone 配置，这里只校验节点上是否已有对应 remote。
mkdir -p /root/.config/rclone
for r in {" ".join(shlex.quote(n) for n in needed) or "''"}; do
  [ -n "$r" ] || continue
  if ! rclone listremotes --config /root/.config/rclone/rclone.conf 2>/dev/null | grep -qx "$r:"; then
    echo "    !! 缺少 remote «$r»"
    echo "    !! 请在面板节点配置里粘贴 rclone.conf，或手动执行 rclone config"
    exit 1
  fi
done
chmod 600 /root/.config/rclone/rclone.conf 2>/dev/null || true
"""

    mount_steps = []
    for pool in pools:
        if not (pool.node_path and pool.rclone_remote):
            continue
        _config_name(pool.name)
        unit = f"mediadeck-mount-{node.name}-{pool.name}"
        mount_config = rclone_mount_unit(node, pool)
        mount_end = _delimiter("MEDIADECK_MOUNT_EOF", mount_config)
        mount_steps.append(f"""
cat > /etc/systemd/system/{unit}.service <<'{mount_end}'
{mount_config}
{mount_end}
systemctl daemon-reload
systemctl enable --now {unit}.service
sleep 5
if mountpoint -q {shlex.quote(pool.node_path)}; then
  echo {shlex.quote(f'    [OK] {pool.name} -> {pool.node_path}')}
else
  echo "    [!!] {pool.name} 挂载失败: journalctl -u {unit} -n 50"
  exit 1
fi""")

    token_recover = (
        "# The one-liner that fetched this script is `.../api/enroll/<token>/script`.\n"
        "# Recover the token from env (preferred) or from $0 if curl left the URL there.\n"
        'ENROLL_TOKEN="${MEDIADECK_ENROLL_TOKEN:-}"\n'
        'if [ -z "$ENROLL_TOKEN" ]; then\n'
        '  case "${0:-}" in\n'
        '    *"/api/enroll/"*) ENROLL_TOKEN=$(printf \'%s\' "$0" | '
        "sed -n 's#.*/api/enroll/\\([^/]*\\)/script.*#\\1#p') ;;\n"
        "  esac\n"
        "fi\n"
    )
    report_home = (
        "# Tell the panel which addresses this machine actually has, so the operator\n"
        "# never has to type them. Failure here is non-fatal: the node still works.\n"
        'if [ -n "${ENROLL_TOKEN:-}" ]; then\n'
        '  echo "==> 上报本机地址"\n'
        f'  PUBLIC_HOST="$(hostname -f 2>/dev/null || hostname || echo {shlex.quote(host)})"\n'
        "  REPORT_JSON=$(printf "
        "'{\"base_url\":\"https://%s\",\"probe_url\":\"http://127.0.0.1:%s/load\",\"host\":\"%s\"}' "
        f'"$PUBLIC_HOST" {LOADPROBE_PORT} "$PUBLIC_HOST")\n'
        f"  curl -fsS -X POST {shlex.quote(panel + '/api/enroll/')}"
        '"${ENROLL_TOKEN}/report" \\\n'
        "    -H 'Content-Type: application/json' \\\n"
        '    -d "$REPORT_JSON" \\\n'
        '    >/dev/null && echo "    [OK] 已回连面板" || echo "    [!!] 回连面板失败（节点本身已可用）"\n'
        "fi\n"
    )

    return f"""#!/bin/bash
# ===========================================================
# mediadeck 节点安装 — "{node.name}"
# ===========================================================
# 在这台【推流节点】上以 root 执行。不会碰你的 Emby 主机。
#
# 签名状态：{sign_note}
# 媒体根映射（Emby 路径 -> 节点路径）：
{roots}
#
# 前置条件：
#   1. DNS 中 {_comment(host)} 已解析到本机，且为 DNS-only（灰云）
#      —— 视频流不能走 CDN 代理，否则会被限速/中断
#   2. {_comment(node.cache_dir)} 所在磁盘剩余空间 > {_comment(node.cache_size)}
# ===========================================================
set -euo pipefail
{token_recover}
echo "==> 1/6 安装依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx python3 fuse3 curl certbot python3-certbot-nginx nftables iproute2

echo "==> 2/6 安装 rclone"
command -v rclone >/dev/null || curl -fsSL https://rclone.org/install.sh | bash
{rclone_step}
echo "==> 4/6 配置挂载与缓存"
mkdir -p {shlex.quote(node.cache_dir)} /opt/mediadeck-agent
# allow_other 必须开启，否则 nginx(www-data) 读不到挂载 -> 全站 403
grep -q '^user_allow_other' /etc/fuse.conf || echo 'user_allow_other' >> /etc/fuse.conf
{"".join(mount_steps) or 'echo "    (无媒体根，跳过)"'}

echo "==> 5/6 配置 nginx 与证书"
mkdir -p /var/lib/mediadeck /var/www/html
# Re-enrollment must not erase an existing exhausted-member deny snapshot.
[ -e /var/lib/mediadeck/deny.map ] || printf '%s\\n' '# none' > /var/lib/mediadeck/deny.map
# Bootstrap HTTP only. certbot --nginx validates the installed config first,
# so installing ssl_certificate paths before they exist cannot bootstrap TLS.
if [ ! -s {shlex.quote(f'/etc/letsencrypt/live/{host}/fullchain.pem')} ]; then
  cat > /etc/nginx/sites-available/mediadeck-{node.name} <<'MEDIADECK_HTTP_EOF'
server {{
    listen 80;
    listen [::]:80;
    server_name {_nginx_quote(host)};
    location /.well-known/acme-challenge/ {{ root /var/www/html; }}
    location / {{ return 404; }}
}}
MEDIADECK_HTTP_EOF
  ln -sf /etc/nginx/sites-available/mediadeck-{node.name} /etc/nginx/sites-enabled/
  nginx -t && systemctl reload nginx
  certbot certonly --webroot -w /var/www/html -d {shlex.quote(host)} --non-interactive --agree-tos \\
    --register-unsafely-without-email || {{
      echo {shlex.quote(f'    !! 证书申请失败：确认 {host} 已指向本机且 80 端口可达')}; exit 1; }}
fi
cat > /etc/nginx/sites-available/mediadeck-{node.name} <<'{nginx_end}'
{nginx_config}
{nginx_end}
ln -sf /etc/nginx/sites-available/mediadeck-{node.name} /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "==> 6/6 安装负载探针与计量代理"
curl -fsSL {shlex.quote(panel + '/agent/loadprobe.py')} -o /opt/mediadeck-agent/loadprobe.py
curl -fsSL {shlex.quote(panel + '/agent/flowmeter.py')} -o /opt/mediadeck-agent/flowmeter.py
curl -fsSL {shlex.quote(panel + '/agent/meterd.py')} -o /opt/mediadeck-agent/meterd.py
mkdir -p /etc/mediadeck /var/lib/mediadeck
install -m 600 /dev/stdin /etc/mediadeck/signing.json <<'{sign_end}'
{sign_config}
{sign_end}
install -m 600 /dev/stdin /etc/mediadeck/report.token <<'{token_end}'
{report_token}
{token_end}
cat > /etc/systemd/system/mediadeck-loadprobe.service <<'{probe_end}'
{probe_config}
{probe_end}
cat > /etc/systemd/system/mediadeck-meterd.service <<'{meter_end}'
{meter_config}
{meter_end}
systemctl daemon-reload
systemctl enable --now mediadeck-loadprobe.service
systemctl enable --now mediadeck-meterd.service
sleep 2

echo
echo "==> 自检"
curl -fsS http://127.0.0.1:{LOADPROBE_PORT}/load >/dev/null && echo "    [OK] 负载探针" || echo "    [!!] 探针未响应"
curl -fsS http://127.0.0.1:{METERD_PORT}/healthz >/dev/null && echo "    [OK] 计量代理" || echo "    [!!] 计量代理未响应"
curl -fsS {shlex.quote(f'https://{host}/healthz')} >/dev/null && echo "    [OK] nginx 对外服务" || echo "    [!!] nginx 未响应"
echo
echo "==========================================================="
echo "节点 {node.name} 安装完成。回面板「节点管理」，状态应变为「可用」。"
echo "==========================================================="

{report_home}"""



# Match the four prefixes registered by main.py. Scope case-insensitivity to
# the playback endpoint: capturing other prefix spellings would turn Emby's
# otherwise valid requests into the panel's 404. Anchor the endpoint so video
# management, subtitles, HLS segments and similarly named paths stay at Emby.
_PLAYBACK_PATH_RE = r"^/(emby/)?[Vv]ideos/[^/]+/(?i:stream|original)(\.[A-Za-z0-9]+)?$"


def emby_frontend_snippet(panel_url: str, emby_url: str, server: str = "caddy",
                          emby_origin_url: str = "") -> str:
    """Front-door rule that puts the panel on the real playback path.

    This is the answer to "how does my existing Emby domain dispatch to nodes":
    the operator keeps one public Emby hostname, and only stream requests are
    handed to the panel. Everything else — web UI, metadata, images,
    transcoding — must keep going straight to Emby.
    """
    panel_host = panel_url.rstrip("/") or "http://127.0.0.1:8300"
    emby_host = (emby_origin_url or emby_url).rstrip("/") or "http://127.0.0.1:8096"
    emby_domain = _host_of(emby_url) or "emby.example.com"
    panel_authority = urlparse(panel_host).netloc

    if server == "nginx":
        return f"""# nginx — 加到 {emby_domain} 的 server 块里，放在 location / 之前
# GET/HEAD stream/original under /emby/Videos, /emby/videos, /Videos and /videos.
# Use a private tunnel or verified TLS between this host and the panel.

location ~ {_PLAYBACK_PATH_RE} {{
    # Method routing happens before proxying. A named error_page target keeps
    # the original method, body and URI; no request reaches the panel first.
    if ($request_method !~ "^(GET|HEAD)$") {{ return 418; }}
    proxy_pass {panel_host};
    proxy_set_header Host {panel_authority};
    proxy_ssl_server_name on;
    proxy_ssl_name {_host_of(panel_host)};
    proxy_ssl_verify on;
    proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Mediadeck-Proxy nginx;
    proxy_set_header X-Mediadeck-Entry $http_x_mediadeck_entry;
    proxy_set_header X-Mediadeck-Entry-Key $http_x_mediadeck_entry_key;
    # Authentication and entry selection must run on every request. In
    # particular, no inherited cache may replay another entry's 302 or 204.
    proxy_cache off;
    proxy_no_cache 1;
    proxy_cache_bypass 1;
    proxy_hide_header X-Mediadeck-Entry-Key;
    add_header Cache-Control "private, no-store" always;
    proxy_redirect off;
    proxy_intercept_errors on;
    error_page 418 500 502 503 504 = @mediadeck_emby_origin;
}}

location @mediadeck_emby_origin {{
    proxy_pass {emby_host};
    proxy_set_header Host $host;
    proxy_set_header X-Mediadeck-Entry "";
    proxy_set_header X-Mediadeck-Entry-Key "";
    proxy_set_header X-Mediadeck-Proxy "";
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_cache off;
    proxy_buffering off;
}}

location / {{
    proxy_pass {emby_host};
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Mediadeck-Entry "";
    proxy_set_header X-Mediadeck-Entry-Key "";
    proxy_set_header X-Mediadeck-Proxy "";
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_cache off;
    proxy_buffering off;
}}
"""

    return f"""# Caddy — {emby_domain} 站点配置
# GET/HEAD stream/original under /emby/Videos, /emby/videos, /Videos and /videos.
# Use core Caddy without a cache handler; preserve entry headers only to panel.
{emby_domain} {{
    @stream {{
        method GET HEAD
        path_regexp stream {_PLAYBACK_PATH_RE}
    }}

    handle @stream {{
        header Cache-Control "private, no-store"
        reverse_proxy {panel_host} {{
            header_up Host {panel_authority}
            header_up X-Mediadeck-Proxy 1
            @fallback status 204 500 502 503 504
            handle_response @fallback {{
                reverse_proxy {emby_host} {{
                    header_up -X-Mediadeck-Entry
                    header_up -X-Mediadeck-Entry-Key
                    header_up -X-Mediadeck-Proxy
                }}
            }}
        }}
    }}

    handle {{
        reverse_proxy {emby_host} {{
            header_up -X-Mediadeck-Entry
            header_up -X-Mediadeck-Entry-Key
            header_up -X-Mediadeck-Proxy
        }}
    }}
}}
"""
