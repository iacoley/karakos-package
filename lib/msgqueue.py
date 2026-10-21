"""Message-queue claim, expiry and wake-up (spec 1.2).

Pure functions over an aiosqlite connection; no HTTP. `shard` is the value in
message_queue.agent (the shard id; the default shard carries the agent's id).
Named msgqueue, not queue: lib/ is first on sys.path and a queue.py there would
shadow the stdlib module that logging.handlers and aiosqlite import.
"""
import asyncio
import json
import math
import sqlite3
from datetime import datetime, timezone

if sqlite3.sqlite_version_info < (3, 35):
    raise ImportError(
        f"msgqueue needs SQLite 3.35+ for UPDATE ... RETURNING; "
        f"found {sqlite3.sqlite_version}")

STATUS_QUEUED = 0
STATUS_IN_PROGRESS = 1
STATUS_COMPLETE = 2
STATUS_CRASHED = 3
STATUS_SKIPPED = 4

# Rows the server itself inserts for a shard (2.6's handoff and compact turns).
# Claimed alone, like a call row: a human line merged into one would have its
# answer suppressed. Same string as session_policy.HANDOFF_CHANNEL.
INTERNAL_CHANNEL = "handoff"

_ORDER = "priority DESC, created_at ASC, id ASC"
# Which row heads a batch (and so which channel the batch is for): the same
# order, except an ordinary automated row (is_bot=1: heartbeat, system poke)
# yields to a human one of the same priority. Calls and internal rows keep
# their place. Only the choice of channel changes; rows inside the claimed
# batch are still _ORDER.
_HEAD_ORDER = (
    "priority DESC,"
    f" CASE WHEN call_id IS NULL AND channel != '{INTERNAL_CHANNEL}'"
    " AND COALESCE(is_bot, 0) = 1 THEN 1 ELSE 0 END ASC,"
    " created_at ASC, id ASC")
# A reply row (call_id set, reply_to_agent NULL) is read by the waiting caller's
# tool call, never run as a turn (step 2.3).
_NOT_REPLY = "NOT (call_id IS NOT NULL AND reply_to_agent IS NULL)"
_TS = "%Y-%m-%dT%H:%M:%SZ"


def utc_iso(when=None) -> str:
    """UTC ISO string in the one format expires_at is stored and compared in."""
    if when is None:
        when = datetime.now(timezone.utc)
    if isinstance(when, str):
        return when
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, tz=timezone.utc)
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime(_TS)


async def write_commit(db, sql, params=(), fetch=False):
    """Run one write statement and commit it in a single hop to the
    connection's thread; returns (rows, rowcount). `execute()` then `commit()`
    is two hops, and between them the connection holds SQLite's write lock while
    waiting on the event loop. Anything that blocks the loop in that gap
    (a synchronous writer sharing the file, as the harness tests have) can never
    be answered by our own commit, and waits out its whole busy timeout."""
    def job(conn):
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if fetch else None
            n = cur.rowcount
            cur.close()
            conn.commit()
            return rows, n
        except BaseException:
            conn.rollback()
            raise
    return await db._execute(job, db._conn)


