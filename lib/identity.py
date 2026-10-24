"""Who said it: the Discord user id is the identity, a display name is a label.

Every sender line the agent reads carries the id (`Name (id 123…)`); routing,
allowlists and trust go by the id alone. The `people` table learns which names
an id goes by, per server, because a member's nickname can differ from one
server to the next. The rendered name is the one the person currently has in
the server the message was posted in; a name new to that id is shown with the
one it replaces. Pure helpers plus an aiosqlite writer the agent server calls on
each inbound human message."""
import logging
import re
import time
from typing import Optional

PEOPLE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS people (
        author_id TEXT NOT NULL,
        guild_id TEXT NOT NULL,          -- "dm" outside a server
        display_name TEXT NOT NULL,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        PRIMARY KEY (author_id, guild_id, display_name)
    )
"""

_NO_ID = ("", "0", "None")
_PREV_RE = re.compile(r"\A(.*?)( \(previously [^()]*\))?\Z", re.S)


def has_id(author_id) -> bool:
    return str(author_id if author_id is not None else "") not in _NO_ID


def _plain(name: str) -> str:
    """A display name cannot carry its own `(id …)`: parentheses become brackets."""
    return str(name).replace("(", "[").replace(")", "]")


def label(name, author_id) -> str:
    """`Name (id 123…)`, with a trailing ` (previously Old)` note kept after the
    id. Just the name when there is no real id (local and hive callers use 0)."""
    name = str(name or "unknown")
    if not has_id(author_id):
        return name
    m = _PREV_RE.match(name)
    base, prev = m.group(1), m.group(2) or ""
    return f"{_plain(base)} (id {author_id}){prev}"


def row_label(msg) -> str:
    """`label` for a queue row (sqlite Row or dict); a row without an id column
    gets the bare name."""
    try:
        aid = msg["author_id"]
    except (KeyError, IndexError):
        aid = None
    return label(msg["author"], aid)


async def record_person(db, author_id, guild_id, name, now: Optional[float] = None) -> Optional[str]:
    """Remember that `author_id` was seen as `name` in `guild_id`. Returns the
    name it replaces when `name` is new to this id (never seen under it in any
    server), else None. Never raises: bookkeeping must not block a message."""
    if not has_id(author_id) or not name:
        return None
    now = time.time() if now is None else now
    author_id, name = str(author_id), str(name)
    guild_id = str(guild_id) if guild_id not in (None, "") else "dm"
    try:
        await db.execute(PEOPLE_SCHEMA)
        cur = await db.execute(
            "SELECT display_name FROM people WHERE author_id = ? AND display_name = ? LIMIT 1",
            (author_id, name))
        known = await cur.fetchone() is not None
        previous = None
        if not known:
            cur = await db.execute(
                "SELECT display_name FROM people WHERE author_id = ?"
                " ORDER BY (guild_id = ?) DESC, last_seen DESC LIMIT 1", (author_id, guild_id))
            row = await cur.fetchone()
            previous = row[0] if row else None
        await db.execute(
            "INSERT INTO people (author_id, guild_id, display_name, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(author_id, guild_id, display_name) DO UPDATE SET last_seen = excluded.last_seen",
            (author_id, guild_id, name, now, now))
        await db.commit()
        return previous
    except Exception as e:
        logging.getLogger("identity").warning("people table update failed: %s", e)
        return None
