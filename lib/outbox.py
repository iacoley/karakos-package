#!/usr/bin/env python3
"""Durable Discord outbox: a table, a retry policy, an audit trail (stdlib).

`data/outbox/outbox.db` is its own file, created empty on first use. A row is
keyed by its own id (`ob-<12 hex>`); `agent` is the posting identity (whose
token sends it), not a queue key. Delivery is at-least-once, narrowed by a
per-chunk nonce the sender passes to Discord. Event `detail` never carries
message content.

Every function takes the connection and `now`; the clock is read only through
the default argument.

CLI (server down): python3 lib/outbox.py [--db PATH] {stats,list,show,retry,discard}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
MAX_ATTEMPTS = 12
MAX_AGE_S = 86400
RATELIMIT_CAP_S = 300
BACKOFF_CAP_S = 3600
DETAIL_MAX = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox(
  id TEXT PRIMARY KEY, created_at REAL, updated_at REAL,
  agent TEXT NOT NULL, channel_id TEXT NOT NULL, reply_to TEXT,
  content TEXT NOT NULL, flags INTEGER DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('pending','sending','delivered','dead','discarded')),
  chunks_total INTEGER, chunks_done INTEGER DEFAULT 0, message_ids TEXT DEFAULT '[]',
  attempts INTEGER DEFAULT 0, next_attempt_at REAL, last_status INTEGER,
  last_error TEXT, dead_reason TEXT, content_sha TEXT NOT NULL,
  queue_message_id TEXT);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_chan ON outbox(agent, channel_id, created_at);
CREATE TABLE IF NOT EXISTS outbox_events(
  id INTEGER PRIMARY KEY, ts REAL, outbox_id TEXT, event TEXT,
  http_status INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_outbox_events_row ON outbox_events(outbox_id);
"""


def _now(now):
    return time.time() if now is None else now


def max_attempts() -> int:
    try:
        return int(os.environ.get("DISCORD_OUTBOX_MAX_ATTEMPTS", MAX_ATTEMPTS))
    except ValueError:
        return MAX_ATTEMPTS


