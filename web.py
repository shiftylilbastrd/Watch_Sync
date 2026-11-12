import os

from flask import Flask, redirect, render_template, request, session, url_for

import db
import plex_client
from plex_client import PlexClient, log

# One client per process, reused across requests so its internal account/
# token cache (PlexClient.TOKEN_CACHE_SECONDS) actually does something -
# a fresh instance per request would re-hit Plex on every page load.
_plex = None


def _get_plex():
    global _plex
    if _plex is None:
        _plex = PlexClient()
    return _plex


def _account_choices():
    """Live account names for the dropdowns. Returns [] (page still
    renders, just with empty selects) if Plex can't be reached right now -
    a page the admin tool can't open is worse than one with a stale/empty list."""
    try:
        return _get_plex().account_names()
    except Exception as e:
        log("couldn't fetch Plex accounts for the UI:", e)
        return []


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "watch-sync")
    db.init_db()

    @app.route("/")
    def index():
        rules = db.list_rules()
        watermarks = db.all_watermarks()
        for r in rules:
            r["watermarks"] = {s: watermarks.get((r["name"], s)) for s in r["sources"]}
        activity = db.recent_activity(50)
        accounts = _account_choices()
        return render_template("index.html", rules=rules, activity=activity,
                                accounts=accounts, error=request.args.get("error"),
                                plex_linked=bool(db.get_setting("plex_token")))

    @app.route("/plex/connect")
    def plex_connect():
        pin_id, code = plex_client.create_pin()
        session["plex_pin_id"] = pin_id
        forward = url_for("plex_callback", _external=True)
        return redirect(plex_client.auth_url(code, forward))

    @app.route("/plex/callback")
    def plex_callback():
        pin_id = session.pop("plex_pin_id", None)
        if not pin_id:
            return redirect(url_for("index", error="Plex sign-in session expired - try again."))
        token = plex_client.check_pin(pin_id)
        if not token:
            return redirect(url_for("index", error="Plex sign-in didn't finish - try again."))
        db.set_setting("plex_token", token)
        _bump_plex_token_version()
        global _plex
        _plex = None  # force the cached client to pick up the new token
        return redirect(url_for("index"))

    @app.route("/plex/disconnect", methods=["POST"])
    def plex_disconnect():
        db.delete_setting("plex_token")
        _bump_plex_token_version()
        global _plex
        _plex = None
        return redirect(url_for("index"))

    @app.route("/rules", methods=["POST"])
    def add_rule():
        name = request.form.get("name", "").strip()
        sources = request.form.getlist("sources")
        targets = request.form.getlist("targets")
        err = _validate(name, sources, targets, is_new=True)
        if err:
            return redirect(url_for("index", error=err))
        db.save_rule(name, sources, targets)
        return redirect(url_for("index"))

    @app.route("/rules/<name>/edit", methods=["POST"])
    def edit_rule(name):
        sources = request.form.getlist("sources")
        targets = request.form.getlist("targets")
        err = _validate(name, sources, targets, is_new=False)
        if err:
            return redirect(url_for("index", error=err))
        db.save_rule(name, sources, targets)
        return redirect(url_for("index"))

    @app.route("/rules/<name>/delete", methods=["POST"])
    def delete_rule(name):
        db.delete_rule(name)
        return redirect(url_for("index"))

    return app


def _bump_plex_token_version():
    """Signals the background worker (sync_worker._loop) to drop its cached
    PlexClient and build a fresh one, rather than keep using a now-stale
    token until its next unrelated failure."""
    import time
    db.set_setting("plex_token_version", str(time.time()))


def _validate(name, sources, targets, is_new):
    if not name:
        return "Rule name is required."
    if is_new and db.get_rule(name):
        return f"A rule named '{name}' already exists."
    if not sources:
        return "At least one source account is required."
    if not targets:
        return "At least one target account is required."
    overlap = set(s.lower() for s in sources) & set(t.lower() for t in targets)
    if overlap:
        return f"Source and target overlap ({', '.join(overlap)}) - that account would try to sync to itself."
    return None
