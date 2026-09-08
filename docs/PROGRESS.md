# Progress Ledger

Newest entries first. Every working session appends one entry.

---

## 2026-09-09 — local workspace and durable watch-time rebuild

- Six primary workspaces retain 25 working pages in secondary navigation.
  Dashboard contains key status, actionable follow-up and one live playback
  surface; recent arrivals live in the library rather than competing on home.
- System/Bot settings use category navigation, scoped saves, expandable advanced
  fields, validation, inline failures and unsaved-change guards. A save does not
  reload or discard other drafts; preview/export inputs are not dirty settings.
  Testing saved Bot credentials no longer implicitly saves or enables the Bot.
- Member and account menus expose Telegram reassignment. The old account may
  create a target-bound, hashed, 30-minute handoff; the new account verifies its
  Emby password before group review. Direct verification from a new Telegram
  remains available without access to the old Telegram. Failed group notices
  can be retried without duplicate cards. Web review is retired with HTTP 410.
- Sampled watch intervals, per-user totals and resumable checkpoints persist in
  SQLite. Checkpoints and samples commit together; restart gaps, pause transitions
  and oversized unobserved gaps are not backfilled. Finishing or pruning history
  does not count sampled time twice or reduce recorded cumulative totals.
- Rolling windows clip sampled intervals. Old whole-session records spanning a
  window boundary are explicitly incomplete, never prorated into invented time.
  Web member details, Bot usage and rankings share these statistics. Grouped
  queries keep leaderboard work independent of member count.
- Operational traffic display uses the measured monthly ledger and credits;
  unavailable daily traffic remains null. Independent direct-link logs and
  bitrate estimates no longer appear as member watch/usage totals.
- Validation: full suite 1196 passed, 56 skipped. Real local-mock Chromium covers
  all 25 pages, independent saves, failure retention, cancelled navigation and
  refresh, logo/group settings, retired review links, incomplete watch history,
  desktop/mobile layouts and mobile backdrop/Escape dismissal. Existing live,
  member and entry browser safety checks remain intact; final 8 relevant checks
  passed after mobile dismissal changes. JavaScript syntax and diff checks pass.
- Ruff 0.16.6 reports 39 existing findings (40 in the same HEAD baseline files),
  with no new findings. The repository is not claimed globally lint-clean.
- Delivery is local only: no upstream push, deployment, production configuration,
  Telegram messages, legacy import or production data modification this round.

---

## 2026-09-08 — local user-management rebuild integrated

- Integrated membership lifecycle/UI, non-destructive live DOM updates, node
  measured accounting, durable multi-node policies and runtime nginx deny-map
  publication. No live deployment, upstream push, or legacy-user migration.
- Full integrated suite: 1068 passed, 56 skipped (optional cases); this includes
  the actual Chromium member/live flows and isolated kernel/nginx regressions.
- Runtime deny validation starts nginx with an empty map, applies a new policy
  through the daemon publisher, verifies 403/200 after stopping meterd, then
  verifies dynamic unblock. A written file alone is not claimed loaded.
- Policy tracking separates sent from node-applied revisions. The stable socket
  identity includes the nginx worker pid and connection number.
- Final project-config lint and JavaScript syntax checks pass. Behaviour-neutral
  lint cleanup was followed by 35 metering/policy/probe regressions passing.
- Rollout still requires operator approval for node components/configuration and
  an explicit measured-baseline cutover. Unregistered origin/transcode paths
  must not be described as measured node traffic.

---

## 2026-09-08 — member browser integration and capability preservation

- Ran the actual index/script order in Chromium through Playwright, using only
  mock API/SSE data. Covered query entry, filters, sorting, paging/history,
  detail tabs, operation failures, live updates and editor/selection/scroll state.
- Fixed deletion confirmation: self-only and inviter-cascade are separate
  buttons; cancelling either performs no DELETE. Failed objects remain selected.
- Restored permission/role editing, password reset, playback stop, manual status,
  quota reset, Telegram unbind, device removal and points adjustment instead of
  replacing existing management capabilities with read-only JSON.
- Restored shared bandwidth presets and policy preview used by the group page.
  Explicit unlimited expiry overrides remain null when editing, not inheritance.
- Same-page detail/tab URL changes keep the live stream working; late detail
  responses cannot overwrite a newer selection. Focused editors no longer
  suppress unrelated member status updates.
- Added selectable sort/advanced filters and corrected sorting to use the same
  effective expiry and Emby activity timestamp shown to the operator.
- Validation: 45 relevant tests passed, including real browser flows and group
  regressions. Playwright is a dev-only dependency; no browser or service
  deployment is performed. Final measured-quota integration remains separate.
- Member rows and detail now distinguish measured quota, legacy estimates and
  unknown/zero readings. Metering preview shows node coverage and a paged ledger;
  activation requires an unchecked-by-default baseline acknowledgement plus
  explicit confirmation. Browser tests verify both cancellation gates.

---

## 2026-09-08 — live updates preserve the page (local development)

- Confirmed the original browser failure: every SSE event called a page loader,
  replacing the node form and losing unfocused edits. The new Chromium
  regression failed on that exact assertion before the change.
- Dashboard, nodes, intake, pipeline, tasks and mounts now paint from pushed
  snapshots using stable-key DOM patches, not their navigation loaders. Keep
  input/caret, selection, scroll, mounted selectors and enrollment previews.
- Added route-aware generation guards and reconnect-timer cleanup; old responses
  and closed EventSources cannot overwrite a newer live page. Query-only hash
  changes and browser navigation keep the full route.
- Added real overview/latest/dispatch SSE producers rather than repeatedly
  refetching unrelated dashboard and node REST endpoints from the browser.
- `registerLiveUpdater(page, topics, handler)` and `pageContext` are the contract
  for the separately developed member module. Unknown bandwidth is not shown as
  zero; paused playback no longer hides measured network activity; user-scoped
  figures are labelled when the source supplies that scope.
- Validation: 151 targeted tests passed, including real isolated Chromium and
  provider/REST parity, plus ruff and JavaScript syntax checks. The old intake
  source assertion now distinguishes navigation placeholders from push updates.
- Not deployed. Member lifecycle, final metering fields and node enforcement
  are separate development packages and are not claimed complete here.

---

## 2026-09-08 — member lifecycle + admin UI
**Done**
- Group switch default is `keep`: existing `expires_at` and personal overrides
  stay put; timed groups no longer silently rewrite 180 days into 30. Permanent
  accounts require an explicit `expiry_policy` (`keep` / `apply_group` / `clear`
  / `set`) via group-preview + POST `/group`.
- Renew extends from the effective expiry and writes `expires_at_override` when
  that layer is present; permanent accounts are refused rather than converted.
- Entitlement, Emby presence, and policy sync are separate observation fields.
  `enforce_now` returns an envelope; fingerprint hits compare every MANAGED_KEY.
  Remote failures are not reported as `ok`/`deleted:true`.
- Delete is self-only by default. `cascade=true` requires exact `confirm_ids`.
  Emby is deleted first; a confirmed remote failure keeps the local row and is
  retryable. Bot `/rm` has separate `rm_self` / `rm_cascade` buttons.
- `GET /api/members` paginates after decorating the full set; unmanaged is
  computed from all known ids. Compact SSE topic `members` feeds local UI
  updates.
- New `members.js` page: real pagination, split status columns, action
  envelopes, `registerLiveUpdater('members', ['members'], handler)` with
  `renderView` / `data-live-key` / `data-live-preserve`. Does not wrap `go()`
  or modify `app.js`.

**Tests**
- New `tests/test_member_lifecycle.py` and `tests/test_members_ui_contract.py`.
- Existing cascade / membership / admin-command tests updated for the new
  defaults. Targeted pytest + ruff run in this session.

**Next**
- Merge with the live-ui shell (`pageContext` / `renderView`) for browser
  verification of query routes and differential updates.

---

## 2026-09-08 — measured flow core + idempotent panel ledger
**Done**
- Node core `agent/flowmeter.py`: dedicated `inet mediadeck_meter` LOOKUP-only
  4-tuple counters (IPv4+IPv6), sync register→allow, mixed-identity refuse,
  `ss -K` on the full tuple, durable spool keyed by boot_id/seq/generation.
  Import and tests do not touch host nft; `enable()` is explicit.
- Panel `MeasuredMeteringService` + SQLite watermarks / monthly totals.
  Unit is `kernel_outbound_ip_bytes` (not HTTP length). Unknown is `null`.
  Old edge ledger and `traffic_used_bytes` are not imported as a baseline.
- Isolated `unshare -n` test drives the real API: two TCP flows, unregistered
  control, precise kill, close retain, IPv6, tuple reuse, restart pending.

**Next**
- nginx auth_request, deny-list / quota cutover, member snapshot injection, UI.

---

## 2026-09-08 — reconcile member rows against Emby accounts
**Done**
- Member rows never noticed when their Emby account disappeared: enrolment was
  a one-shot manual import, so deleting accounts in Emby left orphaned rows
  behind (139 Emby accounts vs 200 member rows in production).
- `MemberService.sync_emby()` now compares member rows with the live Emby user
  list: flags rows whose account is gone via a new `emby_missing_since`
  column, clears the flag when an account comes back, follows renames, and
  reports unmanaged accounts. Deletion is never automatic -- a member row
  carries the traffic ledger, plan, expiry and notes, so an accidental mass
  delete in Emby must not destroy billing history.
- An empty user list is treated as "Emby unreadable", not "Emby has no users":
  one failed poll cannot orphan the entire member base.
- `purge_orphans()` deletes only rows already flagged, so an operator acting on
  a stale page cannot remove a member whose account is present right now.
- Endpoints: `GET /api/members/emby-sync` (read-only preview),
  `POST /api/members/emby-sync` (apply, optional `enroll_new`),
  `POST /api/members/purge-orphans` (explicit ids only). The background loop
  runs the flag/unflag pass every 15 minutes; it never enrols or deletes.
