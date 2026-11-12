# watch_sync

Mirrors Plex watch history from one account to others — e.g. when the
"Family" account finishes an episode, mark it watched for `shiftylilbastrd`
and `momsgonecrazy` too. Replaces the old Tautulli script-agent and, after
that, the 10-minute `family-watch-sync` Unraid User Script.

Runs as a single persistent container that polls Plex's own
`/status/sessions/history/all` endpoint every `POLL_INTERVAL_SECONDS`
(default 45s) — no per-run container startup, no dependency on another
app's Docker image existing, no cron. A small web UI lets you manage sync
rules and see recent activity.

## Why this exists instead of the User Script

The old script borrowed `media-check-tools`'s local Docker image purely as a
disposable python3 runtime. When that image stopped being built locally
(media-check-tools moved to a real GHCR-published image), every run of the
script silently exited at a `docker image inspect` guard before the Python
ever ran — so it looked "fine" (no errors in the log) while quietly doing
nothing for a week. This app has its own image, published by its own CI, so
it can't break that way again.

## Managing rules

Open the container's web UI. Each rule has a name, one or more source
accounts, and one or more target accounts, picked from dropdowns populated
with Plex's own current account list (not free text — avoids the exact kind
of typo/case mismatch that silently broke an earlier version of this app).
A rule is rejected if a name's already in use, if it has no sources or no
targets, or if a source and target overlap (an account syncing to itself).

Each rule tracks its own synced-up-to watermark per source account, so
rules don't interfere with each other. Add as many rules as you want
without touching any config file or redeploying.

## Running on Athena

- Image: `ghcr.io/shiftylilbastrd/watch_sync:latest`
- Port: `8099` → the web UI (rules + recent activity)
- Mounts:
  - `/appdata` (read-only) → your whole `/mnt/user/appdata` share - the app searches under it for Plex's `Preferences.xml` itself (same auto-discovery the old script used), so it doesn't need to know your Plex container's exact folder name. Set `PLEX_PREFS` instead if you'd rather pin an exact file.
  - `/data` → the rules database, sync watermarks, and activity log
- Env (all optional, defaults shown):
  - `PLEX_URL=http://192.168.1.13:32400`
  - `PLEX_PREFS_GLOB_BASE=/appdata`
  - `DB_PATH=/data/watch_sync.db`
  - `POLL_INTERVAL_SECONDS=45`
  - `FIRST_RUN_HOURS=24` — how far back a rule looks on its very first run (no prior watermark)
- Needs inbound access to port 8099 for the UI; outbound to Plex and, for
  resolving Home/managed-user tokens, plex.tv.
- Join the same custom Docker network the rest of the stack uses so it can
  reach Plex (Plex itself is on host networking, reachable via the LAN IP
  above regardless).

A ready-made Unraid template is at `unraid/watch_sync.xml` - drop it into
`/boot/config/plugins/dockerMan/templates-user/` and it shows up under
"Add Container" like a Community Apps template.

Logs go to stdout — `docker logs -f watch_sync` (or Unraid's log viewer)
shows every scrobble as it happens.
