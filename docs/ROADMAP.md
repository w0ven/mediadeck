# Roadmap

Owner sets priorities when he wants to; otherwise this plan is executed
top-down without asking. Every item ships as its own PR + release tag.

## User-management rebuild — live UI foundation (local development)
- [x] Replace SSE-triggered full-page loaders with cached-model DOM patches.
- [x] Keep editor/selection/scroll state and guard route/reconnect races.
- [x] Provide a live-page registration contract and query-aware navigation.
- [x] Reproduce the original failure and verify in isolated Chromium.
- [x] Integrate member lifecycle, measured-quota modules and real browser/kernel/nginx verification.
- [ ] Operator-approved deployment, measured-baseline cutover and legacy-user migration (separate phase).
- No deployment, node configuration change, or existing-user migration in this step.

## Done
- [x] Backend scaffold, env-only config, mock/live adapter split
- [x] Load-aware 302 stream scheduler (weights, health, kick, history, log)
- [x] Node load probe agent (stdlib single file)
- [x] Pipeline overview (sanitized host collector -> panel)
- [x] Emby user management (create/disable/password/policy)
- [x] Import lane skeleton (job lifecycle + API)
- [x] Web-triggered self-update to release tags
- [x] Admin panel shell (grouped sidebar, stat cards, page registry)
- [x] Media library overview
- [~] Acquisition/downloads: dropped by owner decision (stays external)

## Next — Registered external playback entries (draft PR)
- Register HTTPS entries through the existing integration settings API.
- Authenticate each proxy's entry assertion; keep Emby auth and node signing.
- Return entry-local `/_n/<node>/s/...` URLs and isolate playback caches.
- Export a fixed-upstream Caddy template; document the origin proxy hop and
  local verification separately from operator-side end-to-end acceptance.
- Delivery stops at a tested draft PR; rollout is a separate operator action.

## v0.6.0 — Mount health (shipped)
Storage is the single most failure-prone layer in this stack: FUSE mounts go
stale, a union mount loses allow_other and the whole library 403s, ffprobe
wedges in D-state. This page makes all of it visible in one place.
- [x] Collector: per-mount liveness (readdir probe), backend type, options
- [x] Detect stuck I/O (processes blocked in uninterruptible sleep per mount)
- [x] VFS cache usage vs configured limits; free-space floor per filesystem
- [x] Panel page: mount table, health tags, stuck-process alerts

## v0.7.0 — Scheduled task center (shipped)
- [x] Snapshot schema: per-job schedule, last run, status, duration, exit code,
      failure streak
- [x] Panel page: stat cards, task table, alert list
- [ ] Host collector wiring (in progress)
- [ ] Manual trigger (allowlisted) — deferred

## v0.8.0 — Invites & access (shipped in v0.10 panel)
- [x] Invite code issuing with quota/expiry, redemption -> Emby user creation
- [x] Access control view: per-user device/stream limits at a glance
- [x] Public redeem page at `/invite/{code}` with rate limiting

## v0.9.0 — Playback reports & notifications
- [x] Playback history aggregation (top titles, per-user minutes, node split)
- [ ] Notification center with routing rules and delivery log

## v0.10.0 — 运营中枢 (working tree, uncommitted)
- [x] Membership / plans / invites / stats / storage / audit panel pages
- [x] Node enroll: name-only create, one-line install, call-home report, rotate token
- [x] Global storage remotes + mounts; nodes pick mounts instead of pasting rclone.conf
- [x] Referential integrity (plan/remote in use -> HTTP 409)
- [x] max_devices enforced on register + mid-stream kick

## v1.0.0 — Live import executor + UI pass
- [ ] Bridge import jobs to real host-side workers (sanitized IPC)
- [ ] Visual design pass across all pages
