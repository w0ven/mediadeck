# Aggregate direct-video throttling (public deployment template)

The panel's bandwidth unit is **decimal MB/s**: ordinary baseline
120000 kbps = 15000000 B/s; whitelist baseline 280000 kbps = 35000000 B/s.
Bandwidth cards and individual increases are additional entitlements: the
signed **effective** member bandwidth, including increases above 35 MB/s, is
the aggregate node-local budget shared by all of that account's video
connections. Missing or signed-zero rates fall back to 15000000 B/s. A member
with an older lower-rate link and a newer higher-rate link is conservatively
paced at the lowest **active** signed rate. Old higher-rate links alone may
remain valid until their existing signature expires; there is no immediate
revocation or multi-node global limiter.

## Release integration

The repository ships public-only **examples** in `deploy/nginx/stream-rate-*.example.*`.
They are not drop-in replacements for an existing site: render them against
privately supplied deployment configuration in an approved PR → Release/tag →
checkout deployment, then syntax-test and deploy through the release process.
No repository artifact should contain deployment-specific endpoints, paths,
identity, media titles or credentials. Do not hand-edit a deployed site as a
final hot patch. This source change does not deploy or restart anything.

Run exactly one relay process for **all** reader pools on each node. For every
signed video location, route through its own relay pool index rather than
directly to its reader; remove nginx's `limit_rate` because it is **per request**.
Set `X-Mediadeck-Rate-Key` from nginx's verified member-tag map and
`X-Mediadeck-Rate` from its signed-rate map. nginx must overwrite any
client-supplied copies. Keep the existing signature `auth_request` and
fail-closed deny guard, connection limit, speed and dispatch access logs,
announcements, billing/metering include, Range/HEAD/GET behavior, upstream
timeouts and response headers. Never fall back to direct reader traffic if
the shared relay fails. Update the map's absent/zero-rate fallback to exactly
15000000 B/s without deleting any other HTTP-context maps or log formats.

Existing deployment group values are private operational state, **not** part
of this repository. At an authorized release cutover the operator must update
the ordinary and whitelist groups via the **existing group update pathway**
to 120000 and 280000 kbps; this task does not touch a live database. Verify
fresh signed URLs, purchases/increases, old mixed signed rates, single/two/four
parallel Range transfers, isolated accounts, speed/billing logs and reader
failure handling before declaring the deployment complete.

The relay's 35s upstream socket timeout is per blocking read/write, **not** a
whole-response lifetime. An isolated test of four mixed-rate Ranges completed
past one second with that socket idle timeout shortened to 50ms. Genuine
reader stalls remain subject to the existing frontend upstream idle timeout.