- Chose a separate column over a new status value: the billing state machine
  describes what the operator owes a member, and overloading it would let an
  orphan silently clear itself on the next enforcement recompute.

**Tests**
- `ruff check app tests` clean; full backend suite 1023 passed, 31 skipped
  (baseline 1008 passed with the same skips).
- New `tests/test_member_emby_sync.py` (15 cases): preview writes nothing,
  flagging never deletes, first-seen timestamp is stable across syncs,
  returning accounts clear the flag, renames are followed, an empty user list
  is refused, enrolment only on request, purge refuses unflagged/unknown/
  returned ids, and all three endpoints require auth.

**Next**
- Reverse direction (deleting in the panel removes the Emby account) is the
  actual handover from embyboss and needs its own confirmation flow.

---

## 2026-09-08 — pinned external entries (stream host -> one node)
**Done**
- Added a second external-entry mode for a CDN that can rewrite headers but
  cannot route by path. Such an entry registers a `stream_origin` plus a
  pinned `node`; the redirect becomes `https://<stream>/s/...` with the signed
  path and query untouched, so the operator's friend needs only two hostnames,
  each with one fixed upstream. The existing `/_n/<node>/` path-routing mode is
  untouched and stays the default whenever those fields are empty.
- Scheduling for a pinned entry is restricted to its node, so a redirect can
  never name a node the friend's fixed upstream would 404 on. When that node
  cannot serve the item, the ordinary fail-open passthrough to Emby applies
  rather than a different node.
- Validation requires both fields together, requires the node to exist in the
  pool, and rejects a stream origin colliding with the entry origin, the
  official Emby origin or another entry. A pinned export emits two fixed
  hostnames and no `/_n/` route at all; only the pinned node appears in it.
- The settings page gained stream-domain and node inputs, and the entry list
  shows which mode each entry uses.

**Tests**
- `ruff check app tests` clean; full backend suite 1008 passed, 31 skipped.
  The pre-change baseline was 989 passed with the same 31 skips, so the
  path-routed mode is demonstrably unaffected.
- New `tests/test_entry_pinned.py` (19 cases): redirect shape and signature
  verification for GET/HEAD, percent-encoded paths, the pin winning over
  affinity across repeated requests, fallback when the pinned node disappears,
  refusal of half-configured or colliding pins, a read/edit/write round trip
  preserving both pin and credential, and export contents for both engines.

**Next**
- Register the friend's entry with its stream hostname, hand over the generated
  configuration, then verify a real client 302 -> node Range 206.

---

## 2026-09-07 — preserve entry proxy paths and prevent stale registry writes
**Done**
- Reproduced nine path-contract failures in the initial templates: nginx
  re-escaped encoded filenames, both engines collapsed double slashes, nginx
  refused a 12KB path, and bare/traversing reserved paths could reach Emby.
  Preserve the raw suffix with Caddy `path_regexp` and nginx `$request_uri`
  maps into fixed named upstreams. Four additional live failures exposed
  encoded namespace aliases; reject these, dot segments and reserved fallbacks
  before contacting an upstream. nginx now has an explicit 32KiB line budget.
- Scope nginx maps/upstreams per exact entry identity, including case and
  punctuation differences. Two original same-name Upgrade maps actually
  parsed successfully; the risk was shared definitions, not a guaranteed
  syntax error. Separate exported files now have independent names.
- Generate the registered listen port and active certificate paths, with
  explicit certificate/nginx-version/`nginx -t` prerequisites in config and UI.
- Add atomic registry revision checks (409 on conflict, including rotations).
  The UI preserves other row fields, prevents duplicate mutations, ignores
  obsolete exports, clears credentials during mutations, and supports legacy
  clipboard/manual selection. Escaping and credential storage/logging are
  exercised in Chromium using the actual JavaScript.
- Document both proxy engines and the settings workflow; the protected-file
  export helper now supports `--server nginx` as well as its Caddy default.

**Tests**
- `ruff check app tests`: passed. `python -m pytest tests/ -q`: **1020 passed,
  0 skipped**, four existing framework deprecation warnings (96.10s).
  Set `MEDIADECK_NGINX_DOCKER=nginx:1.27-alpine` for this run.
- Real nginx **1.27.5** (disposable host-network containers) and Caddy **2.6.2**:
  both friends loaded together, TLS-verified fixed upstreams, raw encoded paths
  and queries, signatures/Range/If-Range, credential stripping, WebSocket frames,
  non-GET methods/bodies, reserved/encoded traversal refusal and long-path limits.
- Chromium: **20 browser assertions**, covering injection, field preservation,
  stale pages, duplicate writes, obsolete exports and all clipboard fallbacks.
- Isolated mock uvicorn startup: `/healthz` 200 and anonymous `/api/settings`
  401. No production data, settings or services used.

**Next**
- Review and merge separately. No release, deployment, DNS or host configuration
  changes are part of this revision. Real friend deployment acceptance still
  requires the operator's TLS and origin-to-panel setup.

---

## 2026-09-07 — entry management UI and nginx friend templates
**Done**
- Added an "external reverse-proxy entries" card to the settings page:
  register an entry by ID and origin, generate its complete proxy
  configuration, copy it in one click, rotate the credential and remove the
  entry. Registration re-reads the stored list before writing, so an edit made
  from a stale page cannot silently drop another entry.
- Generated friend configuration is now available for nginx as well as Caddy.
  Both engines are produced from one validated plan, so they cannot drift:
  identical per-node prefix strip, identical refusal of unknown `/_n/` paths,
  the entry credential only on the Emby hop, viewer credentials stripped before
  a node hop, no response cache, and a 600s upstream read timeout so a long
  direct-play connection is not cut by nginx's 60s default.
- `GET /api/integration/frontend?...&entry=<id>` accepts `server=nginx`; it
  previously rejected everything except Caddy. Unknown engine names still 400.

**Tests**
- `ruff check app tests` clean; entry/export/origin-scope suites 103 passed.
- Real nginx 1.27 and real Caddy served the generated friend configuration
  against recording upstreams: a percent-encoded CJK media path with its signed
  query reached the node byte-identical, `Range` survived, entry key/viewer
  `Authorization`/`Cookie` never reached a node, unknown node names and bare
  `/_n/` returned 404 without contacting an upstream, and ordinary paths reached
  Emby carrying the entry assertion.

**Next**
- The official front door on the Emby host still needs the playback-interception
  rule installed; without it a friend proxying the main Emby domain never reaches
  the panel and no entry redirect can happen.

---

## 2026-09-07 — restrict origin interception to playback GET/HEAD (draft revision)
**Done**
- Fixed the origin templates capturing every video API and method. In the
  prior draft, subtitle POST/DELETE reached FastAPI's GET/HEAD-only video
  handler, returned 405 and never reached Emby. New real-proxy regressions
  reproduced that failure on both nginx and Caddy before changing the rules.
- Both templates now share an end-anchored stream/original endpoint pattern
  under the four exact supported video prefixes. Subtitle management,
  AdditionalParts, HLS/DASH, similar endpoint names and other prefix spellings
  go straight to Emby. Caddy combines the path matcher with GET/HEAD; nginx
  routes other methods to its named Emby location before contacting the panel,
  preserving method, body, query and authentication. No blanket 405 fallback.
- Added real nginx/Caddy tests against the actual FastAPI panel and a recording
  Emby origin: subtitle POST/DELETE, other methods even on playback paths,
  non-playback GET/HEAD, a large subtitle upload exceeding nginx's body buffer,
  official/registered entry playback on all four prefixes, ordinary transcode
  fallback and unchanged handling of an unexpected panel 405.

**Verification**
- `ruff check app tests` plus the export helper: clean.
- Full backend suite with **both proxy engines available: 966 passed**, zero
  skipped, three existing dependency/lifespan deprecation warnings. Fourteen
  new regressions execute real Caddy 2.6.2 and nginx 1.22.1; nginx is run from an
  extracted package with isolated config, pid, logs and all temp directories.
- No package installation or system service/config changes. Test proxies use
  loopback sockets and are terminated by fixture cleanup.

**Next**
- Re-review the updated draft; no merge, release or deployment in this task.
- Production and friend-side setup/real-client acceptance remain outstanding
  as described in `docs/EXTERNAL-ENTRIES.md`.

## 2026-09-07 — registered external playback entries (draft PR)
**Done**
- Added an optional HTTPS entry registry to the existing integration settings
  API. Each entry gets a generated proxy credential; ordinary settings return
  only its presence, and partial edits preserve it. Rotation/removal applies
  immediately. Entries and credentials stay in the runtime settings store.
- Authenticate the entry assertion separately from Emby's item authorization.
  Only a matching registered ID/key selects an entry-local
  `https://<entry>/_n/<node>/s/...` target; Host, forwarded hosts and source IPs
  cannot select destinations. Preserve the chosen node and original signed
  path/query, including user attribution, rate and expiry.
- Partition playback authorization, metadata and member-rate caches by entry;
  keep media-path scheduling affinity. Cover all four existing video prefixes
  with GET/HEAD and mark decisions private/no-store. Ordinary/unknown entry,
  access denials and non-direct playback keep their existing behaviour.
- Export per-entry Caddyfiles through the existing admin frontend-config API,
  with fixed HTTPS node upstreams, prefix stripping, explicit Host/TLS SNI,
  credential stripping on node requests and no shared response cache. Added
  a private-file export helper and a credential-free reference template.
- Updated the generated origin proxy rules: cover the actual video paths,
  forward entry assertions only to the panel, disable decision caching, and
  serve non-accelerated requests from Emby without redirect loops. nginx uses
  the opt-in 418 fallback mode; other front doors retain the 204 contract.
- Documented onboarding, transport trust, cache rules, rollback and operator
  acceptance in `docs/EXTERNAL-ENTRIES.md`. No frontend UI changes.

