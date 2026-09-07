# External playback entries

A friend can expose one HTTPS domain for both Emby and the selected streaming
node. For example, a request through `https://friend.example.com` receives:

```text
https://friend.example.com/_n/edge-a/s/main/Movies/Demo.mkv?r=...&u=...&e=...&k=...
```

The friend strips only `/_n/edge-a` and proxies to the fixed HTTPS upstream for
`edge-a`. The node sees its original `/s/main/...` path and the original signed
query. Its signing key, expiration check, user attribution, rate limit and
media cache are unchanged. Neither friend proxy receives the node signing key.

This feature is disabled by an empty entry registry on upgrade. It requires
nodes with HTTPS base origins and media URL prefixes under `/s/`. A node URL
outside `/s/` retains the ordinary direct target; template export refuses a
fleet with incompatible mappings instead of emitting a misleading proxy.

## Register an entry

In **Settings → 外部反代入口**, enter an ID and HTTPS origin, then click
**登记**. Choose **Caddy** or **nginx** and click **生成配置** on that row.
The generated text contains that entry's credential; share the private file
only with its owner. **复制配置** uses the browser clipboard, falls back to
legacy copy when needed, and selects the whole text for Ctrl+C / Cmd+C if
clipboard access is unavailable. Nothing is written to browser storage or the
console. **换凭据** and **删除** require confirmation and clear old exports.

The UI blocks duplicate writes and rejects stale pages instead of replacing
someone else's registry. Reload after a conflict, review the current entries,
and repeat the intended action. A pending export cannot replace a newer one
or repopulate the page after a local rotation/removal.

The administrator-authenticated API is also available:

```http
PUT /api/settings/integration
Content-Type: application/json

{
  "external_entries": [
    {"id": "friend-one", "origin": "https://friend.example.com"},
    {"id": "friend-two", "origin": "https://another-friend.example.com"}
  ]
}
```

Read `GET /api/settings/integration` before editing, and include its opaque
`external_entries_revision` in the PUT alongside `external_entries`. The
server atomically compares that revision and returns HTTP 409 without changing
any fields if another writer won first. Rotations also change the revision;
ordinary round trips and unrelated settings edits do not. The UI always sends
this precondition. Legacy API clients may omit it for compatibility, but their
whole-list writes are unguarded and should be updated.

`external_entries` replaces the list, so preserve entries that should remain.
Omitting the field keeps the list; an empty list removes it. Existing settings
forms continue to work.
IDs are stable labels, up to 40 ASCII letters/digits/underscores/hyphens, starting
with a letter or digit. Origins are literal HTTPS DNS origins (optional port),
with no userinfo, paths, query, fragment or wildcard. Duplicate IDs/origins and
the configured official Emby origin are rejected atomically with HTTP 422.

The panel generates a separate high-entropy proxy credential for each new
entry. It is stored with the existing mode-600 runtime settings document, never
in the repository. Ordinary API responses return only `proxy_key_set`, not the
credential. Partial saves, restarts and read/edit/write round trips preserve it.
Set `rotate_proxy_key: true` on an entry to rotate; export and install that
entry's template again afterwards. A removed or rotated credential immediately
stops selecting that entry. Already-issued node signatures retain their normal
expiry; rotation is not node-link revocation.

A new friend needs a registry entry and its exported Caddy or nginx
configuration; application code and the origin's generic forwarding rules do
not change.

## Export the friend's configuration

Set the official HTTPS Emby origin in `integration.emby_public_url`, then use:

```http
GET /api/integration/frontend?server=caddy&entry=friend-one
GET /api/integration/frontend?server=nginx&entry=friend-one
```

