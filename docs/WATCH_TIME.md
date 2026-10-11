# Recorded viewing and strict progress evidence

Member profiles, account cards and personal reports use one recorded viewing
read model. The cumulative value retains the bounded legacy import and old
non-sampled total, then the sampled umbrella total. Verified increments also
exist in that umbrella: they are not added a second time. Completed sampled
session events likewise are metadata, not another addition. Pruning detail does
not reset durable totals. Existing rows are never rewritten to invent history.

Timestamp-backed windows prefer verified intervals over their identical sample
mirror, then retain other recorded samples and non-sampled completed records.
Different concurrent sessions still add; they are not unioned per account.
A scalar without date/title detail contributes only to cumulative viewing.
An old completed record that straddles a window and contains unlocated pauses
cannot be split by wall-clock proportion; the window marks that uncertainty.
Unknown title counts are stated, not assigned to invented movies.

Personal weekly reports cover the previous complete Beijing week: Monday 00:00
to the following Monday 00:00. The batch's creation time fixes this week for
retries. Seconds, unique recorded session counts and title detail come from one
read snapshot and window; formatting happens after summing raw seconds. The
existing UTC daily traffic ledger cannot be precisely split at Beijing's eight
hour boundary, so such a report does not fabricate a weekly traffic value.
Monthly scheduling and the underlying traffic accounting are unchanged.

New effective viewing still requires two same-title/user/device/play observations,
explicitly unpaused, finite advancing position, fresh evidence and unchanged
positive rate. Position advance must match elapsed time at that rate within the
existing subsecond/5% jitter. Credit cannot exceed elapsed time or position
advance divided by rate; 2x playback does not count double. Known runtime limits,
pause/resume baselines, seeks, buffering, rewinds, gaps over 120 seconds and
restart downtime are not bypassed or backfilled.

Authenticated, upstream-accepted Progress now supplies its actual position and
receipt time to that same gate. It must match one existing bound playback. An
omitted rate can use only an explicitly reported, fresh rate from that same
session; a missing/stale/invalid rate remains unknown. Report and polling clocks
are not bridged: switching source establishes a new baseline. Fresh reports take
priority over cached Sessions rows. Per-play credited endpoints and transactional
interval/total/checkpoint writes prevent report/poll or replay double counting.
Stopped reports close proof immediately. A briefly missing Sessions row may keep
fresh report state, but missing time/traffic is never inferred.

Playback attempts and optional local Reporting timestamps provide only sourced
recent-use evidence, never plugin duration, points or bytes. Missing sources stay
unknown. One credential-free diagnostic summary per viewer is retained for at
most 14 idle days, with a 5,000-row cap and bounded counters; no raw body, token
or headers are retained. Detail shows the last observation's source and reason.

Historical records are not converted into `watch_verified_samples`. Existing
verified activity days/scores and rankings keep their original proof-only rules;
recent independent playback evidence can suppress an unsupported dormant label
without changing the score. Traffic, points, enforcement and stream admission
remain outside this display/read-model correction.

This is conservative client-reported progress, not proof of human attention or
reconstruction of intervals that were never collected. First observations,
unsupported client fields and ambiguous transitions may omit real viewing.
