"""SQLite storage for watch_sync: sync rules, per-rule/per-source watermarks,
and a capped recent-activity log the web UI shows."""
import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("DB_PATH", "/data/watch_sync.db")
ACTIVITY_KEEP = 300


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS rules (
            name TEXT PRIMARY KEY,
            sources TEXT NOT NULL,
            targets TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS watermarks (
            rule_name TEXT NOT NULL,
            source TEXT NOT NULL,
            last_viewed_at INTEGER NOT NULL,
            PRIMARY KEY (rule_name, source)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT NOT NULL,
            target TEXT NOT NULL,
            label TEXT NOT NULL,
            watched_by TEXT NOT NULL,
            status TEXT NOT NULL,
            at TEXT NOT NULL
        )""")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- rules ---------------------------------------------------------------

def list_rules():
    with _conn() as c:
        rows = c.execute("SELECT * FROM rules ORDER BY name").fetchall()
    return [_rule_dict(r) for r in rows]


def get_rule(name):
    with _conn() as c:
        row = c.execute("SELECT * FROM rules WHERE name=?", (name,)).fetchone()
    return _rule_dict(row) if row else None


def _rule_dict(row):
    return {
        "name": row["name"],
        "sources": json.loads(row["sources"]),
        "targets": json.loads(row["targets"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_rule(name, sources, targets):
    """Insert or fully replace a rule's sources/targets. Account names are
    stored lowercased since Plex account lookups (PlexClient.accounts(),
    user_tokens()) are keyed lowercase - matching the old script's behavior."""
    sources = [s.strip().lower() for s in sources if s.strip()]
    targets = [t.strip().lower() for t in targets if t.strip()]
    ts = now()
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM rules WHERE name=?", (name,)).fetchone()
        if exists:
            c.execute("UPDATE rules SET sources=?, targets=?, updated_at=? WHERE name=?",
                      (json.dumps(sources), json.dumps(targets), ts, name))
        else:
            c.execute("INSERT INTO rules (name, sources, targets, created_at, updated_at) VALUES (?,?,?,?,?)",
                      (name, json.dumps(sources), json.dumps(targets), ts, ts))


def delete_rule(name):
    with _conn() as c:
        c.execute("DELETE FROM rules WHERE name=?", (name,))
        c.execute("DELETE FROM watermarks WHERE rule_name=?", (name,))


# --- watermarks ------------------------------------------------------------

def get_watermark(rule_name, source, default):
    with _conn() as c:
        row = c.execute("SELECT last_viewed_at FROM watermarks WHERE rule_name=? AND source=?",
                         (rule_name, source)).fetchone()
    return row["last_viewed_at"] if row else default


def set_watermark(rule_name, source, value):
    with _conn() as c:
        c.execute("""INSERT INTO watermarks (rule_name, source, last_viewed_at) VALUES (?,?,?)
                     ON CONFLICT(rule_name, source) DO UPDATE SET last_viewed_at=excluded.last_viewed_at""",
                  (rule_name, source, value))


def all_watermarks():
    with _conn() as c:
        rows = c.execute("SELECT * FROM watermarks").fetchall()
    return {(r["rule_name"], r["source"]): r["last_viewed_at"] for r in rows}


# --- activity log ------------------------------------------------------------

def log_activity(rule_name, target, label, watched_by, status):
    with _conn() as c:
        c.execute("INSERT INTO activity (rule_name, target, label, watched_by, status, at) VALUES (?,?,?,?,?,?)",
                  (rule_name, target, label, watched_by, status, now()))
        c.execute("""DELETE FROM activity WHERE id NOT IN (
                        SELECT id FROM activity ORDER BY id DESC LIMIT ?
                     )""", (ACTIVITY_KEEP,))


def recent_activity(limit=50):
    with _conn() as c:
        rows = c.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