async def expire(db, shard, now=None) -> int:
    """Skip queued rows past expires_at; tell a waiting caller. Returns count."""
    now = utc_iso(now)
    # execute_fetchall runs the statement and drains it in one hop to the
    # connection's thread. With execute() then fetchall() a RETURNING statement
    # stays pending across an await, and another shard's commit() on the shared
    # connection fails with "SQL statements in progress".
    def job(conn):
        try:
            rows = conn.execute(
                "UPDATE message_queue SET processed = ?, response = 'expired',"
                " processed_at = CURRENT_TIMESTAMP"
                " WHERE agent = ? AND processed = ? AND expires_at IS NOT NULL"
                " AND expires_at < ?"
                " RETURNING id, call_id, reply_to_agent",
                (STATUS_SKIPPED, shard, STATUS_QUEUED, now)).fetchall()
            for r in rows:
                if r["call_id"] and r["reply_to_agent"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO message_queue"
                        " (agent, channel, channel_id, server, author, author_id, is_bot,"
                        "  content, message_id, call_id, owner_agent)"
                        " VALUES (?, 'call', '0', 'local', ?, '0', 1, ?, ?, ?, ?)",
                        (r["reply_to_agent"], shard,
                         json.dumps({"error": "expired", "call_id": r["call_id"]}),
                         f"expired-{r['call_id']}-{r['id']}", r["call_id"],
                         r["reply_to_agent"]))
            conn.commit()
            return rows
        except BaseException:
            conn.rollback()
            raise
    rows = await db._execute(job, db._conn)
    if rows:
        for target in {r["reply_to_agent"] for r in rows
                       if r["call_id"] and r["reply_to_agent"]}:
            notify(target)
    return len(rows)


async def claim_batch(db, shard, limit, now=None) -> list:
    """Claim up to `limit` queued rows for `shard`; returns exactly the rows
    this caller now owns, ordered priority DESC, created_at, id.

    One UPDATE ... RETURNING whose candidates come from a subquery, so a row is
    never in two callers' results. A call row (call_id set) or an internal row
    (channel INTERNAL_CHANNEL) is claimed alone; ordinary rows never share a
    batch with one. An ordinary batch holds rows of one channel_id only (the
    head row's), so the reply posts where its rows came from; other channels'
    rows wait for the next turn. The head is the first row by priority, a human
    row before an automated one, then age (_HEAD_ORDER).
    """
    await expire(db, shard, now)
    heads = await db.execute_fetchall(
        f"SELECT id, call_id, channel, channel_id FROM message_queue"
        f" WHERE agent = ? AND processed = ?"
        f" AND {_NOT_REPLY} ORDER BY {_HEAD_ORDER} LIMIT 1", (shard, STATUS_QUEUED))
    head = heads[0] if heads else None
    if head is None:
        return []
    if head["call_id"] is not None or head["channel"] == INTERNAL_CHANNEL:
        where, args = "id = ?", (head["id"],)
    else:
        where = (f"id IN (SELECT id FROM message_queue WHERE agent = ?"
                 f" AND processed = ? AND call_id IS NULL"
                 f" AND channel != '{INTERNAL_CHANNEL}' AND channel_id IS ?"
                 f" ORDER BY {_ORDER} LIMIT ?)")
        args = (shard, STATUS_QUEUED, head["channel_id"], limit)
    rows, _ = await write_commit(
        db,
        "UPDATE message_queue SET processed = ?, claimed_by = ?,"
        " processing_started_at = CURRENT_TIMESTAMP"
        f" WHERE {where} AND processed = ? AND {_NOT_REPLY} RETURNING *",
        (STATUS_IN_PROGRESS, shard, *args, STATUS_QUEUED), fetch=True)
    return sorted(rows, key=lambda r: (-(r["priority"] or 0), r["created_at"], r["id"]))


async def claim_steerable(db, shard, channel_id, limit, now=None) -> list:
    """Claim up to `limit` queued rows of `shard` that may be written into the
    turn already in flight (step 2.5): the claim_batch shape, plus no call or
    reply row, no edit follow-up (it runs next), priority 0, and the in-flight turn's channel. Never claims an
    expired row. Returns exactly the rows this caller now owns, in dispatch
    order."""
    await expire(db, shard, now)
    rows, _ = await write_commit(
        db,
        "UPDATE message_queue SET processed = ?, claimed_by = ?,"
        " processing_started_at = CURRENT_TIMESTAMP"
        " WHERE id IN (SELECT id FROM message_queue WHERE agent = ? AND processed = ?"
        "  AND call_id IS NULL AND reply_to_agent IS NULL AND priority = 0"
        "  AND message_id NOT LIKE 'edit:%'"
        f" AND channel != '{INTERNAL_CHANNEL}'"
        f" AND channel_id = ? ORDER BY {_ORDER} LIMIT ?)"
        " AND processed = ? RETURNING *",
        (STATUS_IN_PROGRESS, shard, shard, STATUS_QUEUED, str(channel_id),
         int(limit), STATUS_QUEUED), fetch=True)
    return sorted(rows, key=lambda r: (r["created_at"], r["id"]))


