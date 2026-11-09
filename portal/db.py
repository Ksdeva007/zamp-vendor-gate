"""SQLite storage. Each call opens its own connection, so the background run thread and web requests don't collide."""
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config
from .validators import acct_hash, acct_last4, make_gstin

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "vendor_portal.db"))
DRAFT_MAX_AGE_DAYS = 30

KAVERI_PAN = "AABCK7316M"
NORTHWIND_ACCOUNT = "50100234567890"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    vendor TEXT NOT NULL,
    country TEXT,
    sample_id TEXT,
    parent_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    form_json TEXT NOT NULL,            -- account number masked
    state TEXT NOT NULL,                -- running | done | error
    active_stage INTEGER,
    status TEXT,                        -- approved | pending | rejected
    headline TEXT,
    owner TEXT,
    vendor_id TEXT,
    added_to_master INTEGER NOT NULL DEFAULT 0,
    ai_used_json TEXT NOT NULL DEFAULT '[]',
    message_to TEXT,
    message_subject TEXT,
    message_body TEXT,
    message_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS stages (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    ms INTEGER,
    PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage_idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,               -- pass | warn | fixable | verify | block | info | skip
    detail TEXT,                        -- internal reasoning, never sent to the vendor
    evidence_json TEXT NOT NULL DEFAULT '[]',
    field TEXT,                         -- vendor-safe: which entry
    entered TEXT,                       -- vendor-safe: what they gave
    expected TEXT,                      -- vendor-safe: what it should be
    action TEXT,                        -- vendor-safe: what to do
    kind TEXT                           -- entry | missing | review
);
CREATE INDEX IF NOT EXISTS idx_checks_run ON checks(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL,
    country TEXT,
    tax_id TEXT,
    pan TEXT,
    acct_hash TEXT,
    acct_last4 TEXT,
    contact_name TEXT,
    contact_email TEXT,
    since TEXT,
    seeded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watchlist (
    name TEXT PRIMARY KEY,
    why TEXT NOT NULL
);
-- A registration being filled in. Removed once the vendor is approved or rejected;
-- a pending one is kept so it can be corrected and resubmitted.
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sample_id TEXT,
    parent_id TEXT,
    submitted_run_id TEXT,
    values_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_documents (
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    slot TEXT NOT NULL,
    name TEXT NOT NULL,
    mime TEXT,
    size INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (draft_id, slot)
);
"""

SEED_VENDORS = [
    ("V-1042", "Kaveri Industrial Supplies Private Limited", "IN", make_gstin("33", KAVERI_PAN), KAVERI_PAN, "50100234567812", "Meena Iyer", "meena.iyer@kaverisupplies.example", "2023-06-14"),
    ("V-1077", "Northwind Logistics Pvt Ltd", "IN", "29AABCN1234F1Z5", "", NORTHWIND_ACCOUNT, "Tom Ashworth", "tom@northwindlogistics.example", "2024-02-02"),
    ("V-1103", "Contoso Supplies LLC", "US", "12-9876543", "", "000123456789", "Dana Reyes", "dana@contososupplies.example", "2024-09-20"),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        for vid, name, country, tax, pan, acct, cname, cmail, since in SEED_VENDORS:
            c.execute(
                "INSERT OR REPLACE INTO vendors VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (vid, name, country, tax, pan, acct_hash(acct), acct_last4(acct), cname, cmail, since),
            )
        c.executemany("INSERT OR IGNORE INTO watchlist (name, why) VALUES (:name, :why)", config.WATCHLIST)
        # A restart ends any run that was in progress.
        c.execute("UPDATE runs SET state='error', active_stage=NULL WHERE state='running'")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=DRAFT_MAX_AGE_DAYS)).isoformat(timespec="seconds")
        c.execute("DELETE FROM drafts WHERE updated_at < ?", (cutoff,))


# Drafts
def create_draft(values, sample_id=None, parent_id=None, documents=None):
    draft_id = "DR-" + secrets.token_urlsafe(12)
    stamp = now()
    with connect() as c:
        c.execute("INSERT INTO drafts (id, created_at, updated_at, sample_id, parent_id, values_json) VALUES (?,?,?,?,?,?)", (draft_id, stamp, stamp, sample_id, parent_id, json.dumps(values)))
        for slot, d in (documents or {}).items():
            c.execute("INSERT INTO draft_documents (draft_id, slot, name, mime, size, data) VALUES (?,?,?,?,?,?)", (draft_id, slot, d["name"], d.get("type"), len(d["bytes"]), d["bytes"]))
    return draft_id


def get_draft(draft_id):
    with connect() as c:
        row = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            return None
        draft = dict(row)
        draft["values"] = json.loads(draft.pop("values_json"))
        draft["documents"] = {r["slot"]: dict(r) for r in c.execute("SELECT slot, name, mime, size FROM draft_documents WHERE draft_id=?", (draft_id,))}
    return draft


def update_draft(draft_id, values, sample_id):
    with connect() as c:
        c.execute("UPDATE drafts SET values_json=?, sample_id=?, updated_at=? WHERE id=?", (json.dumps(values), sample_id, now(), draft_id))


def put_document(draft_id, slot, name, mime, data):
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO draft_documents (draft_id, slot, name, mime, size, data) VALUES (?,?,?,?,?,?)", (draft_id, slot, name, mime, len(data), data))
        c.execute("UPDATE drafts SET sample_id=NULL, updated_at=? WHERE id=?", (now(), draft_id))


def delete_document(draft_id, slot):
    with connect() as c:
        c.execute("DELETE FROM draft_documents WHERE draft_id=? AND slot=?", (draft_id, slot))
        c.execute("UPDATE drafts SET sample_id=NULL, updated_at=? WHERE id=?", (now(), draft_id))


def get_document(draft_id, slot):
    with connect() as c:
        row = c.execute("SELECT name, mime, data FROM draft_documents WHERE draft_id=? AND slot=?", (draft_id, slot)).fetchone()
    return dict(row) if row else None


def load_documents(draft_id, slots):
    with connect() as c:
        rows = c.execute(f"SELECT slot, name, mime, data FROM draft_documents WHERE draft_id=? AND slot IN ({','.join('?' * len(slots))})", (draft_id, *slots)).fetchall() if slots else []
    return {r["slot"]: {"name": r["name"], "type": r["mime"], "bytes": r["data"]} for r in rows}


def mark_draft_submitted(draft_id, run_id):
    with connect() as c:
        c.execute("UPDATE drafts SET submitted_run_id=?, updated_at=? WHERE id=?", (run_id, now(), draft_id))


def delete_draft(draft_id):
    with connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (draft_id,))


def find_draft_for_run(run_id):
    """The draft a pending run came from, or the correction already started from it."""
    with connect() as c:
        row = c.execute("SELECT id FROM drafts WHERE submitted_run_id=? OR parent_id=? ORDER BY updated_at DESC LIMIT 1", (run_id, run_id)).fetchone()
    return row["id"] if row else None


def reopen_draft(draft_id, parent_id):
    with connect() as c:
        c.execute("UPDATE drafts SET submitted_run_id=NULL, parent_id=?, sample_id=NULL, updated_at=? WHERE id=?", (parent_id, now(), draft_id))


# Runs
def create_run(run_id, vendor, country, sample_id, parent_id, form):
    with connect() as c:
        c.execute(
            "INSERT INTO runs (id, created_at, vendor, country, sample_id, parent_id, form_json, state, active_stage) VALUES (?,?,?,?,?,?,?,'running',0)",
            (run_id, now(), vendor, country, sample_id, parent_id, json.dumps(form)),
        )
        c.executemany("INSERT INTO stages (run_id, idx, title) VALUES (?,?,?)", [(run_id, i, t) for i, t in enumerate(config.STAGES)])


def set_active_stage(run_id, idx):
    with connect() as c:
        c.execute("UPDATE runs SET active_stage=? WHERE id=?", (idx, run_id))


def finish_stage(run_id, idx, ms):
    with connect() as c:
        c.execute("UPDATE stages SET ms=? WHERE run_id=? AND idx=?", (ms, run_id, idx))


def add_check(run_id, stage_idx, title, status, detail="", evidence=None, field=None, entered=None, expected=None, action=None, kind=None):
    with connect() as c:
        c.execute(
            "INSERT INTO checks (run_id, stage_idx, title, status, detail, evidence_json, field, entered, expected, action, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, stage_idx, title, status, detail, json.dumps(evidence or []), field, entered, expected, action, kind),
        )


def finish_run(run_id, status, headline, owner, vendor_id, added, ai_used, message):
    with connect() as c:
        c.execute(
            "UPDATE runs SET state='done', active_stage=NULL, status=?, headline=?, owner=?, vendor_id=?, added_to_master=?, ai_used_json=?, message_to=?, message_subject=?, message_body=? WHERE id=?",
            (status, headline, owner, vendor_id, int(added), json.dumps(ai_used), message["to"], message["subject"], message["body"], run_id),
        )


def fail_run(run_id):
    with connect() as c:
        c.execute("UPDATE runs SET state='error', active_stage=NULL WHERE id=?", (run_id,))


def update_message(run_id, subject, body):
    """Saves a person's edit. Returns False if there is no message, or it has already gone out."""
    with connect() as c:
        cur = c.execute("UPDATE runs SET message_subject=?, message_body=? WHERE id=? AND message_body IS NOT NULL AND message_sent_at IS NULL", (subject, body, run_id))
        return cur.rowcount > 0


def mark_sent(run_id):
    """Returns False if the run has no message. Keeps the first timestamp if clicked twice."""
    with connect() as c:
        cur = c.execute("UPDATE runs SET message_sent_at=COALESCE(message_sent_at, ?) WHERE id=? AND message_body IS NOT NULL", (now(), run_id))
        return cur.rowcount > 0


def run_exists(run_id):
    with connect() as c:
        return c.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is not None


def get_run(run_id):
    with connect() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        run["ai_used"] = json.loads(run.pop("ai_used_json"))
        run["form"] = json.loads(run.pop("form_json"))
        run["added_to_master"] = bool(run["added_to_master"])
        checks = [dict(r) for r in c.execute("SELECT * FROM checks WHERE run_id=? ORDER BY id", (run_id,))]
        run["stages"] = [dict(r) for r in c.execute("SELECT idx, title, ms FROM stages WHERE run_id=? ORDER BY idx", (run_id,))]
    for ch in checks:
        ch["evidence"] = json.loads(ch.pop("evidence_json"))
    for s in run["stages"]:
        s["checks"] = [ch for ch in checks if ch["stage_idx"] == s["idx"]]
    return run


_ISSUE_RANK = "CASE status WHEN 'block' THEN 0 WHEN 'verify' THEN 1 WHEN 'fixable' THEN 2 ELSE 3 END"


def search_runs(status=None, query=None, limit=20, offset=0):
    """One page of submissions plus totals, all worked out in SQL."""
    where, params = [], []
    if status:
        where.append("r.status = ?")
        params.append(status)
    if query:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(r.vendor LIKE ? ESCAPE '\\' OR r.id LIKE ? ESCAPE '\\')")
        params += [like, like]
    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT r.id, r.created_at, r.vendor, r.country, r.sample_id, r.parent_id, r.state, r.status, r.owner, r.message_sent_at,
               (SELECT title FROM checks c WHERE c.run_id = r.id AND c.status IN ('block','verify','fixable') ORDER BY {_ISSUE_RANK}, c.id LIMIT 1) AS main_reason,
               (SELECT COUNT(*) FROM checks c WHERE c.run_id = r.id AND c.status IN ('block','verify','fixable')) AS issue_count
        FROM runs r {clause} ORDER BY r.created_at DESC, r.rowid DESC LIMIT ? OFFSET ?"""
    with connect() as c:
        items = [dict(row) for row in c.execute(sql, (*params, limit, offset))]
        total = c.execute(f"SELECT COUNT(*) FROM runs r {clause}", params).fetchone()[0]
        counts = {row["status"]: row["n"] for row in c.execute("SELECT status, COUNT(*) AS n FROM runs GROUP BY status")}
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "stats": {"all": sum(counts.values()), "approved": counts.get("approved", 0), "pending": counts.get("pending", 0), "rejected": counts.get("rejected", 0)},
    }


def clear_history():
    """Deletes runs, drafts and vendors added by runs. Seeded vendors stay."""
    with connect() as c:
        c.execute("DELETE FROM drafts")
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM vendors WHERE seeded=0")


# Vendor master and watchlist
def list_vendors():
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM vendors ORDER BY vendor_id")]


def list_watchlist():
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT name, why FROM watchlist")]


def _next_id(c):
    highest = c.execute("SELECT MAX(CAST(SUBSTR(vendor_id, 3) AS INTEGER)) FROM vendors WHERE seeded=0").fetchone()[0]
    return f"V-{(highest or 2000) + 1}"


def next_vendor_id():
    with connect() as c:
        return _next_id(c)


def add_vendor(legal_name, country, tax_id, pan, account, contact_name, contact_email):
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        vendor_id = _next_id(c)
        c.execute(
            "INSERT INTO vendors VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (vendor_id, legal_name, country, tax_id, pan, acct_hash(account), acct_last4(account), contact_name, contact_email, now()[:10]),
        )
    return vendor_id