**Verification**
- `ruff check app tests` plus the export helper: clean.
- Full backend suite: **952 passed** (97 new tests), with three existing
  dependency/lifespan deprecation warnings.
- Real Caddy 2.6.2 against loopback TLS mock upstreams: entry-local 302 followed
  by signed Range 206, suffix ranges/HEAD, encoded filenames and unchanged
  query, upstream Host/SNI with certificate verification, tampered signatures,
  alternating entries, uncached 401s, unknown node rejection and WebSocket
  handshake/data frames. Both the reference and origin Caddyfiles also pass
  `caddy adapt`. Caddy tests skip when Caddy/OpenSSL are absent.
- Real Uvicorn mock-mode boot: loopback `/healthz` returned HTTP 200 with
  `{"status":"ok"}`; the test process was then shut down cleanly.

**Next / open verification**
- Review this draft; no merge, tag, production installation or DNS changes.
- Connect the operator's actual Emby front door to the panel over a protected
  hop, register entries at runtime and install each friend's private template.
- Verify real authorized 302 -> node Range 206 for each friend/node/pool,
  including warm-cache alternation and native clients. Local TLS mock tests
  do not prove that an existing production proxy chain or friend is configured.

## 2026-09-06 — every deployment reported itself as modified (v0.24.1)
**Done**
- `backend/mediadeck.egg-info/` was committed to the repository, but it is
  regenerated by every editable install. So the moment the package was
  installed on a deployment host, `git describe` reported `-dirty` — a
  correctly deployed release looked modified, and the genuine signal
  ("somebody edited production by hand") was lost in that noise. Observed on
  the live host immediately after the v0.24.0 deploy, and it explains the
  `-dirty` suffix seen on every deploy before this one.
- Untracked the directory, added `*.egg-info/` to `.gitignore`, and added
  repository-hygiene tests that also assert no virtualenv, database or real
  `.env` is ever tracked — the repository is public, so those matter more
  than the cosmetic version string.

**Verification**
- 855 tests (was 852). `ruff` clean.

## 2026-09-06 — node pool editable from the panel (v0.24.0)
**Done**
- **A new 节点池 page** under 资源: per node, an 参与调度 switch, weight,
  bandwidth ceiling and capacity, alongside the live numbers that say whether
  the change did anything — active streams, egress, utilisation and probe
  age. Pulling a node out of rotation is an incident action, so it takes
  effect immediately: saves go through the settings service, which
  reconfigures the **running** scheduler in place. No restart, no shell.
- **A narrow endpoint rather than the existing node update.** `update_node`
  rebuilds a node from its payload and therefore blanks anything the caller
  omitted — a four-field pool editor posting through it could wipe a node's
  media roots or signing key, which would 404 or 403 every stream that node
  serves. `update_node_pool` touches exactly the four fields and leaves the
  rest of the record untouched, with a test that pins it.
- **The audit entry records the change, not the submission.** A form that
  posts every field would otherwise log an edit that changed nothing, and an
  audit log full of no-ops is one nobody reads. No-op saves write no row.
- **Share is computed over enabled nodes only**, so a disabled node reads 0%
  instead of inflating the denominator and making every other node's share
  look too small.

**Decisions**
- *Weight is stored as capacity, not as a second number.* Capacity is what the
  scheduler actually divides by; a separate weight understood only by the UI
  would eventually disagree with it.
- *Config and live state in one payload.* The page shows what was set beside
  what the fleet is doing; fetching them separately makes them disagree for a
  moment on every refresh.
- *ca1 left disabled.* It was taken out of the pool by hand earlier and stays
  that way — this work only makes that state visible and editable.

**Verification**
- 852 tests (was 824): +28. `ruff` clean. Both directions covered: a disabled
  node is never picked across repeated dispatches (not merely deprioritised),
  and re-enabling puts it back; illegal values rejected and a refused edit
  changes nothing; probe history survives an edit.

**Next**
- Consider switching quota enforcement from the legacy sampled field to the
  measured ledger, once it has run long enough to be trusted.

## 2026-09-06 — traffic measured instead of guessed (v0.23.0)
**Done**
- **`members.traffic_used_bytes` was never a traffic figure.** It is derived
  from sampled playback sessions — playing time × bitrate — so it only sees
  what the media server observes. But playback is served by signed direct
  links straight from the edge nodes, which the media server never sees at
  all. On top of that it resets on a rolling period, so it answers "recently"
  rather than "ever". Read as lifetime usage it makes active accounts look
  idle, which is how a cleanup nearly removed 196 of them.
- **The ledger records bytes the edge actually sent**, per user × node × day,
  parsed from each node's nginx access log. Measured against the live fleet:
  **5.8 TB and 218 distinct users** that the old counter never saw.
- **Idempotent by construction.** Cursors are `(inode, offset)` per file and
  owned by the panel, not the node — the panel is the party that must not
  double count, and a node-side cursor would diverge after any restore.
  Rotation is detected by inode change, truncation by a file shorter than the
  cursor; neither replays counted data. Writes are additive upserts of only
  newly-seen bytes, so a retried batch adds nothing twice.
- **Unattributable bytes are kept, not dropped.** A line whose tag matches no
  member is recorded against the tag with an empty user id and surfaced in
  `/api/edge/status`. Traffic that cannot be attributed still left the
  building, and discarding it would make totals disagree with the nodes for
  reasons nobody could later reconstruct. `relink` backfills those rows once
  the member is known, so history becomes attributable retroactively.
- **Both log formats parse.** Nodes provisioned at different times write
  different shapes, and one began appending `s=$status $uri` mid-deployment.
  Fields are read by name with unknown trailing tokens ignored. Verified
  against every real log on all three nodes: **98.4–99.8% coverage**, and
  every rejected line was checked to be legitimately non-billable (unsigned
  request, zero-byte response, or a truncated final write).
- **Members page**: sortable 直链 7 天 / 30 天 / 累计 columns plus 最近活跃
  from Emby's own `LastActivityDate`. Member detail gets per-node and per-day
  breakdowns. The old field stays but is labelled 旧口径（不含直链）.
- **Node side is purely additive**: one new agent file and one systemd unit.
  Nothing in nginx is touched — in particular nothing near `secure_link` or
  rate limiting — so a mistake here cannot break playback.

**Decisions**
- *Push, not pull.* The nodes' existing probe route drops query arguments
  (verified live), so a pull design would have required editing the nginx
  site that also carries `secure_link` and `limit_rate`. The reporter posts
  outward instead: all three nodes already reach the panel over HTTPS, and
  the change set on a node is two new files.
- *A separate table, not an extension of `usage_daily`.* One holds an
  estimate, the other a measurement. Merging them destroys the ability to say
  which is which — and treating the estimate as a measurement is the entire
  bug being fixed.
