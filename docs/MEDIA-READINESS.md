# Isolated media readers and readiness

A stalled FUSE `open` or `stat` blocks an nginx event-loop worker even when
`aio threads` is configured. Stream locations now proxy to a separate rclone
HTTP reader for each pool, bound only to loopback ports 9810 + pool index.
Signing verification, admission and connection/rate limits remain in nginx.
Readers use the existing read-only mount and do not create another disk cache.

For existing nodes, preserve each location's actual mount root (including any
operator-managed union). Install the matching `media_reader_unit` units, check
`systemd-analyze verify` and `nginx -t`, then replace only the media `alias`
directives with the generated proxy block. Do not overwrite an existing site
with the fresh-install template or change its public listeners.

Configure `/etc/mediadeck/media-probes.json` as a JSON array with a known media
file from each pool, for example:

```json
["http://127.0.0.1:9810/example/sample.mkv", "http://127.0.0.1:9811/example/sample.mp4"]
```

Paths must be URL encoded. The probe accepts only credential-free loopback
HTTP URLs and never follows redirects. Every 15 seconds it reads one byte from
each sample with a three-second socket timeout. It requires status 206, a valid
Content-Range and the byte itself. Failed, missing or stale samples make `/load`
report `ok=false` with a diagnostic reason, so the scheduler excludes the node.
The health handler never touches FUSE or waits for these media reads.

Fresh-install units set `LOADPROBE_MEDIA_PROBES` to this file and remain unready
until representative samples are configured. For existing units, add that
environment setting through a backed-up systemd override. The standalone probe
retains its legacy behavior when this option is absent. A successful sample is
evidence for that path, not a claim that every file is readable.

No capable node returns authenticated HTTP 503 with `X-Mediadeck-Fallback:
no-capable-node` and Retry-After; it never authorizes origin streaming. Export
`/api/integration/frontend?server=nginx&origin_fallback=false` when origin fallback
is forbidden. This sends an explicit restrictive policy to the panel and omits
nginx's internal 418 fallback interception. Authentication and admission still
run before any routing failure is returned.

Recovering an existing stuck read-only mount requires checking active users,
mount options and queued/dirty writes, saving thread stacks and backing up
units first. Never delete media or cache files to clear a read stall. The mount
template uses the serial chunk reader and bounded API request rate to avoid
parallel-reader contention and bursts. External storage outages still require
repair; reader isolation alone cannot make unavailable media playable.
