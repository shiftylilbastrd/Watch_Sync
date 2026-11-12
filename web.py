import os

from flask import Flask, redirect, render_template, request, url_for

import db


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
        return render_template("index.html", rules=rules, activity=activity, error=request.args.get("error"))

    @app.route("/rules", methods=["POST"])
    def add_rule():
        name = request.form.get("name", "").strip()
        sources = _split(request.form.get("sources", ""))
        targets = _split(request.form.get("targets", ""))
        err = _validate(name, sources, targets, is_new=True)
        if err:
            return redirect(url_for("index", error=err))
        db.save_rule(name, sources, targets)
        return redirect(url_for("index"))

    @app.route("/rules/<name>/edit", methods=["POST"])
    def edit_rule(name):
        sources = _split(request.form.get("sources", ""))
        targets = _split(request.form.get("targets", ""))
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


def _split(raw):
    return [s.strip() for s in raw.split(",") if s.strip()]


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