This admin-only endpoint returns `{ "server": "caddy" | "nginx", "config": ... }`.
**The explicit export contains the entry credential.** Responses use
`Cache-Control: private, no-store`. Save `config` directly to a private file
(mode 600, readable by the friend's proxy process). The UI explicitly displays
it for copying; do not log or paste the response into a public ticket/chat. Do not give the friend an
administrator or Emby API credential. The standalone helper reads admin
credentials from a local protected JSON file and creates a new output file:

```sh
python3 tools/export-entry-proxy.py \
  --panel https://panel.example.com \
  --credentials /secure/panel-admin.json \
  --entry friend-one \
  --server caddy \
  --output /secure/friend.Caddyfile
```

The credentials file has `username` and `password` fields, created using the
host's private credential setup. Values are read only inside the process;
none belong in command arguments, URLs, shell variables or logs. The helper
refuses HTTP, redirects, public-readable credentials and overwriting a file.
Use `--server nginx --output /secure/friend.conf` for nginx. Transfer the
exported file through an existing private administrator channel.

For nginx **1.25.1 or newer**, put the whole exported file in an `http {}`
include such as `/etc/nginx/conf.d/friend.conf`. It contains `upstream`, `map`
and `server` blocks; do not paste it inside an existing `server {}`. Exported
variables and upstream names are scoped to the exact entry ID and origin,
so different friends' files can coexist. Keep one current export per entry.

Provision a certificate first and adjust the two active `ssl_certificate`
paths, then run `nginx -t` before reloading. The export cannot obtain a
certificate, and a fresh machine without those files will fail validation.
`https://friend.example.com:8443` generates `listen 8443 ssl` (IPv4 and IPv6)
and a hostname-only `server_name`. If an existing load balancer maps external
and internal ports differently, adjust the listener to that deployment.
Caddy keeps its normal automatic HTTPS behaviour.

An example without credentials is in [examples/friend.Caddyfile](examples/friend.Caddyfile).
The API exports a complete file from the current node registry, including
nodes temporarily disabled for scheduling so existing links remain usable.
If nodes are added, renamed or have their base origins changed, re-export the
templates. An additional friend alone does not require updating other friends.

The generated file:

- Defaults all ordinary paths, including authentication, metadata, PlaybackInfo,
  Web UI, transcoding and WebSocket, to the fixed official Emby HTTPS origin.
- Overwrites the two entry assertion headers with fixed registered values;
  arbitrary incoming `Host`, `Forwarded`, `X-Forwarded-Host` and assertion
  headers cannot choose a signed redirect domain.
- Routes the raw `/_n/<known-node>/s/*` prefix to that node's fixed HTTPS
  origin and removes only `/_n/<known-node>`. Both engines preserve percent
  escapes (including `%3F`, `%23`, `%25`, `%20`), double slashes and the exact
  query. Literal filename `?`, `#` and `%` must be URL-encoded by the client.
  Caddy uses `uri path_regexp`, since `strip_prefix` cleans double slashes.
  nginx uses the raw `$request_uri` suffix and a fixed named upstream; a
  literal `proxy_pass .../s/` would normalise and re-escape the path.
- Bare `/_n`, `/_n/`, unknown nodes, non-media reserved paths, and dot-segment
  traversal (including `..%2f`, encoded dots and encoded namespace aliases
  such as `/_n%2f` or `/%5f%6e/`) return 404 locally without
  contacting Emby or a node. No normalisation may escape into the Emby fallback.
  nginx accepts 12KB paths with its explicit 32KiB request-line buffer; larger
  lines return 414 before contacting an upstream. Caddy's default limit is
  larger. Oversized requests are never truncated into a different target.
- Sets upstream Host and TLS SNI explicitly; certificate verification stays on.
  Range, If-Range, methods, bodies, query order/escaping and WebSocket upgrades
  pass through. No URL/query/header supplies an upstream. nginx needs no
  runtime resolver because each named upstream is fixed when loading config.
- Removes entry keys, Emby credentials, Authorization and cookies before node
  requests. Only the signed query is needed there.
- Uses core Caddy without a cache handler or nginx with `proxy_cache off`, and
  marks responses private/no-store. nginx disables inherited access logging.
  Do not enable header/debug logging or add a CDN/response cache on these routes.

## Connect the official Emby front door once

The friend's key is a scoped **entry assertion**, not Emby authentication. The
panel requires both its matching entry ID/key and the caller's existing Emby
item authorization before emitting an entry-local node URL. Possession of an
entry key alone cannot grant playback access. Missing, duplicate, incorrect or
unknown assertion headers use the existing direct-node/origin behaviour.

The official front door must forward the assertion headers unchanged **only to
the panel's video decision routes**. The application compares the key with the
registered entry and takes the target origin from that record. It does not
trust source IPs or Uvicorn's possibly rewritten `request.client` as proof of
entry identity, nor derive destinations from Host/X-Forwarded-Host.

```text
client -> friend's HTTPS Caddy/nginx (injects its entry ID/key)
       -> official Emby HTTPS nginx
          -> protected panel connection -> Emby authorization + node scheduling
          <- 302 to the registered entry /_n/<node>/s/... with original signature
client -> friend's Caddy/nginx -> selected node HTTPS /s/... -> Range 206
```

Export `GET /api/integration/frontend?server=nginx` for the generic origin rule.
Review its panel connection and local Emby upstream on the actual Emby host:
`integration.panel_public_url` selects the panel and `emby.url` supplies the
fallback upstream. A same-looking loopback address on two different machines
is not proof of connectivity. Use a private tunnel or verified TLS across
hosts; never transmit the proxy key or Emby credentials over public HTTP.
The snippet enables upstream TLS certificate verification when using HTTPS.
Keep the existing origin's TLS settings and merge locations into its server
block; do not replace a production site wholesale.

Both origin templates intercept **only GET/HEAD** for the `stream` and
`original` endpoints (optionally followed by a single container extension)
under the four implemented prefixes (`/emby/Videos`, `/emby/videos`, `/Videos`,
`/videos`). The path is end-anchored: subtitle management, AdditionalParts,
HLS/DASH paths, similarly named endpoints and unsupported prefix spellings go
straight to Emby. POST/DELETE/PUT/PATCH/OPTIONS also stay at Emby, including
when sent to a stream/original path. Their method, body, raw query and caller
authentication headers are preserved without asking the panel first.

Caddy uses a combined method/path matcher. nginx checks the method before
proxying and routes other methods through an internal 418 to its named Emby
upstream; a named location preserves the request method and body. This is a
request-routing guard, not a blanket fallback for a panel 405. An unexpected
405 from an intercepted playback request remains visible.

For eligible playback, nginx sends
`X-Mediadeck-Proxy: nginx`, preserves the assertion and caller authentication
headers, and disables inherited response caching. Signed decisions remain
302; denied access rules remain 403. Non-accelerated requests return internal
418, which nginx's named `error_page` location serves directly from Emby while
preserving the original URI, query and method. That avoids redirect loops.
Other front doors retain the existing 204 fallback contract. The generated
Caddy origin snippet consumes 204 with `handle_response`.

Strip `X-Mediadeck-Entry`, `X-Mediadeck-Entry-Key` and `X-Mediadeck-Proxy` before
ordinary or fallback Emby upstream requests. Neither key nor signed Location
belongs in diagnostic logs. Any outer tunnel, WAF or proxy must also bypass
response caching for video decisions and authenticated API responses. Review
forwarded client-IP handling separately under the existing trusted-proxy
policy; this feature does not change access rules or derive trust from XFF.

## Cache and policy behaviour

Playback authorization, item/media-source metadata and member-rate caches are
partitioned by the authenticated entry ID plus canonical origin; the ordinary
entry has its own namespace. URL wrapping happens after the existing routing
and signing decision on every request, and final redirects are not cached.
Integration updates clear those caches immediately. Shared-cache responses
are prohibited with `private, no-store` and an assertion-header `Vary`.

Scheduling still hashes the media path, independent of entry, and uses the same
health, capacity, enabled-state, pool and bandwidth rules. A friend does not
create a new copy of the node pool. Transcodes, unresolved items, unavailable
nodes, feature-off requests and unauthenticated callers keep their existing
fallback. The diagnostic `/stream` route and PlaybackInfo responses are not
rewritten; actual client video requests are the interception boundary.

## Acceptance and remaining deployment work

Repository tests cover both successful and refused entry selection, four video
prefixes and GET/HEAD, credential rotation/removal, configuration persistence,
encoded filenames, original signed query/rate/user tag, media-source/pool
selection and cross-entry cache isolation. `tests/test_entry_proxy_live.py`
runs real Caddy and nginx against TLS mock upstreams to check signatures,
raw paths/queries, byte ranges, Host/SNI, credential stripping, cache absence,
WebSocket frames and non-GET bodies. Two friends' exports are loaded together;
nginx checks separate files included from `conf.d/` with `nginx -t`.
`tests/test_entry_ui.py` runs Chromium against the actual JavaScript to check
escaping, mutation/export races, field preservation and clipboard fallbacks.
These are local integration tests, not live-fleet acceptance.

`tests/test_origin_proxy_scope.py` runs the generated **nginx and Caddy** origin
templates against the actual FastAPI panel and a recording loopback Emby
upstream. It checks subtitle POST/DELETE and other non-playback requests reach
only Emby with the original method/body/query/auth, while the four video
prefixes still return GET/HEAD 302 for official and registered entries. Both
the existing transcode fallback and absence of blanket 405 handling are
covered. Set `MEDIADECK_NGINX_BINARY` to an extracted nginx executable to test
without installing or changing a system service; otherwise nginx must be on
PATH. Alternatively set `MEDIADECK_NGINX_DOCKER=nginx:1.27-alpine` to run both
proxy suites using disposable `docker run --network host` containers. All
listeners/upstreams remain on loopback; only temporary test directories are
mounted, with no changes to installed services. Caddy and OpenSSL are needed
for the TLS friend suite, Chromium for browser tests. Cases for unavailable
tools are skipped.

Before claiming a particular friend's deployment works, the operator still
needs to install the reviewed release, register the entry, connect the actual
origin-to-panel decision route, and have the friend install its private
Caddyfile or nginx configuration with working DNS/TLS and reachability to the official origin and
**every configured node**. None of those operations is performed by this PR.

Verify without logging credentials or signed URLs:

1. Authenticate through that friend using a real authorized Emby client. A 401
   proves rejection only; it does not prove any 302 or node playback.
2. Confirm a real direct-play GET/HEAD receives 302 to that same registered
   origin and the selected `/_n/<node>/s/...` path. Then confirm actual node
   Range 206 and correct bytes after stripping the wrapper.
3. Repeat through a second entry and the official origin, in alternating order
   on the same media/user/device, including warm caches. Unknown/forged entries
   must never cause a signed URL to an arbitrary domain.
4. Exercise each active node and pool, seeking/suffix ranges, expiry/tampered
   signatures, an invalid Emby token, transcoding and WebSocket playback events.
5. Verify signatures with real successful requests. Hashing a complete nginx
   signing directive cannot establish whether two signing keys are different.

Rollback the feature by removing `external_entries`; ordinary direct-node
routing resumes. Revert the reviewed front-door locations if needed. Existing
node signing keys, scheduling settings, media and DNS require no change for
application rollback.
