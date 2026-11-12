"""Background loop: polls Plex's watch history per rule, scrobbles matches
onto each rule's target accounts, and records watermarks/activity via db.py."""
import os
import threading
import time
import traceback

import db
from plex_client import PlexClient, log

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "45"))
FIRST_RUN_HOURS = int(os.environ.get("FIRST_RUN_HOURS", "24"))


def run_rule(plex, rule):
    name = rule["name"]
    accounts = plex.accounts()
    src_ids = {accounts[s]: s for s in rule["sources"] if s in accounts}
    missing_src = [s for s in rule["sources"] if s not in accounts]
    if missing_src:
        log(f"[{name}] WARNING: source account(s) not found on server: {missing_src}")
    if not src_ids:
        return

    for aid, sname in src_ids.items():
        since = db.get_watermark(name, sname, int(time.time() - FIRST_RUN_HOURS * 3600))
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
                    db.log_activity(name, t, label, sname, "ok")
                except Exception as e:
                    log(f"[{name}] FAILED for {t}: {label} - {e}")
                    db.log_activity(name, t, label, sname, f"failed: {e}")
            newest = max(newest, viewed_at)
        db.set_watermark(name, sname, newest)


def _loop():
    log("watch-sync worker starting; polling every", POLL_INTERVAL_SECONDS, "seconds")
    plex = None
    while True:
        try:
            if plex is None:
                # Constructing PlexClient() can fail (Plex unreachable,
                # Preferences.xml not found yet, token not ready) - that must
                # never kill this thread/process, just get retried next tick.
                plex = PlexClient()
            for rule in db.list_rules():
                run_rule(plex, rule)
        except Exception:
            log("ERROR during poll cycle:\n" + traceback.format_exc())
            plex = None
        time.sleep(POLL_INTERVAL_SECONDS)


def start_worker():
    threading.Thread(target=_loop, daemon=True).start()