- *A dedicated per-node credential*, distinct from the one-shot enrol token
  (rotating the installer's credential must not silently stop accounting) and
  scoped to its own node, so one node cannot post another's traffic. It is
  never included in the node list the settings page loads.
- *Client-side sorting.* A few hundred rows are already in the browser; a
  round trip per header click would make an instant interaction depend on the
  network.

**Verification**
- 824 tests (was 780): +44. `ruff` clean.
- Parser run over every real log on all three nodes before merge.

**Next**
- Node pool management in the UI (v0.24.0).

## 2026-09-06 — three things the intake page got wrong in production (v0.22.1)
**Done**
Deployed v0.22.0 and read it against the live system. It was wrong in three
ways that no amount of tmp-directory testing would have surfaced, because all
three only appear at production scale or during a routine operation.

- **A capped listing was being subtracted.** Outstanding cloud jobs are derived
  as claims minus receipts. Both directories hold ~8k entries against a 4000
  cap, so every claim whose receipt fell outside the window counted as
  unfinished: the page reported **2103 outstanding jobs when the true figure
  was five**. Counting now uses a separate cheap path (one syscall per entry,
  200k cap) and, when even that truncates, `outstanding` is `null` and the card
  shows 未知. A partial listing cannot support a subtraction, and a confident
  wrong number is worse than an admitted unknown — this is the same class of
  error as the traffic figure that nearly cost 196 accounts.
- **The probe-loop alarm fired during a normal library scan.** A full scan
  walks one directory at a time, so it concentrates probes *by construction* —
  observed at 100% on a completely healthy server. As written, the page would
  have been red for the entire duration of every scheduled scan. Concentration
  is now only evidence of a loop when nothing is scanning; during a scan it is
  reported as expected.
- **Queue-age amber fired while the drain switch was deliberately off.** With
  suppression on, a growing refresh queue is the switch working as intended.
  Both false positives train an operator to ignore the colour, which costs more
  than the alert was ever worth.
- Refresh queue depth is now exact regardless of how deep it gets: it is a
  headline number, while only the handful of rows actually rendered are parsed.

**Decisions**
- *Separate counting from listing.* They have different failure modes:
  truncating a display list drops rows nobody misses; truncating a set a count
  is derived from invents work that does not exist.
- *Suppress rather than downgrade.* Both false positives keep their row on the
  page at 灰 with the reason stated, instead of disappearing — the operator
  should still see that concentration or queue growth is happening, just not be
  told it is a fault.

**Verification**
- 780 tests (was 775); `ruff` clean. Each fix has a test pinning the exact
  production observation that motivated it.

**Next**
- Direct-link traffic accounting (v0.23.0).

## 2026-09-06 — intake pipeline on one screen (v0.22.0)
**Done**
- **A new 入库流水线 page answers "why has nothing arrived?" without a shell.**
  A file becomes watchable only after a chain of independent steps — download,
  staging, cloud upload, refresh queue, notification — each a separate process
  with its own state directory and no view of the others. When nothing shows
  up, the fault could be an idle download queue (fine), a stalled upload lane,
  a refresh queue suppressed hours ago, or a media server re-probing one
  directory in a loop. Answering that meant opening shells on three machines.
  Now it is one screen with a red/amber/green verdict at the top.
- **The verdict is conjunctive where a single signal would lie.** "No new item
  for 90 minutes" is a quiet night, not a fault; it only turns red when
  notifications are *also* waiting, because that combination is the one that
  means work is stuck rather than absent. A test pins this specific false
  positive, since a light that cries wolf overnight is a light nobody reads.
- **Probe concentration is the loop detector.** A healthy server probes many
  files across many directories; a wedged one probes the same handful forever.
  Only the grouped view separates those, so probe lines are aggregated by their
  first two directory levels and the top share is compared to a threshold.
- **"Unknown" and "zero" are never conflated.** Every section reports its own
  availability, and a missing directory, a truncated JSON file, an unrecognised
  log format and an unreachable media server each degrade alone. This page is
  read during incidents — exactly when parts of the system are broken — so a
  card that renders 0 for a directory it could not open would be worse than
  useless. Nine tests cover those paths specifically.
- **Collection runs on the plugin timer, not per request.** One snapshot walks
  several thousand files, tails a large log and makes three media-server calls.
  Once a minute that is fine; on every page load of an auto-refreshing page it
  would put that cost on a server already struggling. The API serves the last
  snapshot with its age attached, a failed collection keeps the previous one
  rather than erasing it, and 立即采集 forces a fresh pass.
- **Every path is configuration.** The repository carries no deployment layout:
  seventeen optional env keys, all empty by default, so an unconfigured install
  shows honest "未配置" cards instead of failing. Tests build a whole fake host
  under tmp_path.
- The nav guard in `test_plugins.py` now derives its script list from
  `index.html` instead of naming two files, so a page added in a new file
  cannot silently escape the check. It caught this page before the first
  commit — which is what it is for.

**Decisions**
- *Cached snapshot over live collection.* Freshness is bounded and labelled;
  the alternative charges an incident-time page against a sick media server.
- *Emby log tailed client-side.* The log endpoint ignores `Range` and returns
  the whole file (verified against the deployment), so the response is streamed
  and only the last 512 KB retained — bounded memory without server support.
- *Task matched on `Key`, not name.* Three tasks have "scan" in their display
  name and the name is localised; the key is stable. Tested against all three.
- *Downloader auth is lazy.* The API is commonly bound to loopback with auth
  off, so a login is attempted only after a 401/403 rather than unconditionally.
- *Zero-byte files excluded from lane sizing.* Lanes are pre-created with
  placeholder markers; counting them makes an idle lane look busy.

**Verification**
- 775 tests passing (was 722): +53 for this feature. `ruff` clean.
- Boots in mock mode; `/api/intake` returns a fully populated snapshot with no
  credentials configured.

**Next**
- Direct-link traffic accounting (v0.23.0): `members.traffic_used_bytes` covers
  only 7 days and misses direct-link bytes entirely, so it cannot be trusted
  for any decision about an account.

## 2026-09-05 — navigation grouped by task, and no more blank pages
**Done**
- Navigation regrouped by what the operator is doing rather than by which
  subsystem implements it. Invite quota and claim approvals used to sit under
  "Telegram" because the bot delivers them, and requests under "operations"
  because they are a business feature — so finding either one meant knowing the
  implementation first. Everything about *who may watch* is now under 成员,
  everything about *what there is to watch* under 内容.
- 自动化 no longer holds a single item: the plugin centre and the host task
  monitor are both scheduling, so they share a group. 审计日志 moved beside the
  other two forensic views under 安全 — all three answer "what happened".
- Every page now paints a placeholder before its first await. Nine pages in
  app.js had none, so a slow request left the previous page on screen and a
  click read as having done nothing. 23 of 24 pages covered; the last one
  renders synchronously and has nothing to wait for.

**Next**
- Split `PAGES.settings` (164 lines, seven cards in one function) so editing
  one field does not re-render the whole page.

## 2026-09-05 — media requests, uploader claiming and admin commands
**Done**
- **A request names a film, not a string.** Members send a TMDB link or id;
  free text is refused rather than guessed, because a wrong id is invisible
  until an uploader has spent an evening on the wrong title. The id is also
  what makes two people asking in different words one request: the partial
  unique index on `(tmdb_id, media_type) WHERE status IN ('open','claimed')`
  is the mechanism, and `create()` catches the IntegrityError to answer
  "somebody already asked, it is being handled".
- **TMDB is optional and stays optional.** The owner has no key. With none,
  `lookup()` returns None *without opening a socket* and the request is stored
  under a `#12345` placeholder. A request feature that refuses requests until
  a key is configured is a feature that is switched off. Lookups are cached
  for an hour and hand out copies, so a caller editing the dict it got back
  cannot poison later lookups.
- **One claimer, decided by the database.** `claim()` is
  `UPDATE ... WHERE status='open'` and trusts the rowcount. Read-then-write
  would let two uploaders who tapped at the same instant both believe they
  own the job — the same duplicated 40GB the deduplication above prevents.
  The loser is told who won.
- **The fan-out can be taken back.** Each uploader gets their own message and
  its id is stored in `request_notices`; on a claim, every other message is
  rewritten to "已由 X 接单" with the button removed. A live button on a job
  that is gone is how two people start the same download. This also forced a
  fix to the callback handler: the blanket `answerCallbackQuery` is now
  skipped for `req_*`, because a query may only be answered once and acking
  first swallowed the loser's alert entirely.
- **The quota counts refusals.** `request_used` is charged at creation in the
  same transaction as the insert, so a rejected request still spends the
  slot; deriving remaining allowance from open rows would refund every
  refusal and let a member ask for unavailable titles forever. The month key
  is compared on read, so a rolled-over month is correct the first time they
  ask rather than whenever a job next runs.
- **Only the claimer closes it** (or an admin). Otherwise the requester is
  told their title was handled by somebody who never touched it.
- **Thirteen admin commands, gated on the `admin` role**, re-read on every
  command *and* again when a confirmation is tapped — a dialog can sit on
  screen across a demotion. There is deliberately no separate list of
  privileged Telegram ids: a second source of truth would eventually disagree
  with the one the panel already uses to decide who may log in. `/rm`,
  `/renewall` and `/scoreall` show what they are about to do and do nothing
  until a button is pressed; `/rm`'s preview names the cascaded inviter,
  which is the account nobody typed.
- **`/gift` routes through a new `ShopService.grant()`** rather than
  reimplementing the four grants. An admin's "+50GB" and a purchased "+50GB"
  have to be the same write or the two paths drift.
- **The whitelist group is ensured on every boot**, unlike the starter groups
  which seed once. `/prouser` names a specific group, and that command must
  not fail on a database whose owner tidied their group list — but an edited
  whitelist group is left exactly as they left it.

**Verification**
- 722 tests passing (was 480): +42 TMDB, +49 requests, +97 admin commands,
  +27 bot request flow, +18 groups, +7 digest card, plus the existing plugin
  and telegram suites updated. Every new behaviour is asserted from both
  sides — including that a refused command wrote nothing.
- `ruff check app tests` clean; `node --check` clean on both bundles; all 24
  NAV ids resolve to a `PAGES` entry.
- Two gaps found by the tests and fixed in the code rather than the test:
  `lookup()` promised never to raise but only guarded inside `_fetch`, so a
  transport error escaped to the member's request; and the TMDB key was
  reachable through both browser-facing settings endpoints until
  `integration_public()` was split out.

**Next**
- No way to search TMDB by title from the bot; the member has to find the id
  themselves. A search step would need paging and disambiguation in chat.
- Requests are not linked to the import pipeline, so "done" is an uploader's
  word rather than an observed library change.

**Open questions**
- Should a rejected request refund the slot after all? It currently does not,
  which is right for "no source exists" and arguably harsh for "uploader was
  busy". Splitting the two would need a second refusal reason.

## 2026-09-05 — points: ledger, check-in, transfer, shop and the backpack
**Done**
- **The balance is the ledger, not a column.** `points_ledger` is append-only
  and `balance()` is a `SUM` over it. A stored balance and a ledger are two
  sources of truth that eventually disagree, and when they do there is no way
  to tell which one lied. Deriving it means a wrong balance is always a wrong
  row, and a row can be found and reversed. `balance_after` is written as a
  witness for audits; nothing reads it to answer "how much do they have".
- **Nobody goes into debt.** Spending more than the balance raises before
  anything is written. A negative balance is state the panel would then have
  to display, explain and collect.
- **Transfers and redemptions are one transaction.** Debit and effect are
  written under a single lock. A member charged for a reward that never
  arrived is the failure that ends trust in the whole feature, so it is made
  structurally impossible rather than unlikely — and it is tested by
  sabotaging the second half and asserting the first rolled back.
- **Check-in and transfer are plugins**, so the operator gets a switch and the
  bot keyboard follows it: a button for a disabled feature promises something
  and then explains why it cannot. Both act when a member taps, not on a
  timer — awarding a check-in on a schedule would mean the panel deciding that
  someone showed up — so `run()` only reports. The `checkins` primary key
  `(member, day)` is what makes a double tap impossible, rather than a check
  that has already passed by the time the second write lands.
- **The streak bonus is capped.** At +5/day an uninterrupted year is 1800
  points of pure consistency, and the top of the ledger runs away from
  everyone else. A missed day resets to 1, not 0: they did check in today.
- **Transfer fees are destroyed, not collected.** A treasury account would
  have to belong to someone, and "who may spend the fees" has no good answer
  on a server run by one person. The daily cap is the control that matters: it
  is what limits the damage when an account is taken over.
- **The shop is data.** Four `kind`s map to the four things the panel can
  already grant (traffic, days, bandwidth, invite slots); price, size, stock
  limit and visibility are rows an operator edits, so a promotion is a form
  submission rather than a release. Bandwidth boosts are refused on an
  already-unlimited account *before* charging — billing for a no-op is the
  thing a member notices first. Seeded items ship **disabled**; a live default
  catalogue would sell at prices nobody chose. Orders outlive their item, so
  "why does this member have 500GB extra" stays answerable.
- **Bot menu is two levels.** The flat menu grew a row per feature and a
  member looking for their expiry date had to read past invite codes.
  Now 我的信息 / 🎒 背包 are the entry points, with status, points, lines,
  devices, stats and password below the first, and invites, shop and
  redemption history below the second. The recipient of a transfer is told:
  a balance that silently changes is indistinguishable from a bug.

**Verification**
- 480 tests passing (was 379): +30 ledger, +28 shop, +23 plugins, +20 bot.
  Every refusal is asserted from both sides — that it was refused, and that
  nothing was written.
- `ruff check app tests` clean; `node --check` clean on both bundles; all 23
  NAV ids resolve to a `PAGES` entry. Migration verified against a simulated
  v0.19 database: new tables appear, existing rows survive, old accounts
  start at zero and can earn.
- Two bugs found while writing the tests and fixed in the code rather than
  the test: the points ranking was unreachable whenever the stats service was
  missing (the method returned early), and the recipient notification
  depended on a `MemberService.get` the bot's test double lacked.

**Next**
- No way yet to earn points from watching; check-in is the only faucet
  besides operator adjustment.
- The shop cannot express stock limits across all members, only per user.

**Open questions**
- Should redemptions be refundable by an operator? Currently no — the grant
  is already applied, and reversing traffic already spent is not meaningful.

## 2026-09-05 — registration channels, the invite tree and cascade delete
**Done**
- **Three registration channels replace one global switch.**
  `registration_enabled` could only be open to everyone who found the bot or
  closed to everyone including the people the operator wanted in. It is gone,
  replaced by `allow_admin_grant` / `allow_invite` / `allow_redeem`, each
  requiring something the operator issued: a pre-authorised Telegram id, an
  invite a member spent a slot on, or a card carrying its own group and
  duration. An old stored `registration_enabled=False` migrates to all three
  off, so an operator who had closed the door does not find it open after an
  upgrade.
- **resolve() decides, consume() spends, account creation happens in
  between.** This ordering is the whole design of `registration.py`. A
  credential burned before the Emby account exists is a credential the member
  loses when their chosen username turns out to be taken — they paid and got
  nothing. `consume()` is guarded in SQL (`WHERE uses_left > 0`,
  `WHERE status='unused'`) rather than by a read-then-write, so two chats
  racing for the last use of a code cannot both win.
- **The invite tree.** Members carry `inviter_id` / `register_via` /
  `register_at` / `invite_quota`. `register_via='legacy'` is the honest label
  for the several hundred accounts that predate the bot; inventing a channel
  for them would make the source breakdown a fiction.
- **Cascade delete, one level only.** Deleting a member also deletes whoever
  invited them — an invite is a warranty, not a favour — but the cascade stops
  there. Walking the chain would let one bad account take out an unbounded
  line of members above it, and nobody clicking delete on a single row is
  asking for that. The two removals are audited under *different* actions
  (`member.delete` vs `member.delete.cascade`, the latter naming who it came
  from), because the operator never asked for the second one. Bulk delete
  stays unavailable for exactly this reason.
- **The preview is the promise.** `delete_preview()` feeds the confirmation
  dialog and is asserted against the real deletion in tests: an operator told
  "this removes 1 account" who loses 2 will never trust the dialog again.
- **Redeem cards** get a management page — generate in batches, mask in the
  list, reveal on click, revoke, export CSV. A spent card cannot be rewritten
  as revoked: history is what the member was actually given. The audit trail
  records how many cards were minted, never their values, so a log reader
  cannot harvest unsold stock.
- **Member page reworked** into a product back-office: total / active /
  suspended / new-today, filters for channel, TG binding, expiry and inviter,
  and columns for provenance and downstream count. Invitee counts and inviter
  names are two queries for the whole page, not two per row. "Create Emby
  account" is gone (owner decision): accounts arrive through a channel, and a
  panel-made one has no inviter, no channel and no chat behind it.
- **A v0.13-shaped `redeem_codes` table is renamed aside, not dropped.**
  `CREATE TABLE IF NOT EXISTS` is a no-op against an existing table, so the
  old shape would have survived and broken every query written against the new
  columns. Renaming keeps cards an operator may have sold; dropping them
  cannot be undone. Re-running the migration archives nothing further.

**Verification**
- 379 tests passing (was 285): +34 registration, +30 cascade/migration,
  +19 redeem, +15 bot flow. Every new behaviour has a positive and a negative
  case — notably that a failed account creation spends nothing.
- `ruff check app tests` clean; `node --check` clean on both bundles; every
  NAV id resolves to a `PAGES` entry.

**Next**
- The bot does not yet echo a user's numeric Telegram id on `/start`, which is
  what the pre-authorisation page tells the operator to ask for.
- Invite quota is granted by hand; no rule yet that earns slots automatically.

**Open questions**
- Should a cascade-deleted inviter be notified, or is silent removal correct?
  Currently silent — the operator is the one who knows why.

## 2026-09-05 — every scheduled job becomes a plugin
**Done**
- Plugin framework. A plugin declares what it is, what it can be configured
  with, and how to run; the card, the form, the schedule and the run history
  all derive from that declaration. Adding a job is adding a file, not editing
  the front end. Config lives in the settings store under `plugins.<id>`, run
  history in SQLite (`plugin_runs`) because it is append-heavy and the settings
  document is rewritten in full on every save.
- A plugin that raises never takes the scheduler down. The failure is recorded
  on that plugin's card and the loop moves on — the alternative is one bad
  plugin silently stopping every other job.
- Five built-in tasks: group audit, inactive cleanup, viewing report, rankings
  post, expiry reminder. **None of them can delete an account.** The strongest
  action is suspension, which is reversible from the member page; deleting a
  member takes their history and, in a household, other people's access with
  it, and that is not a decision a timer should make.
- Escalation is gradual where it acts at all: group audit and inactive cleanup
  notify first, start a clock, and only suspend after a configurable grace
  period. Someone who comes back has their clock cleared, so leaving twice does
  not get them suspended off a stale note.
- `telegram_notify_loop` is deleted, not disabled. It kept its own day-key for
  the ranking post, so running it alongside the plugins would post the
  leaderboard twice. For the same reason `rankings_*` and `notify_expiring*`
  are gone from the Telegram settings: two switches for one post is how it goes
  out twice, or stops because the wrong one was flipped.
- Upgrades keep working: `migrate_legacy_telegram_jobs()` carries the old
  settings onto the new cards before the scheduler starts — same hour, same
  chat — then drops the legacy keys in the same write, which makes it
  idempotent. Config already set on a card always wins, so the migration cannot
  overwrite a deliberate edit.
- Two framework fixes found while writing the built-ins. A daily plugin that
  declares an `hour` field now schedules on the *configured* value, otherwise
  the field is decoration and the card lies about when the job runs.
  `Plugin.due_today()` lets a plugin add a calendar condition (weekly on
  Monday, monthly on the 1st) without teaching the scheduler about every
  calendar; it gates scheduled runs only, so the manual button can still test a
  weekly report on a Wednesday.

**Panel**
- New 自动化 → 任务中心 page, generated entirely from the plugin payload:
  switch, auto-rendered form per field kind, save / run now, last result with
  timing and a readable summary, and the last ten runs on demand. Category tabs
  are in place with 积分 showing an empty state, so PR-3 adds plugins rather
  than a page.
- 立即运行 saves first and ignores the enabled switch: the button exists to try
  a job *before* switching it on, and running last-saved values instead of
  what is on screen is not what "试一次" means.
- New 安全 group: 访问拦截 (rules, per-rule enable, block log with the reason
  and rule that matched) and 共享检测 (status plus findings, captioned to say it
  only records — a household on Wi-Fi and mobile data looks identical to a
  shared account, so acting on it would lock out paying members).
- A successful 测试连接 now switches the bot on if it was off, and says so. The
  owner tested the bot, read 连接成功 as "it is running", and walked away from
  a stopped bot. Verification proves the credential works, so there is nothing
  left to decide.
- Deleted the duplicate Telegram form on the settings page. Two forms writing
  the same settings meant whichever page was saved last silently reverted the
  other; settings now links to the bot page instead.

**Tests** — 214 → 285.
- `test_plugins.py` covers coercion per kind from both sides, duplicate ids,
  unknown categories and field kinds, unknown config keys being dropped, a
  refused save leaving the previous value intact, failures being recorded
  rather than raised, concurrent runs being refused, and the scheduling rules
  (disabled skipped, interval respected, daily once per day, configured hour
  winning, `due_today` vetoing and a raising `due_today` not breaking the tick).
  Each built-in has both an acting and a non-acting case.
- Two guards on the front end that had no coverage before: every NAV id must
  have a `PAGES` handler (a mismatch renders 页面不存在 and reads as a broken
  deployment), and only one form may own the bot credential field.

**Next**
- PR-2: media requests. `media_requests` is already in the schema, including
  the partial unique index that makes the same title requested twice while open
  one request rather than two.
- PR-3: points plugins into the tab that is already there.
- Account deletion still has no home. Inactive cleanup deliberately stops at
  suspend; if deletion is wanted it needs an operator-reviewed queue showing
  what else goes with the account, not a timer.

## 2026-09-05 — Telegram bot, and it knows who it is talking to
**Done**
- Menu-driven bot over long polling. A webhook would need a public HTTPS route
  into the panel; polling reaches out instead, so the panel stays reachable
  only from where it already was.
- Two audiences, one entry point. The keyboard is chosen from binding state on
  every render: a guest is offered only a way to link an account, a linked
  member lands on their own status. A fixed keyboard would give half of them
  dead ends.
- Binding uses a 6-character one-time code issued from the panel, valid ten
  minutes, single use, and re-issuing invalidates the previous one. The bot
  never asks for an Emby password: a chat transcript is not a safe place to
  type one.
- One chat speaks for one member, enforced by a partial unique index. Rebinding
  detaches the previous holder and says so in the audit trail — silently moving
  a link would leave the old member believing they still get notifications.
- `bot_token` is stored like the Emby API key: masked on read, `__KEEP__`
  sentinel on save so an unretyped field cannot wipe it, and the audit line
  records *that* it changed, never what to. It is a bearer credential; anyone
  holding it can read every message the bot receives and post as it.
- Daily expiry reminders to linked members, keyed by day rather than by an
  interval so a restart cannot fire the same reminder twice.
- `members` gained `tg_user_id` / `tg_username` / `tg_bound_at` via the
  idempotent column migration, plus `find_by_telegram`, `bind_telegram`,
  `unbind_telegram` and `expiring_within`.

**Also in this branch: member list gets selection and batch actions**
- Checkbox column plus a bulk bar that only appears once something is ticked:
  a row of buttons above an empty selection invites a click that cannot do
  anything.
- `POST /api/members/bulk` with renew / activate / suspend / reset-traffic.
  Each id is attempted independently and failures are named, because partial
  success is the normal case: a member can be deleted in another tab while the
  operator is ticking boxes, and failing the whole batch would make them redo
  work that already succeeded.
- Deletion is deliberately absent from the batch path. A mis-click on a
  checkbox column is easy, and bulk delete is the one action with no way back.
- Renew days are bounded 1–3650 server-side, so a typo of an extra zero cannot
  hand out a decade of access.
- One audit summary line per batch carrying `ok=`/`failed=`, so a 200-member
  action is traceable as one operator decision rather than only as 200 rows.
- Telegram column shows who can actually be reached, with per-member
  bind-code and unbind buttons.

**Also in this branch: shared-account detection**
- `SharingDetector` rides along with the usage sampler, which already holds the
  only live view of who is playing from where, so detection costs no extra
  Emby calls.
- The signal is *distinct networks playing simultaneously*, not session count.
  Finding concurrent sessions is easy; the value is in not crying wolf, because
  an operator who stops reading alerts is worse off than one with none:
  - sessions from one address are one place, however many devices
  - addresses inside a /24 (or /48 for v6) are one place too, so a household
    behind NAT or a dual-stack router is not split into several
  - a network must hold playback for 90s before it counts, so a wifi-to-mobile
    handover that briefly overlaps the old session does not register
  - a network that stops playing is forgotten, so moving house does not
    accumulate places forever
- One finding per account per hour. Repeating the same alert every 15s tick
  would bury the real ones.
- Findings are recorded for a person to judge. Nothing is suspended
  automatically: the cost of being wrong is locking out a paying member over a
  VPN reconnect. A test asserts the class exposes no suspend/block/kick path.
- Fixed while testing: `_last_reported` defaulted to `0.0`, which conflates
  "never reported" with "reported at the epoch". It only worked because a real
  clock is a large number.
- Also fixed: `usage.py` used `contextlib.suppress` without importing it. The
  module imported fine; it would only have raised the first time detection
  actually fired.

**Also in this branch: access rules on the playback edge**
- Two rule kinds, both evaluated against the request asking for media:
  `client` (regex against User-Agent) and `network` (single address or CIDR,
  v4 and v6).
- **Country-level rules are deliberately absent.** They need a GeoIP database
  this host does not carry, and a rule that silently matches nothing is worse
  than no rule at all.
- Fail-open throughout. An empty rule set, a stored pattern that no longer
  compiles, a malformed address, an exception inside evaluation — every
  uncertain outcome resolves to allow. The panel sits on the media path; if a
  rule bug can deny playback it is worse than not being there.
- Excluded users bypass every rule, so one bad regex typed at 3am cannot end
  access for the person who would have to fix it.
- An explicit `allow` wins over any `deny`, so one exception can be carved out
  without rewriting the deny around it.
- Patterns are validated at save time, where a person is present to read the
  error, never at match time on the playback path.
- Rules run *before* routing: a denied request never causes a node to be
  selected or a signature to be minted.
- Refusals are recorded with agent, address (port stripped) and matched rule.
  A block that leaves no trace is indistinguishable from a broken node, and
  the operator ends up debugging the wrong machine.

**Reworked before release: the bot registers, it does not hand out codes**
- Registration creates the Emby account from the chat directly. There is no
  code to copy, because the chat already proves who is asking: the Telegram id
  *is* the identity and is recorded as owner at creation time. Passwords are
  generated and shown once, never typed into a transcript.
- That leaves exactly two cases a requester cannot self-prove, and both go to
  an approval queue instead: claiming an account that predates the bot, and
  moving an account to a different Telegram id.
- Every gate is re-checked at creation, not only when the conversation began:
  a slot can fill or registration can close while someone is still typing a
  username. A test covers exactly that race.
- Group requirement for registration, with the lookup failing *open*: Telegram
  being unreachable, or the bot not being an admin of the group, must not
  silently close registration for everyone.
- Group audit reports who has left, and never acts. Leaving a chat is not the
  same as ceasing to pay.
- Daily rankings (top viewers by hours, top titles by plays) both in-bot and
  as a scheduled post to a group.
- Telegram now has its own nav section — bot settings, approval queue, group
  audit — because scattering them through "settings" and "members" made each
  one hard to find.

**Two bugs caught in the pre-release review**
- The scheduled ranking post had settings and an endpoint but was never wired
  into the background loop: turning it on did nothing. Expiry reminders and
  rankings now track their own send-day separately, so a restart between the
  two cannot cancel whichever has not gone out.
- `audit_group_membership` walked `members.list()`, which caps at 500 rows. A
  larger install would have skipped everyone past the cap and still reported
  "all present". Added `linked_telegram()`, unpaginated. An audit that
  under-reports is worse than none, because it is believed.

**Next**
- Country rules once a GeoIP source exists.
- Access-rule and sharing-finding UI pages.

## 2026-09-05 — dashboard shows the library, not just counters
**Done**
- Dashboard renders a poster wall of recent additions, plus playback cards
  carrying artwork, viewer and progress. The panel managed a media library
  while showing none of it; a row of counters gave no sense of what was
  actually arriving or being watched.
- New `GET /api/emby/latest`. Asks Emby for whole titles only (`Movie,Series`):
  a show that just gained twelve episodes would otherwise fill the wall with
  one series and bury everything else added that day. Limit is clamped to 24
  server-side and the result cached 60s, so the 30s auto-refresh does not
  re-query on every render.
- Sessions now carry `ItemId`, `ItemType`, `ProductionYear`, `Genres`,
  `Overview`, `RunTimeTicks`, `PositionTicks`, `ProgressPercent`. Without
  `ItemId` a card can only print a title where the poster belongs.
- `ProgressPercent` is `None` when runtime is unknown, never `0.0`. A live
  channel reports no runtime, and an empty bar is indistinguishable from a
  session that just started, so the UI omits the bar instead of lying.
- Artwork is addressed through the existing cached-image route, never Emby
  directly. A warm wall costs Emby one list query and nothing more; the
  metadata DB just moved off spinning disks and should not be hit per tile.
- Entries with no Primary image tag are dropped from the wall rather than
  rendered as grey holes in the grid.

**Next**
- User-management pages: the next feature focus.

## 2026-09-05 — dark console theme, and room for artwork
**Done**
- Panel is dark by default. It runs on a second monitor for hours next to a
  media player; a bright white sheet was the wrong default, and it made poster
  art read as a foreign object pasted onto a form.
- Palette is a deep indigo ramp, not neutral grey: at these luminances pure
  grey goes muddy, while a slight blue cast keeps large dark areas from
  looking flat. Cyan means "current/primary" and is used sparingly; green,
  amber and red are reserved for state, never decoration.
- Token set widened so pages stop hardcoding hex: `--panel-2/-3`, `--sidebar`,
  `--topbar`, `--control`, `--line-soft`, `--text-2`, and `*-soft` fills for
  each state colour.
- New media primitives, ready for the dashboard to use:
  `.poster-grid/.poster` (2:3 ratio preserved, bottom scrim, type badge),
  `.play-row` (compact "who is watching" list) and `.play-card` (poster,
  synopsis, viewer, elapsed/remaining).
- Fixes that only show up on a dark surface: native `select` dropdowns were
  rendering white, focus rings were invisible, and progress bars now carry a
  gradient so a nearly-empty track still shows direction.
- Styling only. No markup or JS touched: all 85 previously defined classes are
  still defined, 20 added, none dropped.

**Next**
- Wire the dashboard to `.poster-grid` and `.play-card` — done in the entry
  above; the existing image cache turned out to already cover the load
  concern that was blocking it.
- Then the user-management pages, which are the next feature focus.

## 2026-09-05 — dispatch follows the wire, and hot titles spread
**Done**
- `StreamNode.bandwidth_mbps`: a node's real link ceiling, headroom already
  deducted. `0` keeps the previous stream-count-only behaviour.
- `NodeState.utilisation()` now takes the worse of stream-count load and
  egress-against-ceiling. Ten streams on a 48-capacity node read as 21% while
  five direct-play 4K clients had already filled a 1 Gbit pipe; the wire now
  gets a vote.
- `Scheduler.SPREAD_MARGIN` (0.15): affinity still pins a title to a node for
  cache locality, but once the preferred node is measurably busier than the
  quietest peer the request goes to the quiet one. Without this a popular
  title collected every viewer on one machine while peers idled.
- New `reason` value `affinity-spread` so the dispatch log says why.
- `tests/test_dispatch_bandwidth.py`: saturated link reads busy, light streams
  are not talked down, nodes without a ceiling are unchanged, hot titles move,
  cold titles stay put under jitter, a fully saturated fleet still answers.

**Next**
- Expose `bandwidth_mbps` in the node editor UI (currently settings.json only).
- Consider deriving the ceiling from a periodic measurement instead of a
  hand-entered number.

## 2026-08-30 — per-user rate cap actually holds (panel MB/s + node nginx)
**Done**
- Panel bandwidth field is MB/s, matching the live speed column. Stored
  column stays kbps for Emby `RemoteClientBitrateLimit`.
- Saving a group/member cap drops the signed-URL cache and stops in-flight
  sessions so the next 302 carries the new `r=`.
- Node nginx template: HTTP/1.1 (no multiplexed Range streams), one live
  connection per member, no `limit_rate_after` burst.
- Already-302 viewers are labelled node speed (0 while the probe catches
  up) instead of the sampler `≈` bitrate guess.
- Probe user/egress window 3s -> 8s so a buffer burst does not read as a
  different movie every refresh.

**Next**
- ruff + pytest, PR, tag, deploy panel, apply nginx on the live node.

**Open questions**
- Uncapped (`r=0`) still means nginx `limit_rate 0` = unlimited. That is
  the operator choosing no cap, not a bug.

---

## 2026-08-29 (8) — 运营中枢 UI + membership backend gaps (v0.10)
**Done**
- Panel pages for members / plans / invites / stats / storage / audit. NAV
  entries no longer point at unimplemented views; `PAGES.users` is gone.
- Public invite redeem page at `/invite/{code}` (no admin auth).
- Dashboard cards for members, MRR, 即将到期, 超额.
- Settings page: membership sampling/enforcement + image-cache size/hit-rate.
- Node add flow is name (+ optional capacity) only; install command polls
  until the node reports home; enroll token can be rotated.
- Nodes pick global mounts instead of pasting rclone.conf; legacy inline
  config is labelled 旧式配置 with a migrate button.
- Backend gaps closed: HTTP 409 when deleting an in-use plan or remote;
  invite redeem rate limit; `max_devices` refused + mid-stream kick;
  enroll report + rotate; `delete_emby` is explicit; libraries expose `id`.
- Tests: membership suite gained §7 coverage (plan 409, remote 409, public
  redeem, rate limit, weekly rollover, device cap, enroll report). Baseline
  before this session was 88 passed.

**Next**
- Reviewer commit / tag / deploy. Notification center (v0.9 leftover).

**Open questions**
- Period-over-period stats delta uses `overview(days*2) - overview(days)` as
  an approximation of the previous window; exact previous-window queries
  were not added to the stats API.

---

## 2026-08-29 (7) — storage management backend (v0.8.0)
**Done**
- Panel becomes a configuration entry point, not just a viewer: cloud remotes
  and mounts can now be created from the API instead of being hand-edited on
  the host.
- `app/modules/storage.py`: StorageManager (configparser-based remote CRUD with
  atomic writes, connectivity test, systemd unit generation, start/stop/delete)
  plus MockStorage for credential-free development.
- Settings: rclone_binary, rclone_config_path, mount_root, cache_root,
  systemd_unit_dir, systemd_unit_prefix.
- Nine /api/storage/* routes with ValueError -> 422 and other failures -> 409.
- Security gates reviewed line-by-line and verified by hand: name allowlist
  regex, realpath containment against mount root (blocks ../, absolute paths
  and nested traversal), list-form subprocess with no shell, fixed unit-name
  prefix, secret redaction on read, explicit not-configured errors.
- Implementation delegated to a coding subagent; reviewed in depth (this
  touches production writes and permission logic) before commit.
- Tests 16/16, ruff clean.

**Next**
- Storage management UI page.
- Full-replacement roadmap (own scraper/library/playback) per owner decision.

---

## 2026-08-29 (6) — scheduled task center (v0.7.0)
**Done**
- `app/modules/tasks.py`: TasksReader over a sanitized host snapshot (missing
  file / unreadable JSON fail-safe, stale after 600s) + MockTasks covering ok,
  failing-with-streak, never-run and disabled jobs.
- `tasks_snapshot_path` setting, `GET /api/tasks`, env placeholder.
- Panel page 调度中心: three stat cards (total / currently failing / disabled),
  task table (schedule, status tag, relative last-run, duration, failure
  streak highlighted) and an alert card.
- First task produced under the new split-role workflow: implementation was
  delegated to a coding subagent, then reviewed line-by-line and committed by
  the main agent. Review notes: pattern-consistent with mounts.py, all HTML
  interpolation escaped, no real identifiers, null last_run handled.
- Tests 15/15, ruff clean.

**Next**
- Host-side task collector (outside repo) exporting the sanitized snapshot.
- v0.8.0 invites & access.

---

## 2026-08-29 (5) — mount health module (v0.6.0)
**Done**
- Rewrote ROADMAP into a self-driving plan (v0.6 mounts -> v0.7 scheduled
  tasks -> v0.8 invites/access -> v0.9 reports/notifications -> v1.0 live
  import executor + UI pass); no longer waits for per-step direction.
- `app/modules/mounts.py`: MountsReader over a sanitized host snapshot,
  MockMounts for dev. GET /api/mounts.
- Panel page 挂载管理: alive/stuck/cache stat cards, per-mount table
  (kind, options, readdir latency, stuck-process count, cache usage vs
  limit, free space) and an alert list.
- Host collector (outside repo) probes readdir in a child process so a wedged
  FUSE mount cannot hang the collector, counts D-state processes per mount,
  measures VFS cache dirs, and flags a union mount missing allow_other —
  the exact failure that took the library down earlier today.
- Tests 14/14, ruff clean.

**Next**
- v0.7.0 scheduled task center.

---

## 2026-08-29 (4) — drop acquisition module, add media library page (v0.5.0)
**Done**
- Owner decision: download/acquisition management stays out of scope. Removed
  the MoviePilot adapter, all /api/mp/* routes, its settings fields, env
  placeholders, tests and the two panel pages that used it.
- New media library module: EmbyAdapter.libraries() (mock + live via
  VirtualFolders + per-library item counts), GET /api/emby/libraries, and a
  媒体库 page with stat cards + library table. Dashboard now shows library
  counts instead of subscription/download counts.
- Tests 13/13, ruff clean.

**Next**
- Fill remaining pages step by step (invites, scheduled tasks, playback
  reports, mount management) per owner priority.

---

## 2026-08-29 (3) — panel shell redesign (v0.4.0)
**Done**
- Replaced the flat tab page with a proper admin shell: grouped left sidebar
  (概览 / 工作台 / 资源服务 / 系统管理), sticky topbar with page title+subtitle,
  hash routing, 30s auto refresh, toast layer.
- Split static assets into index.html + app.css + app.js (mounted at /static);
  new /api/whoami for the sidebar identity block.
- Pages: dashboard (6 stat cards + sessions/queues/quota/alerts), 搜索订阅,
  下载任务, 网盘上片, 用户管理, 节点管理, 管线状态, 版本更新.
- Page registry (PAGES) so new modules only add one entry + one nav item.
- Tests 13/13, ruff clean.

**Next**
- Fill pages step by step per owner feedback (media library, invites,
  scheduled tasks, playback reports).

---

## 2026-08-29 (2) — MoviePilot acquisition integration (v0.3.0)
**Done**
- `app/adapters/mp.py`: LiveMoviePilot (bearer login w/ token cache + renew on
  401) and MockMoviePilot. Media recognition search, site torrent search,
  subscribe add/list/delete, push download, downloading list.
- Endpoints under /api/mp/*; panel gets a new 搜索/订阅 tab (media search ->
  one-click subscribe w/ season prompt; torrent search -> one-click download;
  subscription table w/ unsubscribe; active downloads w/ progress).
- +1 test (13 passing), ruff clean.

**Next**
- Live import executor bridging host cloud-drive workers.
- Invite codes; notification center.

---

## 2026-08-29 (1) — basic functional web UI (v0.2.0)
**Done**
- Single-file functional panel at / (auth-protected): overview (sessions,
  pipeline queues, quota, fallback, alerts), stream nodes (status + dispatch
  log + enable/disable), Emby users (create/disable/enable/password), import
  jobs (submit/progress/cancel), update tab (check + one-click apply).
- Plain functional styling only; the visual design pass stays in Phase 4.
- Tests 12/12, ruff clean.

---

## 2026-08-28 (7) — root path redirect (v0.1.1)
**Done**
- GET / now 307-redirects to /docs so the panel root is not a bare 404.
- +1 test (12 passing), ruff clean. Released as v0.1.1 to exercise the
  web-triggered update flow end to end.

---

## 2026-08-28 (6) — self-update from the web panel
**Done**
- `app/modules/updater.py`: version (git describe), check origin for newest
  semver release tag, apply update via detached helper (fetch tags, checkout,
  pip install, service restart) so the API process can die safely mid-update.
- Endpoints: GET /api/update/version, GET /api/update/check,
  POST /api/update/apply (409 when already up to date / no valid tag).
- +2 tests (11 passing), ruff clean.

**Next**
- First release tag v0.1.0 + local deployment as a systemd service.
- Live import executor; invite codes.

---

## 2026-08-28 (5) — import lane module skeleton
**Done**
- `app/modules/imports.py`: unified ImportJob lifecycle (queued/running/done/
  failed), ImportManager registry with executor delegation, MockExecutor
  simulating progress. Kinds: cloud-drive, drive-link.
- Endpoints: POST /api/imports, GET /api/imports (+state filter),
  GET /api/imports/{id}, POST /api/imports/{id}/cancel.
- +1 test (9 passing), ruff clean.

**Next**
- Live executor adapter bridging host-side import workers (sanitized IPC).
- Invite-code system.

---

## 2026-08-28 (4) — Emby user management (write ops)
**Done**
- EmbyAdapter contract extended: create_user, set_user_disabled,
  set_user_password, apply_policy (mock + live implementations).
- Endpoints: POST /api/emby/users, /{id}/disable|enable, /{id}/password,
  /{id}/policy (policy patch restricted to an allowlist of safe fields).
- +1 test (8 passing), ruff clean.

**Next**
- Import-lane module skeleton (cloud-drive importers, Phase 2).
- Invite-code system design.

---

## 2026-08-29 (3) — node provisioning, signed URLs, settings-page fix
**Owner-reported issues, all root-caused**
1. *Panel feels laggy* — `/api/emby/libraries` took 2.16s: one item-count query
   per library (N+1), re-run on every page render and every 30s auto-refresh.
   Added a shared TTL cache (libraries 120s, sessions 5s).
2. *Settings page fails to load* — **my bug, shipped in v0.9.0**: `/api/settings`
   never returned `playback`, but the page did `const p = s.playback` and then
   read `p.enabled`, throwing before anything rendered. Added the field and
   pinned the whole payload shape with `test_settings_overview_contract`, since
   this class of bug is invisible until the page is opened.
3. *Nodes are unconfigurable / "云里雾里"* — correct, and the most important
   one. The panel could register a node but never explained how to build one;
   registering a node that does not exist only produces 404s.
4. *Security* — unsigned node URLs are permanent public download links.

**Done**
- `app/modules/signing.py`: nginx `secure_link_md5` compatible signed URLs with
  expiry. Digest is computed over the **decoded** `$uri` and only the result is
  percent-encoded — the reverse order 403s every path with a space or CJK
  character, i.e. most of a Chinese library. Key rotation invalidates every
  link already handed out.
- `app/modules/provisioning.py`: renders the whole node stack from stored
  settings — rclone mount unit (VFS cache, read-only, `Before=nginx`), nginx
  vhost (secure_link, range support, autoindex off), loadprobe unit, and a
  single install script wiring them together with self-checks. Also renders the
  Caddy/nginx front-door rule that routes **only** stream paths to the panel,
  which is the missing answer to "how does emby.example.com dispatch to nodes".
  Nothing is executed and no remote host is contacted; it emits reviewable text.
- Panel serves `/agent/loadprobe.py` so the installer fetches the agent from
  the panel itself and there is exactly one copy of it in the repo.
- Settings page gained 链接安全（签名）and 接入方式 cards; node page can fetch
  its install script.
- Settings/update pages no longer auto-refresh: re-rendering mid-edit wiped
  whatever the operator was typing.

**Verified**
- Signed URL round-trip incl. CJK path; expiry and wrong-path rejection.
- Install script contains every layer (mount, VFS cache, secure_link with the
  shared secret, `user_allow_other`, certbot, probe) and no unrendered template.
- Front-door snippet diverts only `emby/Videos/.../stream`.
- 38 tests passing, ruff clean.

**Next**
- Node-side verification against a real server (ca1) once owner approves.
- Invite codes / access control.

**Open questions**
- Install script assumes Debian/Ubuntu + systemd + nginx. Other distros need
  either detection or a documented manual path.

---

## 2026-08-29 (2) — playback interception (multi-node becomes real)
**Done**
- `app/modules/playback.py`: PlaybackRouter — Emby-compatible stream edge at
  `GET /emby/Videos/{id}/{rest}`. Resolves the item's backing file via Emby,
  uses that **file path** (not the request URL) as the affinity key, and 302s
  the client to the chosen node's copy. Until now the scheduler only had the
  `/stream/` test edge, so no real client ever reached it.
- **Fail-open by construction**: disabled, transcode/HLS, missing `Static=true`,
  unresolved item, no healthy node, empty mapped path and Emby errors all fall
  through to the Emby origin. A panel bug can only mean "not accelerated this
  time", never "playback is broken".
- Operator-configurable path mapping (`strip_prefix` + `path_template`) because
  a node's media root rarely matches Emby's; `{path}` placeholder enforced.
  `GET /api/playback/preview?item_id=` dry-runs the mapping so misconfiguration
  is caught in the UI instead of as 404s in client logs.
- TTL cache (300s) on item->path lookups so a popular title starting on many
  clients does not become one Emby metadata call per client; negatives cached
  too. Cache invalidated on settings save.
- `LiveEmby.item_media_paths()` maps MediaSourceId -> on-disk path, skipping
  http(s) sources that have no local file to serve.
- Decision log (`/api/playback/log`) records reason/node/media_path per
  interception, so "why did this not accelerate" is answerable.
- Interception defaults to **off** and refuses to enable without a node.
- Panel: 播放分流 card in 系统设置 (toggle, direct-only, path mapping, preview).

**Fixed while testing**
- Mock demo nodes were fabricated in `main.py` and never entered the settings
  store, so the settings page showed 0 nodes while the node page showed 2 —
  the same panel contradicting itself, and the "needs a node" guard misfiring.
  Demo nodes now seed through the store like real ones; `_mock_nodes()` removed
  and the scheduler reads nodes from a single source of truth.

**Verified** (live mock instance)
- Real Emby path `/emby/Videos/item42/stream.mkv?Static=true` -> 302 to node.
- Affinity: same item 10/10 to one node; 30 distinct items split 17/13.
- Fail-open: m3u8, no-Static, unknown item, disabled -> all to Emby origin.
- Path template `files/{path}` -> `https://node/files/Movies/Demo/item7.mkv`.
- 28 tests passing, ruff clean.

**Next**
- Node agent: serve mapped media paths (currently the node side is assumed).
- Invite codes / access control (v0.8.0).

**Open questions**
- Node-side auth: clients get a plain node URL. Signed/expiring URLs are needed
  before this is exposed to untrusted users commercially.

---

## 2026-08-29 — settings center + affinity dispatch
**Done**
- `app/core/store.py`: JSON-backed runtime settings document (atomic write,
  mode 600, gitignored data dir). Operator config no longer lives in `.env`.
- `app/modules/settings.py`: settings service — Emby connection, dispatch
  policy and streaming nodes are all editable via API/UI and applied to the
  running process immediately (scheduler is reconfigured in place, no restart).
  API keys are returned masked only; a `__KEEP__` sentinel lets the operator
  edit a URL without re-typing the secret.
- `LiveEmby` now resolves its connection per call from the settings store
  instead of frozen env vars; added `system_info()` + standalone `probe_emby()`
  so the UI can test a connection before saving it.
- Typed domain errors (`ConfigError` / `NotConfigured` / `UpstreamError`) with
  FastAPI handlers -> 422 / 409+needs_setup / 502, so the UI can distinguish
  "you typed something wrong" from "not connected yet" from "Emby is down".
- Scheduler: added `affinity` policy (rendezvous hashing) alongside
  `least-load`. Same path always resolves to the same node, so a title is
  cached once instead of pulled from origin by every node; falls through to the
  next candidate when the preferred node is unhealthy or above the utilisation
  threshold. Dispatch log records policy + reason.
- **Fixed a real modelling bug found during verification**: the old `weight`
  field made "normalized load" an absolute stream count, so comparing it to a
  0-1 threshold always overflowed and every request fell back to least-load
  (60 distinct files all landed on one node). Replaced `weight` with absolute
  `capacity`; load is now `active_streams / capacity`, so one threshold is
  meaningful across a heterogeneous fleet.
- Panel: new 系统设置 page (Emby connect + test, dispatch policy, node summary);
  node page gained add/edit-capacity/delete and shows utilisation %.
- Tests isolated per-test via `conftest.py` (own data dir), +6 tests (22 total),
  ruff clean.

**Verified**
- Live mock instance: same path -> same node 20/20; 60 distinct paths spread
  19/22/19 across three nodes. Secret persisted with mode 600, never returned
  in cleartext; URL-only edit retains the stored key.

**Next**
- Node agent config distribution (push rclone/mount config from the panel).
- Emby PlaybackInfo middleware so real playback uses the 302 scheduler.

**Open questions**
- Panel auth is still a single HTTP-Basic admin; multi-operator accounts and
  audit logging are needed before this is commercially usable.

---

## 2026-08-28 (3) — scheduler dispatch log + probe history
**Done**
- Scheduler keeps per-node probe history (deque, ~3h at 15s interval) and a
  recent dispatch decision log (node chosen, normalized load, candidate count,
  request context). Dry-run picks are NOT recorded; real /stream 302s are.
- New endpoints: `GET /api/nodes/{name}/history`, `GET /api/dispatch/log`.
- +1 test (7 passing), ruff clean.

**Next**
- Host-side collector script (outside repo) exporting sanitized pipeline snapshot.
- Roadmap Phase 2 prep: import-lane module skeleton.

---

## 2026-08-28 (2) — pipeline overview + node probe agent
**Done**
- `app/modules/pipeline.py`: PipelineReader serves a sanitized JSON snapshot
  written by a host-local collector (real paths never enter the repo);
  staleness detection (>300s); MockPipeline for credential-free dev.
- `agent/loadprobe.py`: stdlib-only single-file load probe for streaming
  nodes — /load endpoint reporting ESTABLISHED-connection stream count and
  5s-window egress Mbps, optional bearer token.
- Wired `/api/pipeline` into the app; +1 test (6 passing), ruff clean.

**Next**
- Dispatch log + probe history in scheduler.
- Host-side collector script (lives outside this repo, on the operator host).

**Open questions**
- none new.

---

## 2026-08-28 — Phase 1 scaffold
**Done**
- Repo initialized (public), FastAPI backend skeleton.
- Config strictly from env (`.env.example` only in repo); mock/live adapter
  architecture so the whole panel runs credential-free with `MEDIADECK_MOCK=1`.
- Load-aware 302 stream scheduler: normalized load = active_streams/weight,
  health probing with failure threshold, manual disable/enable, dispatch
  dry-run endpoint, real `/stream/{path}` 302 edge.
- Emby adapter (users, active sessions) in mock + live variants.
- 5 smoke tests passing; dev workflow codified in DEVELOPMENT.md.

**Next**
- Pipeline overview module: read-only queue/quota state via a host-local
  collector script that exports sanitized JSON (keeps real paths out of repo).
- Node probe agent (single-file, deployable to streaming nodes).

**Open questions**
- Panel domain: undecided (owner: "whatever for now").
- Frontend stack decision deferred until Phase 4 UI pass.
