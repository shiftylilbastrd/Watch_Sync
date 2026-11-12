#!/usr/bin/env python3
"""Mirror Plex watch history from source accounts to target accounts.

Replaces the old 10-minute family-watch-sync User Script. Runs forever as a
single persistent process - no per-run container startup, no cron, no
dependency on another app's Docker image. Polls Plex's own watch-history
endpoint (the same one the old script used) on a short interval, so sync
lands within POLL_INTERVAL_SECONDS of Plex marking something watched.

Config is a list of independent rules, each with its own source account(s),
target account(s), and its own last-synced watermark, loaded from a JSON
file so rules can be added/changed without rebuilding the image:

  {
    "rules": [
      {"name": "family", "sources": ["Family"], "targets": ["shiftylilbastrd", "momsgonecrazy"]}
    ]
  }

Env vars:
  PLEX_URL              default http://192.168.1.13:32400
  PLEX_PREFS            exact path to Preferences.xml, if you'd rather pin it than search for it
  PLEX_PREFS_GLOB_BASE  directory to search under for Preferences.xml when PLEX_PREFS isn't set
                         or doesn't exist, default /appdata (mount your whole appdata share here,
                         read-only - same auto-discovery the old family-watch-sync script used, so
                         the template doesn't need your Plex folder's exact name)
  CONFIG_PATH            path to rules.json, default /config/rules.json
  STATE_PATH             path to state file, default /data/state.json
  POLL_INTERVAL_SECONDS  default 45
  FIRST_RUN_HOURS        how far back to look on a rule's very first run, default 24
"""
import glob
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://192.168.1.13:32400")
PREFS_PINNED = os.environ.get("PLEX_PREFS", "")
PREFS_GLOB_BASE = os.environ.get("PLEX_PREFS_GLOB_BASE", "/appdata")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/rules.json")
STATE_PATH = os.environ.get("STATE_PATH", "/data/state.json")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "45"))
FIRST_RUN_HOURS = int(os.environ.get("FIRST_RUN_HOURS", "24"))