# Rows a thief may take: plain human/bot rows, never addressed to one shard.
def _stealable(a=""):
    return (f"{a}call_id IS NULL AND {a}reply_to_agent IS NULL"
            f" AND {a}channel NOT IN ('hive', 'call')"
            f" AND {a}priority = 0"
            f" AND ({a}not_before IS NULL OR {a}not_before <= :now_epoch)")


_AGE_FMT = "%Y-%m-%d %H:%M:%S"


def _now_dt(now):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, tz=timezone.utc)
    if isinstance(now, str):
        return datetime.strptime(now, _TS).replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _age_cutoff(now_dt, min_age_s) -> str:
    """Newest created_at a thief may take. created_at has one-second
    resolution (the true time is in [c, c+1)), so the floor is rounded up:
    a row is stealable only once c + 1 <= now - min_age_s, i.e. never younger."""
    t = now_dt.timestamp() - float(min_age_s)
    return datetime.fromtimestamp(math.floor(t) - 1, tz=timezone.utc).strftime(_AGE_FMT)


async def claim_stolen(db, thief, victim, limit, min_age_s, now=None) -> list:
    """Claim up to `limit` of `victim`'s waiting rows for `thief` (step 2.4).

    The row keeps agent = victim and gets claimed_by = thief. Same single
    UPDATE ... RETURNING shape as claim_batch, so a row is never in two
    callers' results. Never takes a call, reply, buzz or priority row, a row
    held behind a wall, an expired or too-young row, or anything in a channel
    the victim is mid-conversation in (or has an earlier row there that a thief
    may not take). Channel '0' is exempt from continuity.
    """
    now_dt = _now_dt(now)
    await expire(db, victim, now_dt)
    params = {"victim": victim, "thief": thief, "limit": int(limit),
              "q": STATUS_QUEUED, "ip": STATUS_IN_PROGRESS,
              "now_epoch": int(now_dt.timestamp()),
              "cutoff": _age_cutoff(now_dt, min_age_s)}
    eligible = (
        " FROM message_queue m"
        "  WHERE m.agent = :victim AND m.processed = :q"
        f"  AND {_stealable('m.')}"
        "  AND m.created_at <= :cutoff"
        "  AND (m.channel_id = '0' OR ("
        "   NOT EXISTS (SELECT 1 FROM message_queue p WHERE p.agent = :victim"
        "    AND p.channel_id = m.channel_id AND p.processed = :ip)"
        "   AND NOT EXISTS (SELECT 1 FROM message_queue e WHERE e.agent = :victim"
        "    AND e.channel_id = m.channel_id AND e.processed = :q"
        "    AND (e.created_at < m.created_at"
        "         OR (e.created_at = m.created_at AND e.id < m.id))"
        f"    AND NOT ({_stealable('e.')} AND e.created_at <= :cutoff))))")
    # One channel per stolen batch (the reply posts in the batch's channel):
    # the first takeable row's. The inner SELECT re-uses `eligible` unchanged.
    rows, _ = await write_commit(
        db,
        "UPDATE message_queue SET processed = :ip, claimed_by = :thief,"
        " processing_started_at = CURRENT_TIMESTAMP"
        f" WHERE id IN (SELECT m.id{eligible}"
        "  AND m.channel_id IS (SELECT m.channel_id"
        f"{eligible} ORDER BY m.created_at ASC, m.id ASC LIMIT 1)"
        "  ORDER BY m.created_at ASC, m.id ASC LIMIT :limit)"
        " AND processed = :q RETURNING *", params, fetch=True)
    return sorted(rows, key=lambda r: (r["created_at"], r["id"]))


