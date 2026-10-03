# Account access restrictions

The access page exposes independent automatic sanctions for concurrent-play
violations, Emby Web login and Emby Web playback. Each rule supports refusal /
stop of the offending request or session, account disable, or account deletion.
Deletion never cascades to an inviter. Web rules are opt-in on upgrade; the
operator must explicitly enable them. Concurrent-play admission remains active
when its automatic-sanction switch is disabled.

## Evidence and exemptions

- Playback authenticates the caller and item permission before any rule can
  issue a sanction. A refusal, transport failure or browser User-Agent alone is
  not evidence of a violation.
- Browser detection uses the authenticated Emby session's `Client` identity
  (`Emby Web`, `Emby Web Mobile`, `Emby Web Client`). It is not cryptographic
  browser attestation; clients holding valid credentials can spoof client
  metadata. Ambiguous session identity is refused without punishment.
- Login metadata is proxied at both `Users/AuthenticateByName` and the legacy
  `Users/{id}/Authenticate` entry. The caller's password and non-secret client
  identity are forwarded, never an administrative token. Only a successful
  authenticated response and matching fresh session may cause punishment.
  Loading a login page or submitting an incorrect password cannot punish an
  account. A denied login never returns its newly issued access token.
- PlaybackInfo, direct redirect and nginx HLS admission share Web-playback checks.
  Existing logged-in browser tokens are covered on their next playback request;
  existing login sessions are not retrospectively punished on deployment.
- Emby administrators and Deck `admin` members are exempt. Whitelist membership
  does not bypass these rules; its effective concurrent-play limit still applies.
- Concurrent-play sanctions require fresh reconfirmation of the exact excess
  session, effective limit and original occupied sessions. Pending starts,
  ambiguous/cold-start overflow and dependency failures are refusals, not sanctions.
  The polling fallback also rechecks exact overflow and requires reliable start
  order. Stop targets only the confirmed excess session.
- Normal pauses retain a seat; completed/stopped sessions release it. See
  [playback admission](PLAYBACK_ADMISSION.md) for pending leases and out-of-order
  report semantics. Already issued third-party URLs cannot be revoked.

## Execution and notifications

Account disable is reflected in local suspended state so quota reconciliation
cannot re-enable the account. Remote failures are recorded as failures/unknown,
not success. Deletion removes only the confirmed account and its membership;
restriction receipts and notification destinations survive it.

Sanctions are serialized, deduplicated per user/rule/session for five minutes
(login retries are deduplicated per user/rule), and never automatically replayed
following a crash or uncertain remote response. A later new attempt can be a
new incident. Client Stop acceptance is reported as a command accepted, not as
proof that playback actually ceased.

Every incident queues a separate receipt for each configured interaction group
and the member's bound Telegram private chat. The message includes username,
rule, selected action and actual result, without credentials or IP addresses.
Unbound accounts/missing group configuration are recorded as skipped. Failed
private delivery does not undo a successful group post, and vice versa.
Temporary Telegram failures retry at most four total attempts, with bounded
backoff; blocked users and unknown delivery outcomes are not retried blindly.
Interrupted sending is marked unknown after restart. The access page exposes
both sanction and individual notification outcomes.

## Deployment

Use the versioned `deploy/nginx/playback-admission.conf` at the media entry, not
only the panel's own routes. Existing deployments with inlined admission rules
must add its two login locations while preserving their stream/gateway locations.
Validate nginx before reloading. No Emby restart is required.

Test the login transport, wrong-password/admin exemptions, all playback issuers,
exit/replacement lifecycle, pending-vs-real overflow and independent notification
receipts. Never use a real member as a punitive deployment smoke test.
