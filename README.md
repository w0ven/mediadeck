# mediadeck

Self-hosted unified control panel for an Emby-based media stack.

One panel to operate what is usually scattered across many tools:

- **Streaming node management** — multi-node 302 redirect scheduler with
  file-affinity dispatch (same title always served by the same node, so it is
  cached once instead of pulled from origin by every node), capacity-based
  load fallback, health probing, manual drain & kick.
- **Everything configurable from the UI** — connect Emby, add streaming nodes
  and change dispatch policy from the panel, applied live. No shell access, no
  service restart, no editing files on the server.
- **Pipeline overview** — staging/upload queues, quota state, oldest stuck
  items, emergency-local fallback lane, all read-only from your existing
  worker state files.
- **User management** — Emby users, policies, device limits, invite codes.
- **Import lanes** — cloud-drive (115 / Google Drive link) importers, PT
  search/subscribe via MoviePilot as the engine behind the panel.
- **Identify & scrape review** — AI identification audit queue, manual TMDB
  correction.

## Telegram ranking commands

- `/rank`: yesterday's user viewing-time ranking.
- `/today`: today's user ranking, from 00:00 Beijing time to the request snapshot.
- `/rank 影片` / `/today 影片`: movie and series viewing-time charts for the same periods.
- `/rank 7`: the previous seven complete calendar days (legacy command retained).

Today's posters, captions and numbered pages share one verified snapshot, display
its cutoff time and exclude paused or invalid playback. There are no period
switching buttons and no comparison against yesterday's full-day movement.
Send `/today` again to refresh; expired paging snapshots request a fresh command.

## Design rules

- **Zero data in repo.** All endpoints, tokens, paths come from environment /
  local config (`.env`, gitignored). The repo ships `.env.example` only.
- **Configuration belongs to the operator, not the filesystem.** Anything an
  Emby operator is expected to change is editable in the panel and persisted
  to the data directory (mode 600); `.env` only carries bootstrap values.
  Secrets are never returned to the browser in cleartext.
- Backend: FastAPI (Python 3.11+). Frontend: Vue 3 + Vite (added later —
  functionality first, UI polish last).
- Every external system sits behind an adapter with a mock implementation, so
  the full panel runs locally with `MEDIADECK_MOCK=1` and zero real
  credentials.

## Dev quickstart

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # fill in, or set MEDIADECK_MOCK=1
uvicorn app.main:app --reload --port 8300
# open http://127.0.0.1:8300/docs
```

## License

MIT
