# Deck hang watchdog and restart window

Two independent host-side pieces. Neither changes admission policy: while the
Deck cannot answer, nginx still refuses logins and playback exactly as before.

## 1. Hang watchdog (`deck_watchdog.py`)

`systemd` already restarts the Deck when the process exits (`Restart=on-failure`).
The watchdog covers the other failure: the process is alive but no longer
answers. Every 15 s it GETs `/healthz` with a 5 s timeout. Only probes that get
no answer at all count; any HTTP status resets the count. After 4 consecutive
unanswered probes (about a minute) it runs `systemctl restart` once.

It does nothing while:

- the maintenance flag exists (default `/run/mediadeck-maintenance`). Put an
  expiry (epoch seconds) in it, otherwise it expires 2 h after its mtime:

  ```sh
  date -d '+30 min' +%s > /run/mediadeck-maintenance   # before a deploy
  rm -f /run/mediadeck-maintenance                      # after it
  ```

- the unit is not `active/running` (manual stop, starting, stopping, failed),
  or a systemd job for it is pending;
- the unit became active less than 60 s ago;
- it already restarted the unit 3 times in the last hour (it then logs that an
  operator is needed instead of looping).

Every pause, failed probe and restart is written to the journal of the watchdog
unit (`journalctl -u mediadeck-watchdog`) and, if `DECK_WATCHDOG_LOG` is set,
appended to that file. Install from `mediadeck-watchdog.example.service`.

## 2. Keep the listening socket across restarts (`mediadeck.example.socket`)

With socket activation systemd owns the listening socket, so it stays open
while the Deck restarts: new connections wait in the kernel backlog and are
served by the new process instead of being refused. Requests still fail
closed if the Deck does not come back within nginx's own proxy timeouts.

Host changes (a drop-in for the existing service):

```ini
# /etc/systemd/system/mediadeck.service.d/80-socket.conf
[Unit]
Requires=mediadeck.socket
After=mediadeck.socket
[Service]
ExecStart=
ExecStart=<venv>/bin/uvicorn app.main:app --fd 3 <existing flags>
```

Keep every other existing flag (for example `--no-access-log`,
`--timeout-graceful-shutdown`). After `systemctl daemon-reload`, one
`systemctl restart mediadeck.service` switches over: the old process frees the
port, then the socket binds and the new process starts (`Requires=`/`After=`).
`systemctl enable mediadeck.socket` keeps it across reboots.

Behaviour to know:

- `systemctl stop mediadeck.service` alone is undone by the next incoming
  connection. Stop both units to keep the Deck down.
- Restarting `mediadeck.socket` restarts the service and briefly closes the port.
- If the Deck cannot answer at all, nginx still refuses (auth_request 5xx), only
  after its proxy read timeout instead of immediately.

Rollback: remove the drop-in, `systemctl daemon-reload`,
`systemctl stop mediadeck.service mediadeck.socket`, `systemctl disable mediadeck.socket`,
`systemctl start mediadeck.service`.
