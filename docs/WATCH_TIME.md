# Verified viewing time

Profiles, user charts, movie/series charts, overview, daily series and recent
viewing all read `watch_verified_samples`. Lifetime totals are durable in
`watch_verified_totals`; finishing a session does not add those seconds again.

An interval needs two observations of the same title, user, device and playback
identity, explicitly unpaused, with finite advancing `PositionTicks`, fresh
`LastActivityDate`, and unchanged positive playback rate. Position advance must
match the heartbeat elapsed time at that rate (bounded subsecond/5% jitter).
Credit is no greater than elapsed time or content advance divided by the rate.
Thus 2x playback does not count double. Positions beyond a known runtime fail
verification. Polling a cached session does not become a new heartbeat.

Pause, resume baseline, stalled/zero/missing position, rewind, seek, buffering,
missing/stale heartbeat, changed identity/rate and gaps beyond 120 seconds are
not backfilled. A process restart restores recorded totals but discards the
previous observation baseline; downtime cannot be credited. First observations
and ambiguous transition intervals may omit some genuine viewing time. This
is conservative verified client-reported playback, not proof of human attention
or a reconstruction of intervals without usable client evidence.

The first schema upgrade records `watch_verified_since`. No old sample, finished
session, live checkpoint or legacy import is converted into verified time. Old
rows remain intact; `historical_unverified_seconds` exposes the old lifetime
reference separately, excluding new verified increments. Web/Bot explain this
cutover. Pre-cutover calendar charts are empty rather than fabricated. Calendar
and rolling windows clip verified intervals at their exact boundaries, including
plays still in progress.

Intervals, lifetime totals and resumable state commit together. `(run_key,
ended_at)` deduplicates replays; pruning detail retains lifetime totals. Existing
traffic-byte estimates, measured accounting, enforcement and concurrent-stream
admission are unchanged by this viewing-time correction.