# How long resolved user tokens and the account-id map are trusted before
# re-fetching from Plex. These rarely change; no need to hit plex.tv every
# poll.
TOKEN_CACHE_SECONDS = 30 * 60


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def http(url, token, method="GET", accept_json=True, timeout=30):
    req = urllib.request.Request(url, method=method, headers={
        "X-Plex-Token": token,
        "Accept": "application/json" if accept_json else "application/xml",
        "X-Plex-Client-Identifier": "athena-watch-sync",
        "X-Plex-Product": "watch-sync",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    if not accept_json:
        return body
    return json.loads(body or b"{}")


def find_prefs():
    """A pinned PLEX_PREFS wins if it exists; otherwise search under
    PREFS_GLOB_BASE the same way the old bash script did, so the Docker
    template can just mount the whole appdata share read-only instead of
    needing to know the Plex container's exact folder name."""
    if PREFS_PINNED and os.path.exists(PREFS_PINNED):
        return PREFS_PINNED
    patterns = [
        os.path.join(PREFS_GLOB_BASE, "*", "[Pp]lex*", "Preferences.xml"),
        os.path.join(PREFS_GLOB_BASE, "*", "[Pp]lex*", "Library", "Application Support",
                      "Plex Media Server", "Preferences.xml"),
    ]
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            try:
                if "PlexOnlineToken" in open(path, encoding="utf-8", errors="ignore").read():
                    return path
            except OSError:
                continue
    sys.exit(f"Preferences.xml not found (checked PLEX_PREFS={PREFS_PINNED!r} and under {PREFS_GLOB_BASE!r})")


def admin_token():
    prefs = find_prefs()
    xml = open(prefs, encoding="utf-8", errors="ignore").read()
    m = re.search(r'PlexOnlineToken="([^"]+)"', xml)
    if not m:
        sys.exit("PlexOnlineToken not found in " + prefs)
    return m.group(1)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        log(f"WARNING: couldn't parse {path}, using default:\n{traceback.format_exc()}")
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_PATH, {"rules": []})
    rules = cfg.get("rules", [])
    out = []
    for i, r in enumerate(rules):
        name = r.get("name") or f"rule{i}"
        sources = [s.strip().lower() for s in r.get("sources", []) if s.strip()]
        targets = [t.strip().lower() for t in r.get("targets", []) if t.strip()]
        if not sources or not targets:
            log(f"WARNING: rule '{name}' is missing sources or targets, skipping")
            continue
        out.append({"name": name, "sources": sources, "targets": targets})
    return out


class PlexClient:
    """Wraps the admin token, account-id lookup and target-user token
    resolution, all cached so a poll loop doesn't hammer plex.tv."""

    def __init__(self):
        self.tok = admin_token()
        self._accounts_at = 0
        self._accounts = {}          # name(lower) -> account id
        self._user_tokens_at = 0
        self._user_tokens = {}       # name(lower) -> server access token

    def accounts(self):
        if time.time() - self._accounts_at > TOKEN_CACHE_SECONDS or not self._accounts:
            accts = http(PLEX_URL + "/accounts", self.tok)["MediaContainer"].get("Account", [])
            self._accounts = {(a.get("name") or "").lower(): a["id"] for a in accts if a.get("name")}
            self._accounts_at = time.time()
        return self._accounts

    def user_tokens(self, needed):
        """needed: set of lowercased usernames we need tokens for. Only
        resolves what's missing/stale."""
        if time.time() - self._user_tokens_at > TOKEN_CACHE_SECONDS:
            self._user_tokens = {}
        missing = [u for u in needed if u not in self._user_tokens]
        if not missing:
            return self._user_tokens
        tok = self.tok
        ident = http(PLEX_URL + "/identity", tok)["MediaContainer"]["machineIdentifier"]
        me = ET.fromstring(http("https://plex.tv/users/account", tok, accept_json=False))
        for key in ("username", "title", "email"):
            if me.get(key):
                self._user_tokens.setdefault(me.get(key).lower(), tok)
        try:
            root = ET.fromstring(http("https://plex.tv/api/servers/%s/shared_servers" % ident, tok, accept_json=False))
            for s in root.iter("SharedServer"):
                if s.get("accessToken"):
                    for key in ("username", "email"):
                        if s.get(key):
                            self._user_tokens.setdefault(s.get(key).lower(), s.get("accessToken"))
        except Exception as e:
            log("shared_servers lookup failed:", e)
        try:
            root = ET.fromstring(http("https://plex.tv/api/home/users", tok, accept_json=False))
            for u in root.iter("User"):
                name = (u.get("username") or u.get("title") or "").lower()
                if not name or name in self._user_tokens or name not in needed:
                    continue
                sw = ET.fromstring(http("https://plex.tv/api/home/users/%s/switch" % u.get("id"), tok, "POST", False))
                utok = sw.get("authenticationToken") or sw.get("authToken")
                if not utok:
                    continue
                res = ET.fromstring(http("https://plex.tv/api/resources?includeHttps=1", utok, accept_json=False))
                for d in res.iter("Device"):
                    if d.get("clientIdentifier") == ident and d.get("accessToken"):
                        self._user_tokens[name] = d.get("accessToken")
        except Exception as e:
            log("home users lookup failed:", e)
        self._user_tokens_at = time.time()
        return self._user_tokens

    def history_since(self, account_id, since):
        q = urllib.parse.urlencode({"accountID": account_id, "viewedAt>": since, "sort": "viewedAt:asc"})
        return http(PLEX_URL + "/status/sessions/history/all?" + q, self.tok)["MediaContainer"].get("Metadata", [])

    def scrobble(self, token, rating_key):
        http(PLEX_URL + "/:/scrobble?" + urllib.parse.urlencode(
            {"key": rating_key, "identifier": "com.plexapp.plugins.library"}), token, accept_json=False)


def run_rule(plex, rule, state):
    name = rule["name"]
    rule_state = state.setdefault(name, {})
    accounts = plex.accounts()
    src_ids = {accounts[s]: s for s in rule["sources"] if s in accounts}
    missing_src = [s for s in rule["sources"] if s not in accounts]
    if missing_src:
        log(f"[{name}] WARNING: source account(s) not found on server: {missing_src}")
    if not src_ids:
        return

    any_new = False
    for aid, sname in src_ids.items():
        key = f"src:{sname}"
        since = int(rule_state.get(key) or time.time() - FIRST_RUN_HOURS * 3600)
        hist = plex.history_since(aid, since)
        items = []
        for h in hist:
            if h.get("ratingKey") and int(h.get("viewedAt", 0)) > since:
                items.append((int(h["viewedAt"]), h["ratingKey"],
                               h.get("grandparentTitle") or h.get("title"),
                               h.get("title"), h.get("type")))
        if not items:
            continue
        items.sort()

        tokens = plex.user_tokens(set(rule["targets"]))
        missing_tgt = [t for t in rule["targets"] if t not in tokens]
        if missing_tgt:
            log(f"[{name}] WARNING: no Plex token for target user(s): {missing_tgt}")

        newest = since
        for viewed_at, rk, show, title, typ in items:
            label = f"{show} - {title}" if typ == "episode" else title
            for t in rule["targets"]:
                if t not in tokens:
                    continue
                try:
                    plex.scrobble(tokens[t], rk)
                    log(f"[{name}] marked watched for {t}: {label} (watched by {sname})")
                except Exception as e:
                    log(f"[{name}] FAILED for {t}: {label} - {e}")
            newest = max(newest, viewed_at)
        rule_state[key] = newest
        any_new = True

    if any_new:
        save_json(STATE_PATH, state)


def main():
    log("watch-sync starting; polling every", POLL_INTERVAL_SECONDS, "seconds")
    plex = PlexClient()
    state = load_json(STATE_PATH, {})
    last_config_log = None
    while True:
        try:
            rules = load_config()
            summary = [(r["name"], r["sources"], r["targets"]) for r in rules]
            if summary != last_config_log:
                log("active rules:", summary or "(none configured)")
                last_config_log = summary
            for rule in rules:
                run_rule(plex, rule, state)
        except Exception:
            log("ERROR during poll cycle:\n" + traceback.format_exc())
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
