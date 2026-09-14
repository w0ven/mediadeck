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
Gateway code changes are not required. Deployment needs a Deck restart and nginx
configuration test/reload, not an Emby restart or mass policy rewrite.

## Protocol contract

Normal clients keep their original Emby URLs:

| Original request | Local handling |
| --- | --- |
| GET/HEAD/POST Items/{item}/PlaybackInfo | Deck `/api/playback/info/{item}` verifies caller/item, admits, forwards metadata, binds returned PlaySessionId before returning URLs |
| GET/HEAD stream/original | shared admission, then unchanged gateway/Deck 302 dispatch |
| video/audio stream/HLS URLs | shared admission, then unchanged Emby origin |
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
  releases a bound lease. For legacy unbound sessions, a transition from observed
  playback to idle also releases it. A bound PlaySessionId is not cleared merely
  by an idle snapshot, which can lag behind a replacement start.
- Pending starts do not expire blindly after a few seconds. A client that abandons
  PlaybackInfo and never reports Stopped retains its seat until its Emby Session
  disappears (for example logout). Likewise, a client that omits the current
  PlaySessionId in Stopped is not promised immediate release. This conservative
  behavior avoids claiming that a missing observation proves no stream exists.
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

Local tests execute real nginx on temporary ports against Deck's mock adapters.
They cover both the metadata issuance transaction and direct/manifest entrances.
Operator-side live-client validation remains a separate deployment acceptance.