def max_age_s() -> float:
    try:
        return float(os.environ.get("DISCORD_OUTBOX_MAX_AGE_S", MAX_AGE_S))
    except ValueError:
        return MAX_AGE_S


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def ensure_column(conn, table, column, decl):
    """Add `column` to `table` if an older build created the table without it."""
    if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def open_store(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    ensure_column(conn, "outbox", "queue_message_id", "TEXT")
    ensure_column(conn, "outbox", "attachments", "TEXT DEFAULT '[]'")
    ensure_column(conn, "outbox", "allowed_mentions", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_qmid ON outbox(queue_message_id)")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def _event(conn, outbox_id, event, now, http_status=None, detail=None):
    conn.execute(
        "INSERT INTO outbox_events(ts, outbox_id, event, http_status, detail) VALUES (?,?,?,?,?)",
        (now, outbox_id, event, http_status, (detail or "")[:DETAIL_MAX] or None))


def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")


def _row(r):
    return dict(r) if r is not None else None


# --- policy ------------------------------------------------------------------

class AttachmentError(ValueError):
    """An outbound attachment was refused (outside the root, missing, too big)."""


def resolve_attachments(paths, root, max_bytes=25 * 1024 * 1024, max_count=10,
                        max_total=None):
    """Validate outbound attachment paths; return their resolved absolute paths.

    Every path must resolve (symlinks followed) to a regular file inside `root`,
    be non-empty and no larger than `max_bytes`; at most `max_count` files, and
    together no larger than `max_total` (default `max_bytes`, Discord's per-message
    limit). Raises AttachmentError on the first problem so nothing is half-queued.
    """
    paths = list(paths or [])
    if len(paths) > max_count:
        raise AttachmentError(f"too many attachments ({len(paths)} > {max_count})")
    max_total = max_bytes if max_total is None else max_total
    total = 0
    base = Path(root).resolve()
    out = []
    for raw in paths:
        p = Path(raw)
        p = (p if p.is_absolute() else base / p).resolve()
        if base != p and base not in p.parents:
            raise AttachmentError(f"attachment outside {base}: {raw}")
        if not p.is_file():
            raise AttachmentError(f"attachment is not a file: {raw}")
        size = p.stat().st_size
        if size == 0:
            raise AttachmentError(f"attachment is empty: {raw}")
        if size > max_bytes:
            raise AttachmentError(f"attachment too large ({size} > {max_bytes}): {raw}")
        total += size
        if total > max_total:
            raise AttachmentError(f"attachments too large together ({total} > {max_total})")
        out.append(str(p))
    return out


_ATTACH_RE = re.compile(r"^[ \t]*\[\[attach:\s*(.+?)\s*\]\][ \t]*$", re.MULTILINE)


def extract_attachments(text, root, max_bytes=25 * 1024 * 1024, max_count=10,
                        max_total=None):
    """Pull `[[attach: path]]` lines out of a reply.

    Returns (clean_text, paths, notes). Valid files come back as resolved paths;
    a refused marker is dropped from the text and explained in `notes`, so the
    reply still goes out and the author can see why a file did not.
    """
    paths, notes = [], []

    def take(m):
        raw = m.group(1)
        if len(paths) >= max_count:
            notes.append(f"too many attachments (max {max_count}): {raw}")
            return ""
        try:
            # re-validate the accepted set plus this one, so the total cap holds
            ok = resolve_attachments(paths + [raw], root, max_bytes, max_count, max_total)
        except AttachmentError as e:
            notes.append(str(e))
            return ""
        paths[:] = ok
        return ""

    clean = _ATTACH_RE.sub(take, text or "")
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, paths, notes


def backoff_s(attempts: int, jitter: bool = True) -> float:
    base = min(BACKOFF_CAP_S, 5 * 3 ** (max(1, min(attempts, 20)) - 1))
    return base * random.uniform(0.9, 1.1) if jitter else float(base)


def classify(http_status, json_code=None) -> str:
    """'ok' | 'retry' | 'ratelimit' | 'permanent'. None status = network error."""
    if http_status in (200, 201):
        return "ok"
    if http_status == 429:
        return "ratelimit"
    if http_status is None or http_status >= 500 or http_status < 400:
        return "retry"
    return "permanent"


# --- store -------------------------------------------------------------------

def _older_active(conn, agent, channel_id):
    return conn.execute(
        "SELECT 1 FROM outbox WHERE agent=? AND channel_id=? "
        "AND status IN ('pending','sending') LIMIT 1", (agent, channel_id)).fetchone()


def enqueue(conn, agent, channel_id, content, reply_to=None, flags=0, claimed=False,
            now=None, content_sha=None, chunks_total=None, queue_message_id=None,
            attachments=None, allowed_mentions=None):
    """Insert a row. `claimed` (inline owner) is honoured only when no older
    pending/sending row exists for (agent, channel). `attachments` is a list of
    already-validated file paths (see `resolve_attachments`), sent with the
    first chunk. `allowed_mentions` (a dict, or None for Discord's default) is sent
    with every chunk. Returns (id, claimed)."""
    now = _now(now)
    rid = "ob-" + uuid.uuid4().hex[:12]
    sha = content_sha or globals()["content_sha"](content)
    _tx(conn)
    try:
        claimed = bool(claimed) and not _older_active(conn, agent, channel_id)
        conn.execute(
            "INSERT INTO outbox(id, created_at, updated_at, agent, channel_id, reply_to, content,"
            " flags, status, chunks_total, next_attempt_at, content_sha, queue_message_id,"
            " attachments, allowed_mentions)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, now, now, agent, str(channel_id), reply_to, content, flags,
             "sending" if claimed else "pending", chunks_total, now, sha, queue_message_id,
             json.dumps([str(a) for a in (attachments or [])]),
             json.dumps(allowed_mentions) if allowed_mentions is not None else None))
        _event(conn, rid, "enqueued", now, detail="claimed inline" if claimed else "queued")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return rid, claimed


def claim_due(conn, now=None, limit=10):
    """Move due pending rows to sending and return them. A row is claimable only
    when no older pending/sending row exists for its (agent, channel)."""
    now = _now(now)
    cur = conn.execute(
        "UPDATE outbox SET status='sending', updated_at=? WHERE id IN ("
        " SELECT o.id FROM outbox o WHERE o.status='pending' AND o.next_attempt_at <= ?"
        " AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.agent=o.agent AND p.channel_id=o.channel_id"
        "  AND p.status IN ('pending','sending') AND p.id != o.id"
        "  AND (p.created_at < o.created_at OR (p.created_at = o.created_at AND p.rowid < o.rowid)))"
        " ORDER BY o.created_at, o.rowid LIMIT ?) RETURNING *", (now, now, limit))
    rows = [_row(r) for r in cur.fetchall()]
    rows.sort(key=lambda r: r["created_at"])
    return rows


def get(conn, rid):
    return _row(conn.execute("SELECT * FROM outbox WHERE id=?", (rid,)).fetchone())


def record_chunk(conn, rid, message_id, now=None):
    now = _now(now)
    _tx(conn)
    try:
        r = conn.execute("SELECT message_ids, chunks_done FROM outbox WHERE id=? AND status='sending'",
                         (rid,)).fetchone()
        if r is None:
            conn.execute("ROLLBACK")
            return False
        ids = json.loads(r["message_ids"] or "[]") + [message_id]
        conn.execute("UPDATE outbox SET message_ids=?, chunks_done=?, updated_at=?, last_status=200,"
                     " last_error=NULL WHERE id=?", (json.dumps(ids), r["chunks_done"] + 1, now, rid))
        _event(conn, rid, "chunk_delivered", now, 200, f"chunk {r['chunks_done'] + 1}")
        conn.execute("COMMIT")
        return True
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def mark_delivered(conn, rid, now=None):
    now = _now(now)
    _tx(conn)
    try:
        cur = conn.execute("UPDATE outbox SET status='delivered', updated_at=?, next_attempt_at=NULL"
                           " WHERE id=? AND status='sending'", (now, rid))
        if cur.rowcount:
            _event(conn, rid, "delivered", now, 200)
        conn.execute("COMMIT")
        return bool(cur.rowcount)
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def record_failure(conn, rid, kind, http_status=None, detail=None, retry_after=None, now=None):
    """Record a failed attempt; returns the new status ('pending' or 'dead')."""
    now = _now(now)
    _tx(conn)
    try:
        r = conn.execute("SELECT * FROM outbox WHERE id=? AND status='sending'", (rid,)).fetchone()
        if r is None:
            conn.execute("ROLLBACK")
            return None
        attempts = (r["attempts"] or 0) + 1
        detail = (detail or "")[:DETAIL_MAX]
        reason = None
        if kind == "permanent":
            reason = f"permanent failure: {detail or http_status}"
        elif attempts >= max_attempts():
            reason = f"max attempts ({max_attempts()}): {detail or http_status}"
        elif now - (r["created_at"] or now) > max_age_s():
            reason = f"max age ({int(max_age_s())}s): {detail or http_status}"
        _event(conn, rid, "attempt_failed", now, http_status, detail)
        if reason:
            conn.execute("UPDATE outbox SET status='dead', attempts=?, updated_at=?, last_status=?,"
                         " last_error=?, dead_reason=?, next_attempt_at=NULL WHERE id=?",
                         (attempts, now, http_status, detail, reason[:DETAIL_MAX], rid))
            _event(conn, rid, "dead", now, http_status, reason)
            status = "dead"
        else:
            if kind == "ratelimit":
                wait = min(float(retry_after) if retry_after else backoff_s(attempts), RATELIMIT_CAP_S)
            else:
                wait = backoff_s(attempts)
            conn.execute("UPDATE outbox SET status='pending', attempts=?, updated_at=?, last_status=?,"
                         " last_error=?, next_attempt_at=? WHERE id=?",
                         (attempts, now, http_status, detail, now + wait, rid))
            status = "pending"
        conn.execute("COMMIT")
        return status
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def recover_sending(conn, now=None) -> int:
    """Rows left in `sending` by a crash go back to pending."""
    now = _now(now)
    _tx(conn)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM outbox WHERE status='sending'")]
        for rid in ids:
            conn.execute("UPDATE outbox SET status='pending', updated_at=?, next_attempt_at=? WHERE id=?",
                         (now, now, rid))
            _event(conn, rid, "recovered", now, detail="was sending at startup")
        conn.execute("COMMIT")
        return len(ids)
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def next_due(conn):
    r = conn.execute("SELECT MIN(next_attempt_at) FROM outbox WHERE status='pending'").fetchone()
    return r[0]


def stats(conn, now=None) -> dict:
    now = _now(now)
    counts = {s: 0 for s in ("pending", "sending", "dead")}
    for r in conn.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status"):
        if r[0] in counts:
            counts[r[0]] = r[1]
    old = conn.execute("SELECT MIN(created_at) FROM outbox WHERE status IN ('pending','sending')").fetchone()[0]
    counts["oldest_pending_age_s"] = None if old is None else max(now - old, 0.0)
    return counts


def find_by_queue_id(conn, queue_message_id):
    """The outbox row a queue row's reply was enqueued under (stable key, no
    clock). Newest first if a reply was ever enqueued twice."""
    if not queue_message_id:
        return None
    return _row(conn.execute(
        "SELECT * FROM outbox WHERE queue_message_id=? ORDER BY created_at DESC LIMIT 1",
        (str(queue_message_id),)).fetchone())


def find_for_reply(conn, agent, channel_id, content_sha, around_ts, window_s=120):
    """The outbox row for a reply, matched by posting identity, channel and the
    hash of the text as received; nearest created_at within the window."""
    r = conn.execute(
        "SELECT * FROM outbox WHERE agent=? AND channel_id=? AND content_sha=? AND status != 'discarded'"
        " AND created_at BETWEEN ? AND ? ORDER BY ABS(created_at - ?) LIMIT 1",
        (agent, str(channel_id), content_sha, around_ts - window_s, around_ts + window_s, around_ts)).fetchone()
    return _row(r)


def _operator(conn, rid, now, frm, to, event, extra="", extra_args=()):
    _tx(conn)
    try:
        cur = conn.execute(f"UPDATE outbox SET status=?, updated_at=? {extra} WHERE id=? "
                           f"AND status IN ({','.join('?' * len(frm))})", (to, now, *extra_args, rid, *frm))
        if cur.rowcount:
            _event(conn, rid, event, now, detail="operator")
        conn.execute("COMMIT")
        return bool(cur.rowcount)
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def retry(conn, rid, now=None) -> bool:
    now = _now(now)
    return _operator(conn, rid, now, ("dead", "discarded"), "pending", "retried_manually",
                     ", attempts=0, dead_reason=NULL, last_error=NULL, next_attempt_at=?", (now,))


def discard(conn, rid, now=None) -> bool:
    return _operator(conn, rid, _now(now), ("pending", "dead"), "discarded", "discarded",
                     ", next_attempt_at=NULL")


def insert_dead(conn, agent, channel_id, content, reason, created_at, attempts=0, now=None, flags=0):
    """Insert an already-dead row (legacy dead-letter import)."""
    now = _now(now)
    rid = "ob-" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO outbox(id, created_at, updated_at, agent, channel_id, content, flags, status,"
        " attempts, dead_reason, content_sha) VALUES (?,?,?,?,?,?,?,'dead',?,?,?)",
        (rid, created_at, now, agent, str(channel_id), content, flags, attempts,
         reason[:DETAIL_MAX], content_sha(content)))
    _event(conn, rid, "dead", now, detail=reason)
    return rid


def purge(conn, now=None, delivered_days=7, dead_days=90) -> dict:
    now = _now(now)
    _tx(conn)
    try:
        d_cut, x_cut = now - delivered_days * 86400, now - dead_days * 86400
        gone = [r[0] for r in conn.execute(
            "SELECT id FROM outbox WHERE (status='delivered' AND updated_at < ?)"
            " OR (status IN ('dead','discarded') AND updated_at < ?)", (d_cut, x_cut))]
        n_del = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='delivered' AND updated_at < ?",
                             (d_cut,)).fetchone()[0]
        for rid in gone:
            conn.execute("DELETE FROM outbox_events WHERE outbox_id=?", (rid,))
            conn.execute("DELETE FROM outbox WHERE id=?", (rid,))
        if gone:
            _event(conn, None, "purged", now, detail=f"{len(gone)} row(s): {n_del} delivered, "
                                                      f"{len(gone) - n_del} dead/discarded")
        conn.execute("COMMIT")
        return {"purged": len(gone), "delivered": n_del, "dead": len(gone) - n_del}
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# --- reads for the routes and the CLI -----------------------------------------

