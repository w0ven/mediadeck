# Playback admission (302 retained)

Device registration is observational: no registration-count cap exists in the
panel or Bot. Counts, device history, manual block and forget remain available.
`max_streams` is the effective member concurrency setting; this change does not
rewrite existing users' values. Native Emby `SimultaneousStreamLimit` remains a
second layer, not the evidence that the third URL was withheld.

## Entrance wiring

Run **one Deck worker**, with its persistent data directory. The service stores
session leases in `stream_leases` in the existing SQLite database. Do not delete
this state during upgrade. Multiple Deck workers/replicas are not supported by
this admission coordinator.

Inside the media nginx server:

1. Add `auth_request /_deck_admission;` to the existing stream/original location.
   Keep its gateway upstream, entry credentials, no-cache and redirect behavior.
2. Include `deploy/nginx/playback-admission.conf` **after** that existing location.
   Define `deck_admission_backend` and `emby_playback_origin` upstreams in `http`.
   Resolve them to the operator's private Deck and Emby endpoints.
3. Keep the existing source-capability location unchanged. It is a server/source
   capability, not a logged-in user's identity. Neither the template nor Deck
   substitutes an administrator token for a user's token.
4. Retain the ordinary origin location for non-playback traffic. Subtitles,
   artwork and metadata do not acquire seats. The guarded video/audio endpoints
   are stream/original, master/main/manifest/live, HLS and audio universal.
5. No cache or error fallback may turn an admission error into an origin request.
   `auth_request` surfaces a failed dependency as an error, not permission.

The template requires nginx's HTTP auth_request module. Inspect the current
production configuration before applying an operator-specific candidate; the
public template intentionally contains no production hosts or credentials.
Lifecycle wiring needs a Deck restart and nginx configuration test/reload,
not an Emby restart or mass policy rewrite. The separately versioned gateway
registry optimization retains its existing routing and capability contract.

## Protocol contract

Normal clients keep their original Emby URLs:

| Original request | Local handling |
| --- | --- |
| GET/HEAD/POST Items/{item}/PlaybackInfo | Deck `/api/playback/info/{item}` verifies caller/item, admits, forwards metadata, binds returned PlaySessionId before returning URLs |
| GET/HEAD stream/original | shared admission, then unchanged gateway/Deck 302 dispatch |
| video/audio stream/HLS URLs | shared admission, then unchanged Emby origin |
| POST Sessions/Playing | Deck `/api/playback/started` forwards the caller's report and observes only an existing matching bound play after Emby accepts |
| POST Sessions/Playing/Progress | Deck `/api/playback/progress` does the same, including paused progress; it cannot acquire another seat |
| POST Sessions/Playing/Stopped | Deck `/api/playback/stopped` forwards the caller's report, releases only on successful delivery and matching PlaySessionId |

PlaybackInfo POST accepts either a JSON object or an empty body with query
parameters. Caller headers, query and device profile are forwarded, without a
configured admin key. Video data is **not** proxied through Deck.

The nginx internal auth subrequest calls `GET /api/playback/admit`, passing the
original caller headers and `X-Original-URI`. The latter supplies only the path
and query; it is not a trusted user assertion. Authentication and item access
are rechecked before admission or URL routing. Missing/ambiguous identity,
malformed session responses and dependency outages do not fail open.

Use the same token and DeviceId across login, PlaybackInfo, Playing, stream and
Stopped. Admission counts distinct authenticated **Emby Session Ids**, not
DeviceIds. If several Sessions have the same DeviceId, a SessionId must identify
the caller; ambiguity is refused rather than merged into one stream. HLS can
also resolve its session through the previously bound PlaySessionId when it
omits DeviceId. A request-supplied UserId is never used as authentication.

## Lease lifecycle

- Concurrent starts use one serialized fresh-session observation and durable
  claims. Rechecking through nginx and Deck does not claim a second seat.
- Retry/seek within the same Emby session retains its seat. PlaybackInfo response
  binding prevents an old play's delayed Stopped report releasing its replacement.
- Pauses retain the seat, while usage billing still excludes paused time. This
  is necessary for normal resume using an already-issued 302 URL without a new
  media request. Paused/idle clients are not selected for advisory overflow Stop.
- A matching successful Stopped report or disappearance of the Emby Session
  releases its lease. An observed play also releases when a fresh snapshot is
  idle, including a bound PlaySessionId. Start/progress acknowledgements update
  observation even if nobody requests another URL during the entire play.
- A newly bound replacement resets observation so delayed old events cannot
  release or mark the new pending play. Failed metadata issuance releases only
  the seat newly acquired by that failed request, not an existing play.
- An unobserved pending start has a **120-second grace period**. Only a fresh
  snapshot showing neither playing nor paused may expire it. Snapshot failure
  never means idle. A recheck/seek does not renew the deadline; a genuinely new
  PlaySessionId starts a new grace period. Deadline state persists on restart.
- Legacy rows without issuance timestamps receive one 120-second migration
  grace period; restarting again does not renew it. Missing lifecycle reports
  no longer hold an idle account forever. A client resuming after expiry must
  pass admission again, and may be rejected if another session took the seat.
- This intentionally prioritizes avoiding indefinite lockout over strict
  enforcement for clients that hide all playback state: an already-issued
  cloud URL cannot be revoked by deleting its lease. Real playback and pause
  visible in Emby remain counted regardless of pending age.
- Stop command HTTP acceptance is advisory. It does not delete the Emby Session,
  and both accepted and failed commands back off. LastActivityDate never decides
  who started first; unknown start order is not a license to kick existing plays.

Already-issued direct-link reuse is outside the requested protection. This is
not a video proxy or a promise to revoke a cloud URL after issuance.

## Acceptance

Use a dedicated member with cap 2 and three genuine authenticated Emby Sessions
with distinct DeviceIds. Do not change unrelated users' caps.

1. Request PlaybackInfo on the first two, retaining each returned PlaySessionId.
   Send normal Playing reports; confirm existing direct dispatch remains 302.
2. Third PlaybackInfo and stream requests must fail with 403 and no URL/Location,
   before any gateway cloud lookup or origin URL response. Exercise ordinary and
   gateway-selected editions, plus HLS manifests/segments.
3. Seek/retry either admitted client; verify no extra seat. Pause/resume retains
   its seat and does not cause Stop of either existing client.
4. Send Stopped with the **current** PlaySessionId of one admitted client; after
   success, retry the third PlaybackInfo and expect admission. A stale old
   PlaySessionId must not release a new play.
5. Check query-only empty POST PlaybackInfo, subtitle fetch with no seat claimed,
   invalid token/item denial, and dependency failure without fallback.
6. Start and finish a play with no intervening admission; after successful
   start/progress the idle snapshot must release it without a password change.
7. Abandon a pending start: deny another session at 119 seconds, admit at 120
   only with a fresh idle snapshot. Repeat across restart; playing/paused and
   snapshot-outage cases must never be expired by time alone.

Local tests execute real nginx on temporary ports against Deck's mock adapters.
They cover both the metadata issuance transaction and direct/manifest entrances.
Operator-side live-client validation remains a separate deployment acceptance.
