"""Plex API access: admin token discovery, account/user-token resolution,
and the two calls the sync loop needs (watch history, scrobble)."""
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://192.168.1.13:32400")
PREFS_PINNED = os.environ.get("PLEX_PREFS", "")
PREFS_GLOB_BASE = os.environ.get("PLEX_PREFS_GLOB_BASE", "/appdata")
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
    # PREFS_GLOB_BASE is already a single, specific mount point (the Docker
    # template mounts the host's appdata share directly to it) - the Plex
    # folder sits directly under it, e.g. /appdata/Plex-Media-Server/...,
    # not nested one level deeper. (The old bash script's "*" before
    # [Pp]lex* matched variant appdata SHARE names on the host, like
    # appdata vs appdata2 - that's not needed here since the template
    # mounts one specific path.)
    patterns = [
        os.path.join(PREFS_GLOB_BASE, "[Pp]lex*", "Preferences.xml"),
        os.path.join(PREFS_GLOB_BASE, "[Pp]lex*", "Library", "Application Support",
                      "Plex Media Server", "Preferences.xml"),
    ]
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            try:
                if "PlexOnlineToken" in open(path, encoding="utf-8", errors="ignore").read():
                    return path
            except OSError:
                continue
    raise RuntimeError(
        f"Preferences.xml not found (checked PLEX_PREFS={PREFS_PINNED!r} and under {PREFS_GLOB_BASE!r})")


def admin_token():
    prefs = find_prefs()
    xml = open(prefs, encoding="utf-8", errors="ignore").read()
    m = re.search(r'PlexOnlineToken="([^"]+)"', xml)
    if not m:
        raise RuntimeError("PlexOnlineToken not found in " + prefs)
    return m.group(1)


class PlexClient:
    """Wraps the admin token, account-id lookup and target-user token
    resolution, all cached so a poll loop doesn't hammer plex.tv."""

    def __init__(self):
        self.tok = admin_token()
        self._accounts_at = 0
        self._accounts = {}          # name(lower) -> account id
        self._account_display = []   # account names as Plex shows them, for UI dropdowns
        self._user_tokens_at = 0
        self._user_tokens = {}       # name(lower) -> server access token

    def accounts(self):
        if time.time() - self._accounts_at > TOKEN_CACHE_SECONDS or not self._accounts:
            accts = http(PLEX_URL + "/accounts", self.tok)["MediaContainer"].get("Account", [])
            self._accounts = {(a.get("name") or "").lower(): a["id"] for a in accts if a.get("name")}
            self._account_display = sorted({a.get("name") for a in accts if a.get("name")}, key=str.lower)
            self._accounts_at = time.time()
        return self._accounts

    def account_names(self):
        """Account names in Plex's own casing, for populating a dropdown.
        Matching elsewhere stays case-insensitive via accounts()."""
        self.accounts()
        return self._account_display

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
