"""Graph schema. ensure_schema() creates; check_schema() only reads."""
import sqlite3

SCHEMA_VERSION = 1


class GraphNotInitialised(Exception):
    """The graph database is missing, empty, or newer than this code."""


# Created lazily by the first agent-facing delete on an existing graph (no
# schema bump), and with everything else on a new one.
AUDIT_DDL = """CREATE TABLE IF NOT EXISTS memory_audit (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  agent TEXT,
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id INTEGER,
  reason TEXT,
  detail TEXT
)"""

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'thing',
  summary TEXT,
  importance REAL DEFAULT 5.0,
  created_at TEXT, updated_at TEXT, last_seen_at TEXT, archived_at TEXT,
  embedding BLOB, embed_model TEXT,
  UNIQUE(name_norm, kind)
);
CREATE TABLE IF NOT EXISTS entity_aliases (
  entity_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  alias_norm TEXT,
  PRIMARY KEY(entity_id, alias_norm)
);
CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY,
  src_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  dst_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  relation TEXT NOT NULL,
  weight REAL DEFAULT 1.0,
  created_at TEXT, updated_at TEXT,
  UNIQUE(src_id, dst_id, relation),
  CHECK(src_id != dst_id)
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('fact','episode','pattern')),
  subkind TEXT,
  content TEXT NOT NULL,
  entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL,
  importance REAL DEFAULT 5.0,
  base_importance REAL,
  confidence REAL,
  domain TEXT, agent TEXT, channel TEXT, tags TEXT,
  reinforcement_count INTEGER DEFAULT 1,
  source TEXT NOT NULL DEFAULT 'write'
    CHECK(source IN ('write','nightly','migrated','candidate')),
  legacy_ref TEXT,
  created_at TEXT, inserted_at TEXT, updated_at TEXT, consolidated_at TEXT,
  superseded_by INTEGER REFERENCES observations(id),
  archived_at TEXT,
  embedding BLOB, embed_model TEXT
);
CREATE TABLE IF NOT EXISTS observation_mentions (
  observation_id INTEGER REFERENCES observations(id) ON DELETE CASCADE,
  entity_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  PRIMARY KEY(observation_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_obs_kind ON observations(kind, archived_at, importance DESC);
CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id);
CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
  content, content='observations', content_rowid='id', tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
  name, summary, content='entities', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations BEGIN
  INSERT INTO observations_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
  INSERT INTO observations_fts(observations_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN
  INSERT INTO observations_fts(observations_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
  INSERT INTO observations_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
  INSERT INTO entities_fts(rowid, name, summary) VALUES (new.id, new.name, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, name, summary)
    VALUES ('delete', old.id, old.name, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, name, summary)
    VALUES ('delete', old.id, old.name, old.summary);
  INSERT INTO entities_fts(rowid, name, summary) VALUES (new.id, new.name, new.summary);
END;
"""

_TABLES = ("meta", "entities", "entity_aliases", "edges", "observations",
           "observation_mentions", "observations_fts", "entities_fts")


def ensure_schema(conn) -> None:
    """Create everything if absent. Idempotent. Refuses a newer schema."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='graph_schema'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None and int(row[0]) > SCHEMA_VERSION:
        raise GraphNotInitialised(
            f"graph schema {row[0]} is newer than this code ({SCHEMA_VERSION})")
    conn.executescript(DDL)
    conn.execute(AUDIT_DDL)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('graph_schema', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


def check_schema(conn) -> None:
    """Read-only. Raises GraphNotInitialised if absent, partial or too new."""
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table')")}
        missing = [t for t in _TABLES if t not in have]
        if missing:
            raise GraphNotInitialised(f"graph not initialised (missing: {', '.join(missing)})")
        row = conn.execute("SELECT value FROM meta WHERE key='graph_schema'").fetchone()
    except sqlite3.DatabaseError as e:
        raise GraphNotInitialised(f"graph not readable: {e}") from e
    if row is None:
        raise GraphNotInitialised("graph not initialised (meta.graph_schema missing)")
    try:
        n = int(row[0])
    except (TypeError, ValueError):
        raise GraphNotInitialised("graph schema number unreadable")
    if n > SCHEMA_VERSION:
        raise GraphNotInitialised(
            f"graph schema {n} is newer than this code ({SCHEMA_VERSION})")
