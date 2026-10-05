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

Account disable is reflected in local suspended state (existing pending status
is retained) so quota reconciliation cannot re-enable the account. Remote failures are recorded as failures/unknown,
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

## Administrator manual disable / release

- Web member overview and the administrator's `/kk` target card expose separate
  **禁用账号** and **解除禁用** buttons, with confirmation. Group and private Bot
  cards bind actor, reviewer account, target, message and forum topic; execution
  rechecks authority and consumes a one-use nonce before awaiting dependencies.
  Anonymous administrators and other card readers cannot execute the operation.
- Manual intent is independent of `membership.enforcement_enabled`. Sanctions,
  manual operations and reconciliation share the policy-apply lock. A confirmed
  release clears suspended status (including automatic sanctions), not expiry,
  quota, group, roles or other policy. Pending accounts remain pending. Only
  `IsDisabled` is patched; an expired/exhausted/pending account stays disabled.
  Known remote-access/playback/library restrictions are reported, not bypassed.
  Release is not an exemption: a later freshly confirmed violation can sanction
  the account again, including within the previous login deduplication window.
- Emby/Deck administrators and the operator's own account are protected. Disable
  requires affirmative remote non-administrator identity; the live adapter reads
  the target policy again and checks target/authority immediately before POST.
  Emby does not offer a cross-system transaction or conditional policy write:
  external permission changes after that read and lost transport acknowledgments
  cannot be made atomic. Failed/unknown outcomes never claim current availability;
  refresh and explicitly confirm a retry. Stop-command failure is separate from
  a confirmed disable and does not repeat successful result notifications.
- Authenticated `POST /api/members/{id}/actions/disable|enable` accepts optional
  `request_id` (1–64 ASCII letters/digits/`-_:`). Replies include `ok`, `local_ok`,
  `remote_ok`, `retryable`, `result`, `event_id` and fresh `access` observation
  (`status`, `state`, `emby_disabled`, `remaining_restrictions`,
  `account_available`). Reusing an identity observes its receipt and current
  state, never replays it; a different target/action is rejected. Web retains the
  identity across an uncertain transport retry. A failed/unknown receipt needs
  a new explicit confirmation/identity to execute again.
- Legacy member status suspended/active, bulk suspend/activate and Emby
  disable/enable use the same path. Bulk accepts a request identity and returns
  individual `results`; unmanaged Emby accounts are not silently enrolled.
  Legacy policy edits containing `IsDisabled` use this path too; mixed disable
  and other policy edits must be submitted separately (422). Missing Emby users
  retain 404. Generic retry-remote refuses an unconfirmed manual intent (409),
  rather than accidentally reconciling a failed disable back to enabled.
- Existing restriction tables/notices retain automatic incident meanings. Manual
  events use `rule=manual`, `action=disable|enable` and explicit administrator
  audit entries; identical successful intent/no-op does not resend notices.
  Group/private delivery receipts and retry rules remain independent. A release
  receipt states whether the account is usable and any remaining restriction.
  Manual audit changes also invalidate Web observations after Bot-only edits.

## Deployment

Use the versioned `deploy/nginx/playback-admission.conf` at the media entry, not
only the panel's own routes. Existing deployments with inlined admission rules
must add its two login locations while preserving their stream/gateway locations.
Validate nginx before reloading. No Emby restart is required.

Test the login transport, wrong-password/admin exemptions, all playback issuers,
exit/replacement lifecycle, pending-vs-real overflow and independent notification
receipts. Never use a real member as a punitive deployment smoke test.
