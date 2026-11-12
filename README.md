# watch_sync

Mirrors Plex watch history from one account to others — e.g. when the
"Family" account finishes an episode, mark it watched for `shiftylilbastrd`
and `momsgonecrazy` too. Replaces the old Tautulli script-agent and, after
that, the 10-minute `family-watch-sync` Unraid User Script.

Runs as a single persistent container that polls Plex's own
`/status/sessions/history/all` endpoint every `POLL_INTERVAL_SECONDS`
(default 45s) — no per-run container startup, no dependency on another
app's Docker image existing, no cron.

## Why this exists instead of the User Script

The old script borrowed `media-check-tools`'s local Docker image purely as a
disposable python3 runtime. When that image stopped being built locally
(media-check-tools moved to a real GHCR-published image), every run of the
script silently exited at an `docker image inspect` guard before the Python
ever ran — so it looked "fine" (no errors in the log) while quietly doing
nothing for a week. This app has its own image, published by its own CI, so
it can't break that way again.

## Config

Rules live in a JSON file (mounted into the container, not baked into the
image, so you can add/change rules without rebuilding):

```json
{
  "rules": [
    {
      "name": "family",
      "sources": ["Family"],
      "targets": ["shiftylilbastrd", "momsgonecrazy"]
    }
  ]
}
```

Each rule is independent — its own source account(s), target account(s),
and its own synced-up-to watermark per source, stored in the state file.
Add more rule objects to sync other account pairs; no code changes needed.
Account names are matched case-insensitively against Plex's own account
names, same as the old script.

See `rules.example.json` for a starting point.

## Running on Athena

- Image: `ghcr.io/shiftylilbastrd/watch_sync:latest`
- Mounts:
  - `/appdata` (read-only) → your whole `/mnt/user/appdata` share - the app searches under it for Plex's `Preferences.xml` itself (same auto-discovery the old script used), so it doesn't need to know your Plex container's exact folder name. Set `PLEX_PREFS` instead if you'd rather pin an exact file.
  - `/config/rules.json` → your rules file
  - `/data` → persisted state (`last_viewed_at` watermark per rule/source)
- Env (all optional, defaults shown):
  - `PLEX_URL=http://192.168.1.13:32400`
  - `PLEX_PREFS_GLOB_BASE=/appdata`
  - `CONFIG_PATH=/config/rules.json`
  - `STATE_PATH=/data/state.json`
  - `POLL_INTERVAL_SECONDS=45`
  - `FIRST_RUN_HOURS=24` — how far back a rule looks on its very first run (no prior state)
- No inbound ports needed — it only makes outbound calls to Plex and, for
  resolving Home/managed-user tokens, plex.tv.
- Join the same custom Docker network the rest of the stack uses so it can
  reach Plex (Plex itself is on host networking, reachable via the LAN IP
  above regardless).

Logs go to stdout — `docker logs -f watch_sync` (or Unraid's log viewer)
shows every scrobble as it happens.