def list_rows(conn, status=None, limit=50, include_content=False):
    q, args = "SELECT * FROM outbox", []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    rows = [_row(r) for r in conn.execute(q, args)]
    for r in rows:
        if not (include_content and r["status"] == "dead"):
            r.pop("content", None)
    return rows


def show(conn, rid, include_content=False):
    row = get(conn, rid)
    if row is None:
        return None
    if not (include_content and row["status"] == "dead"):
        row.pop("content", None)
    row["events"] = [_row(r) for r in conn.execute(
        "SELECT * FROM outbox_events WHERE outbox_id=? ORDER BY id", (rid,))]
    return row


# --- CLI ------------------------------------------------------------------------

def main(argv=None) -> int:
    default = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")) / "data" / "outbox" / "outbox.db"
    p = argparse.ArgumentParser(prog="outbox")
    p.add_argument("--db", default=str(default))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--include-content", action="store_true")
    for name in ("show", "retry", "discard"):
        sub.add_parser(name).add_argument("id")
    a = p.parse_args(argv)
    if not Path(a.db).is_file():
        print(f"no outbox at {a.db}", file=sys.stderr)
        return 1
    conn = open_store(a.db)
    if a.cmd == "stats":
        out = stats(conn)
    elif a.cmd == "list":
        out = list_rows(conn, a.status, a.limit, a.include_content)
    elif a.cmd == "show":
        out = show(conn, a.id, include_content=True)
    else:
        out = {"id": a.id, a.cmd: (retry if a.cmd == "retry" else discard)(conn, a.id)}
    if out is None or (isinstance(out, dict) and out.get(a.cmd) is False):
        print(json.dumps(out), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