async def steal_wait_s(db, victim, min_age_s, now=None):
    """Seconds until the oldest otherwise-stealable row of `victim` is old
    enough to take (<= 0 when one already is), or None when no row of the
    victim is of a stealable kind. Lets the caller arm one timer instead of
    polling."""
    now_dt = _now_dt(now)
    rows = await db.execute_fetchall(
        "SELECT MIN(created_at) AS c FROM message_queue"
        f" WHERE agent = :victim AND processed = :q AND {_stealable()}"
        " AND (expires_at IS NULL OR expires_at >= :exp)",
        {"victim": victim, "q": STATUS_QUEUED,
         "now_epoch": int(now_dt.timestamp()), "exp": utc_iso(now_dt)})
    c = rows[0]["c"] if rows else None
    if not c:
        return None
    t = datetime.strptime(c, _AGE_FMT).replace(tzinfo=timezone.utc).timestamp()
    return t + 1 + float(min_age_s) - now_dt.timestamp()


async def queued_depths(db, shards) -> dict:
    """Queued row count per shard, not counting reply rows (nobody runs those
    as a turn). Every requested shard appears."""
    shards = list(shards)
    out = {s: 0 for s in shards}
    if not shards:
        return out
    rows = await db.execute_fetchall(
        f"SELECT agent, COUNT(*) AS n FROM message_queue WHERE processed = ?"
        f" AND {_NOT_REPLY} AND agent IN ({','.join('?' * len(shards))})"
        " GROUP BY agent", (STATUS_QUEUED, *shards))
    for r in rows:
        out[r["agent"]] = r["n"]
    return out


