# Playback signature v2 — coordinated upgrade required

v0.30.0 replaces the ambiguous v1 concatenation signature. **There is no v1 acceptance or downgrade path.** Updating the panel alone is not sufficient: prepare every serving node, switch node verification, then start the upgraded panel. Old links will stop working and clients may need to restart playback.

## Protocol and proxy compatibility

The digest value is `v2.` followed by an unpadded base64url HMAC-SHA256. The authenticated UTF-8 message is compact JSON:

```text
["mediadeck.file-url",2,decoded_path,expiry_integer,rate_integer,user_tag_string]
```

Use `ensure_ascii=False` and separators `(',', ':')`. The secret is the existing raw node secret, not an nginx-escaped representation. Protocol/version, explicit JSON types and field boundaries prevent the former URI/rate/identity repartition attack.

Existing domains, media paths and query argument **names** are preserved (`r/u` and each node's configured `k/e`, `md5/expires`, etc.). Transparent reverse proxies keep forwarding the original encoded media path and full query; they do not sign URLs or add verification headers. Registered `/_n/<node>` stripping and pinned stream-domain behavior remain unchanged. A proxy that independently signs, validates, filters/rewrites authentication parameters or changes the file path needs separate adjustment.

The verifier rejects v1, altered MAC/version/path/rate/tag/expiry, expired links, duplicate or aliased signature fields, noncanonical integers, control characters and dot-segments. It compares the once-decoded UTF-8 request path with nginx's actual file URI. GET and HEAD authorize the same file; Range does not change its identity.

For real authenticated playback, an unresolved caller returns to the configured Emby origin rather than receiving an anonymous uncapped direct link. No member identity is guessed.

## Existing probe, independent verification

Verification runs in the existing standalone `agent/loadprobe.py` HTTP process on port 9800. No backend import, new package, additional daemon or new port is required. Verification does not depend on successful load sampling or an enabled collector.

Create a private mode-600 JSON file:

```json
{"version":2,"secret":"EXISTING_NODE_SECRET","arg_digest":"k","arg_expires":"e","metering":false,"metering_port":9801}
```

Default: `/etc/mediadeck/signing.json`. An explicit `--signing-config` or `LOADPROBE_SIGNING_CONFIG` selects another private path. Pass only the file path via CLI/environment, never the secret. Configuration is read at startup; restart the existing probe after atomically installing it. Missing/invalid configuration leaves load reporting available but verification fails closed.

- `metering:false`: valid signatures return 204 without contacting meterd. Do not enable a collector solely to support signatures.
- `metering:true`: only after HMAC verification succeeds, the probe sends the authenticated user tag and nginx-captured connection tuple to the existing loopback meterd. Explicit meterd denials remain denials; its previous network/502/503/504 failure tolerance applies only to already-authenticated requests.

## Nginx integration

Use `provisioning.nginx_signing_guard()` and `nginx_signing_endpoint()` as the authoritative generated snippets. For existing installations patch only the verification integration, **not the whole site**: actual alias roots may differ from the panel's provisioning defaults.

1. Remove `secure_link`, `secure_link_md5` and their v1 conditional checks from protected media handling.
2. Add `merge_slashes off;` to the streaming server so the authenticated path and actual URI agree.
3. In each protected **parent media location**, capture `$uri`, `$request_method` and the connection tuple using the generated guard, then use the sole `auth_request /_mediadeck/verify`.
4. Add the generated `internal` verification endpoint, forwarding only controlled headers to `http://127.0.0.1:9800/verify`. Client-supplied verification headers must not pass through.
5. Replace the existing media `auth_request /_mediadeck/register`; do not stack a second auth_request. Preserve the original rate, concurrent-connection, alias and known-deny-map rules.
6. Signature verification must have **no** `error_page` route to an allow/file-serving location. Do not inherit meterd's old fail-open endpoint as a signature fallback. Probe failure must not serve the file.

The path/method capture belongs inside the parent media location, not the server or verification subrequest: subrequest rewriting must not replace the authenticated file path.

## Safe rollout

1. Back up settings, node configuration and operational databases without stopping playback. Pin a WAL read snapshot when making a chunked SQLite backup under continuous writes.
2. Prepare the new probe and private configuration, and validate full candidate nginx configuration while retaining the original alias/domain/rate/deny rules.
3. Restart existing probes and verify the new local route/configuration. Do not change node enabled states or metering defaults.
4. Reload all prepared nodes with the v2-only gate, verify that independently generated v1 links are rejected, and switch the panel to v2 issuance promptly. This intentionally has a brief incompatible-signing window rather than a vulnerable dual-protocol grace period.
5. Verify real new GET/HEAD/Range media requests, tamper rejection, official and registered proxy paths, runtime source revisions, preserved account/entitlement configuration and continued observation/reporting.
6. Upgrade the audit-corrected edge reporter with the panel's atomic byte/cursor API; do not use the old non-advancing intermediate-batch reporter contract as a fallback.

Do not roll verification back to v1 to recover availability. Keep v2 fail-closed and correct the deployment; any alternative routing/containment decision must preserve the security boundary. Nginx reload does not retroactively revalidate bytes already authorized on an established transfer.

## Regression evidence

`tests/test_signing_v2.py` contains independent HMAC/v1 vectors. `tests/test_signing_v2_nginx.py` runs real TLS nginx, standalone probe, optional meterd/nft and the existing transparent nginx entry template in an isolated network namespace. It covers both standard argument-name pairs, media roots, encoded/Unicode paths, GET/HEAD/Range, field tampering, v1 rejection, disabled/enabled/down collectors, missing configuration, unavailable verification, forged internal headers and POST rejection.

The former audit security XFAIL is now a normal passing regression. Unavailable Caddy environments and unexecuted external integrations remain explicit limits; template/unit checks are not a claim that every third-party deployment was tested.