async def reap_hive_rows(db, now=None, ttl=600) -> int:
    """Skip reply rows nobody consumed within `ttl` seconds (step 2.3)."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif isinstance(now, (int, float)):
        now = datetime.fromtimestamp(now, tz=timezone.utc)
    cutoff = datetime.fromtimestamp(now.timestamp() - ttl, tz=timezone.utc)
    rows, _ = await write_commit(
        db,
        "UPDATE message_queue SET processed = ?, response = 'stale',"
        " processed_at = CURRENT_TIMESTAMP"
        " WHERE processed = ? AND call_id IS NOT NULL AND reply_to_agent IS NULL"
        " AND created_at <= ? RETURNING id",
        (STATUS_SKIPPED, STATUS_QUEUED, cutoff.strftime("%Y-%m-%d %H:%M:%S")), fetch=True)
    return len(rows)


async def fail_calls(db, shard, code, detail="") -> int:
    """Answer every queued call row addressed to `shard` with an error reply
    row {"call_id", "error": code, "detail"} (same insert `expire` uses) and
    skip the call row (`response = code`). Returns the count."""
    def job(conn):
        try:
            rows = conn.execute(
                "UPDATE message_queue SET processed = ?, response = ?,"
                " processed_at = CURRENT_TIMESTAMP"
                " WHERE agent = ? AND processed = ? AND call_id IS NOT NULL"
                " AND reply_to_agent IS NOT NULL"
                " RETURNING id, call_id, reply_to_agent",
                (STATUS_SKIPPED, code, shard, STATUS_QUEUED)).fetchall()
            for r in rows:
                if r["reply_to_agent"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO message_queue"
                        " (agent, channel, channel_id, server, author, author_id, is_bot,"
                        "  content, message_id, call_id, owner_agent)"
                        " VALUES (?, 'call', '0', 'local', ?, '0', 1, ?, ?, ?, ?)",
                        (r["reply_to_agent"], shard,
                         json.dumps({"call_id": r["call_id"], "error": code, "detail": detail}),
                         f"{code}-{r['call_id']}-{r['id']}", r["call_id"],
                         r["reply_to_agent"]))
            conn.commit()
            return rows
        except BaseException:
            conn.rollback()
            raise
    rows = await db._execute(job, db._conn)  # one hop: no write lock across an await
    for target in {r["reply_to_agent"] for r in rows if r["reply_to_agent"]}:
        notify(target)
    return len(rows)


async def peek_claimable(db, shard, limit=20, now=None) -> list:
    """The rows the next `claim_batch(db, shard, limit)` would claim, without
    claiming them: same predicates and ordering, no UPDATE, and no expiry side
    effects (an already-expired row is simply left out, as claim_batch's
    expire step would have skipped it first)."""
    live = " AND (expires_at IS NULL OR expires_at >= ?)"
    now = utc_iso(now)
    heads = await db.execute_fetchall(
        f"SELECT id, call_id, channel, channel_id FROM message_queue"
        f" WHERE agent = ? AND processed = ?{live}"
        f" AND {_NOT_REPLY}"
        f" ORDER BY {_HEAD_ORDER} LIMIT 1", (shard, STATUS_QUEUED, now))
    head = heads[0] if heads else None
    if head is None:
        return []
    if head["call_id"] is not None or head["channel"] == INTERNAL_CHANNEL:
        return list(await db.execute_fetchall(
            "SELECT * FROM message_queue WHERE id = ?", (head["id"],)))
    rows = await db.execute_fetchall(
        f"SELECT * FROM message_queue WHERE agent = ? AND processed = ?"
        f" AND call_id IS NULL AND channel != '{INTERNAL_CHANNEL}'{live}"
        f" AND channel_id IS ? AND {_NOT_REPLY} ORDER BY {_ORDER} LIMIT ?",
        (shard, STATUS_QUEUED, now, head["channel_id"], limit))
    return sorted(rows, key=lambda r: (-(r["priority"] or 0), r["created_at"], r["id"]))


async def release(db, ids) -> int:
    """Return claimed rows to the queue (still-in-progress only)."""
    ids = list(ids)
    if not ids:
        return 0
    _, n = await write_commit(
        db,
        "UPDATE message_queue SET processed = ?, claimed_by = NULL,"
        " processing_started_at = NULL"
        f" WHERE id IN ({','.join('?' * len(ids))}) AND processed = ?",
        (STATUS_QUEUED, *ids, STATUS_IN_PROGRESS))
    return n


async def set_partial(db, id, text) -> None:
    await write_commit(db, "UPDATE message_queue SET partial_response = ? WHERE id = ?",
                       (text, id))


# --- wake-up ---------------------------------------------------------------
# One Condition per (event loop, shard); the loop in the key keeps tests that
# run a fresh loop each from sharing a Condition across loops.
_conds: dict = {}
# Per-(loop, shard) count of notify() calls, bumped synchronously. A waiter that
# read the count before it looked at the database passes it as `since`, so a
# notify that fires between its check and its wait is not lost (a Condition
# wake-up only reaches tasks already waiting).
_versions: dict = {}


def work_version(shard) -> int:
    return _versions.get((asyncio.get_running_loop(), shard), 0)


def _cond(shard) -> asyncio.Condition:
    key = (asyncio.get_running_loop(), shard)
    c = _conds.get(key)
    if c is None:
        c = _conds[key] = asyncio.Condition()
    return c


async def _notify(shard) -> None:
    c = _cond(shard)
    async with c:
        c.notify_all()


def notify(shard) -> None:
    """Wake waiters on `shard`. Sync; call from inside the running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    key = (asyncio.get_running_loop(), shard)
    _versions[key] = _versions.get(key, 0) + 1
    asyncio.get_running_loop().create_task(_notify(shard))


async def wait_for_work(shard, timeout, since=None) -> bool:
    """Block until `shard` is notified (True) or `timeout` seconds pass (False).
    With `since` (a work_version() read before the caller checked its state),
    returns True at once if a notify already happened after that read."""
    c = _cond(shard)
    async with c:
        if since is not None and work_version(shard) != since:
            return True
        try:
            await asyncio.wait_for(c.wait(), timeout)
        except asyncio.TimeoutError:
            return False
    return True
