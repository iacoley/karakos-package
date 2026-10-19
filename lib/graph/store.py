"""GraphStore: one SQLite file, short-lived connection per call group.

Writes use BEGIN IMMEDIATE so the tool server, the hook and the monitor job
can write at once. open_graph(create=False) never writes a schema.
"""
import json
import re
import sqlite3
import unicodedata
from urllib.parse import quote
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from lib.graph import embed as _embed_mod
from lib.graph.schema import AUDIT_DDL, GraphNotInitialised, check_schema, ensure_schema

DB_REL = Path("memory") / "graph.db"
EDGE_STEP = 0.1
EDGE_CAP = 5.0
WRITE_EMBED_BUDGET_S = 5.0
_WS = re.compile(r"\s+")


def norm(text) -> str:
    """NFKC, casefolded, whitespace collapsed."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", str(text or "")).casefold()).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GraphStore:
    def __init__(self, path):
        self.path = Path(path)

    # -- connections -------------------------------------------------------
    def connect(self, create=False):
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.exists():
            raise GraphNotInitialised(f"graph database not found: {self.path}")
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read_ro(self):
        """Read-only connection (mode=ro, query_only). Never creates the file."""
        if not self.path.exists():
            raise GraphNotInitialised(f"graph database not found: {self.path}")
        conn = sqlite3.connect(f"file:{quote(self.path.as_posix())}?mode=ro", uri=True,
                               timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA query_only=ON")
            check_schema(conn)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def check(self):
        with self.read() as conn:
            check_schema(conn)

    # -- entities ----------------------------------------------------------
    def _find_entity(self, conn, ref, kind=None):
        if isinstance(ref, int):
            return conn.execute("SELECT * FROM entities WHERE id=?", (ref,)).fetchone()
        n = norm(ref)
        if kind is not None:
            row = conn.execute("SELECT * FROM entities WHERE name_norm=? AND kind=? "
                               "AND archived_at IS NULL", (n, kind)).fetchone()
            if row:
                return row
            return conn.execute(
                "SELECT e.* FROM entities e JOIN entity_aliases a ON a.entity_id=e.id "
                "WHERE a.alias_norm=? AND e.kind=? AND e.archived_at IS NULL "
                "ORDER BY e.id LIMIT 1", (n, kind)).fetchone()
        row = conn.execute("SELECT * FROM entities WHERE name_norm=? AND archived_at IS NULL "
                           "ORDER BY id LIMIT 1", (n,)).fetchone()
        if row:
            return row
        return conn.execute(
            "SELECT e.* FROM entities e JOIN entity_aliases a ON a.entity_id=e.id "
            "WHERE a.alias_norm=? AND e.archived_at IS NULL ORDER BY e.id LIMIT 1",
            (n,)).fetchone()

    def _entity(self, conn, name, kind, summary=None, aliases=()):
        ts = now_iso()
        row = self._find_entity(conn, name, kind)
        if row is None:
            gone = conn.execute("SELECT id FROM entities WHERE name_norm=? AND kind=? "
                                "AND archived_at IS NOT NULL", (norm(name), kind)).fetchone()
            if gone:  # re-adding a soft-deleted entity brings it back
                conn.execute("UPDATE entities SET archived_at=NULL, summary=?, updated_at=?, "
                             "last_seen_at=? WHERE id=?", (summary, ts, ts, gone["id"]))
                row = conn.execute("SELECT * FROM entities WHERE id=?",
                                   (gone["id"],)).fetchone()
        created = row is None
        if created:
            cur = conn.execute(
                "INSERT INTO entities(name, name_norm, kind, summary, created_at, "
                "updated_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
                (str(name).strip(), norm(name), kind, summary, ts, ts, ts))
            eid = cur.lastrowid
        else:
            eid = row["id"]
            if summary and not row["summary"]:
                conn.execute("UPDATE entities SET summary=?, updated_at=? WHERE id=?",
                             (summary, ts, eid))
            conn.execute("UPDATE entities SET last_seen_at=? WHERE id=?", (ts, eid))
        for a in aliases or ():
            an = norm(a)
            if an and an != norm(name):
                conn.execute("INSERT OR IGNORE INTO entity_aliases(entity_id, alias_norm) "
                             "VALUES (?,?)", (eid, an))
        return eid, created

    def add_entity(self, name, kind="thing", summary=None, aliases=()):
        if not norm(name):
            raise ValueError("entity name is empty")
        with self.write() as conn:
            return self._entity(conn, name, kind, summary, aliases)

    def _resolve_or_create(self, conn, ref, create=True):
        if isinstance(ref, int):
            row = self._find_entity(conn, ref)
            if row is None or row["archived_at"]:
                raise KeyError(f"no entity with id {ref}")
            return row["id"]
        row = self._find_entity(conn, ref)
        if row is not None:
            return row["id"]
        if not create:
            raise KeyError(f"no entity named {ref!r}")
        return self._entity(conn, ref, "thing")[0]

    def get_entity(self, ref):
        with self.read() as conn:
            row = self._find_entity(conn, ref)
            if row is None:
                return None
            d = {k: row[k] for k in row.keys() if k != "embedding"}
            d["aliases"] = [r[0] for r in conn.execute(
                "SELECT alias_norm FROM entity_aliases WHERE entity_id=? ORDER BY alias_norm",
                (row["id"],))]
            return d

    # -- edges -------------------------------------------------------------
    def add_edge(self, src, dst, relation, weight=None, create_missing=True):
        if not str(relation or "").strip():
            raise ValueError("relation is empty")
        ts = now_iso()
        with self.write() as conn:
            s = self._resolve_or_create(conn, src, create_missing)
            d = self._resolve_or_create(conn, dst, create_missing)
            if s == d:
                raise ValueError("self-edges are not allowed")
            row = conn.execute(
                "SELECT id, weight FROM edges WHERE src_id=? AND dst_id=? AND relation=?",
                (s, d, relation)).fetchone()
            if row:
                w = min(EDGE_CAP, (row["weight"] or 1.0) + EDGE_STEP)
                conn.execute("UPDATE edges SET weight=?, updated_at=? WHERE id=?",
                             (w, ts, row["id"]))
                return row["id"], False, w
            w = 1.0 if weight is None else min(EDGE_CAP, float(weight))
            cur = conn.execute(
                "INSERT INTO edges(src_id, dst_id, relation, weight, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)", (s, d, relation, w, ts, ts))
            return cur.lastrowid, True, w

    def neighbors(self, entity_id, limit=20):
        with self.read() as conn:
            rows = conn.execute(
                "SELECT e.id, e.name, e.kind, ed.relation, ed.weight, 'out' AS direction "
                "FROM edges ed JOIN entities e ON e.id=ed.dst_id WHERE ed.src_id=? "
                "UNION ALL "
                "SELECT e.id, e.name, e.kind, ed.relation, ed.weight, 'in' "
                "FROM edges ed JOIN entities e ON e.id=ed.src_id WHERE ed.dst_id=? "
                "ORDER BY weight DESC LIMIT ?", (entity_id, entity_id, int(limit))).fetchall()
            return [dict(r) for r in rows]

    # -- observations ------------------------------------------------------
    def add_observation(self, content, kind="fact", entity=None, importance=None,
                        confidence=None, domain=None, agent=None, tags=None,
                        source="write", created_at=None, embed=True):
        content = str(content or "").strip()
        if not content:
            raise ValueError("content is empty")
        blob = None
        if embed:
            blob = _embed_one(content)
        ts = now_iso()
        if isinstance(tags, (list, tuple)):
            tags = json.dumps(list(tags))
        imp = 5.0 if importance is None else float(importance)
        with self.write() as conn:
            eid = None
            if entity is not None:
                eid = self._resolve_or_create(conn, entity, True)
            cn = norm(content)
            for r in conn.execute(
                    "SELECT id, content FROM observations WHERE kind=? AND entity_id IS ? "
                    "AND archived_at IS NULL", (kind, eid)):
                if norm(r["content"]) == cn:
                    conn.execute("UPDATE observations SET updated_at=? WHERE id=?",
                                 (ts, r["id"]))
                    return r["id"], False
            cur = conn.execute(
                "INSERT INTO observations(kind, content, entity_id, importance, "
                "base_importance, confidence, domain, agent, tags, source, created_at, "
                "inserted_at, updated_at, embedding, embed_model) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, content, eid, imp, imp, confidence, domain, agent, tags, source,
                 created_at or ts, ts, ts, blob, _embed_mod.MODEL_NAME if blob else None))
            return cur.lastrowid, True

    # -- hygiene: soft delete, supersede, merge ------------------------------
    # Agent-facing deletes never remove an observation or entity row: they set
    # archived_at / superseded_by and write a memory_audit row. Edges have no
    # archive column, so a deleted edge is removed and its row kept in the
    # audit detail. Hard delete is operator-only.
    def _audit(self, conn, agent, action, target_type, target_id, reason=None, detail=None):
        conn.execute(AUDIT_DDL)
        conn.execute(
            "INSERT INTO memory_audit(at, agent, action, target_type, target_id, reason, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (now_iso(), agent, action, target_type, target_id, reason,
             json.dumps(detail) if detail is not None else None))

    def forget_observation(self, obs_id, reason=None, agent=None):
        with self.write() as conn:
            row = conn.execute("SELECT id, archived_at FROM observations WHERE id=?",
                               (obs_id,)).fetchone()
            if row is None:
                raise KeyError(f"no observation with id {obs_id}")
            if row["archived_at"]:
                raise ValueError(f"observation {obs_id} is already forgotten")
            ts = now_iso()
            conn.execute("UPDATE observations SET archived_at=?, updated_at=? WHERE id=?",
                         (ts, ts, obs_id))
            self._audit(conn, agent, "forget", "observation", obs_id, reason)
        return obs_id

    def supersede_observation(self, obs_id, text, agent=None, embed=True):
        text = str(text or "").strip()
        if not text:
            raise ValueError("text is empty")
        blob = _embed_one(text) if embed else None
        with self.write() as conn:
            old = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
            if old is None:
                raise KeyError(f"no observation with id {obs_id}")
            if old["archived_at"] or old["superseded_by"]:
                raise ValueError(f"observation {obs_id} is already forgotten or superseded")
            ts = now_iso()
            cur = conn.execute(
                "INSERT INTO observations(kind, subkind, content, entity_id, importance, "
                "base_importance, confidence, domain, agent, tags, source, created_at, "
                "inserted_at, updated_at, embedding, embed_model) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'write',?,?,?,?,?)",
                (old["kind"], old["subkind"], text, old["entity_id"], old["importance"],
                 old["base_importance"], old["confidence"], old["domain"],
                 agent or old["agent"], old["tags"], ts, ts, ts, blob,
                 _embed_mod.MODEL_NAME if blob else None))
            new_id = cur.lastrowid
            conn.execute("UPDATE observations SET superseded_by=?, updated_at=? WHERE id=?",
                         (new_id, ts, obs_id))
            self._audit(conn, agent, "supersede", "observation", obs_id,
                        detail={"new_id": new_id, "old_content": old["content"]})
        return new_id

    def delete_entity(self, ref, reason=None, agent=None):
        with self.write() as conn:
            row = self._find_entity(conn, ref)
            if row is None or row["archived_at"]:
                raise KeyError(f"no entity {ref!r}")
            eid = row["id"]
            edges = [dict(r) for r in conn.execute(
                "SELECT id, src_id, dst_id, relation, weight FROM edges "
                "WHERE src_id=? OR dst_id=?", (eid, eid))]
            conn.execute("DELETE FROM edges WHERE src_id=? OR dst_id=?", (eid, eid))
            ts = now_iso()
            conn.execute("UPDATE entities SET archived_at=?, updated_at=? WHERE id=?",
                         (ts, ts, eid))
            self._audit(conn, agent, "delete_entity", "entity", eid, reason,
                        {"name": row["name"], "edges_removed": edges})
        return eid, len(edges)

    def delete_edge(self, src, dst, relation, reason=None, agent=None):
        with self.write() as conn:
            s = self._resolve_or_create(conn, src, create=False)
            d = self._resolve_or_create(conn, dst, create=False)
            row = conn.execute(
                "SELECT id, src_id, dst_id, relation, weight FROM edges "
                "WHERE src_id=? AND dst_id=? AND relation=?", (s, d, relation)).fetchone()
            if row is None:
                raise KeyError("no such edge")
            conn.execute("DELETE FROM edges WHERE id=?", (row["id"],))
            self._audit(conn, agent, "delete_edge", "edge", row["id"], reason, dict(row))
        return row["id"]

    def merge_entities(self, src, dst, reason=None, agent=None):
        """Fold entity `src` into `dst`. `src` is archived, never removed."""
        with self.write() as conn:
            s = self._resolve_or_create(conn, src, create=False)
            d = self._resolve_or_create(conn, dst, create=False)
            if s == d:
                raise ValueError("cannot merge an entity into itself")
            sr = conn.execute("SELECT * FROM entities WHERE id=?", (s,)).fetchone()
            moved = {"observations": 0, "edges": 0, "edges_dropped": 0}
            moved["observations"] = conn.execute(
                "UPDATE observations SET entity_id=? WHERE entity_id=?", (d, s)).rowcount
            conn.execute("INSERT OR IGNORE INTO observation_mentions(observation_id, entity_id)"
                         " SELECT observation_id, ? FROM observation_mentions WHERE entity_id=?",
                         (d, s))
            conn.execute("DELETE FROM observation_mentions WHERE entity_id=?", (s,))
            # The old name and aliases keep resolving, now to dst.
            conn.execute("INSERT OR IGNORE INTO entity_aliases(entity_id, alias_norm) "
                         "SELECT ?, alias_norm FROM entity_aliases WHERE entity_id=?", (d, s))
            dn = conn.execute("SELECT name_norm FROM entities WHERE id=?", (d,)).fetchone()[0]
            if sr["name_norm"] != dn:
                conn.execute("INSERT OR IGNORE INTO entity_aliases(entity_id, alias_norm) "
                             "VALUES (?,?)", (d, sr["name_norm"]))
            conn.execute("DELETE FROM entity_aliases WHERE entity_id=?", (s,))
            for e in conn.execute("SELECT * FROM edges WHERE src_id=? OR dst_id=?",
                                  (s, s)).fetchall():
                ns = d if e["src_id"] == s else e["src_id"]
                nd = d if e["dst_id"] == s else e["dst_id"]
                dup = conn.execute("SELECT id FROM edges WHERE src_id=? AND dst_id=? "
                                   "AND relation=?", (ns, nd, e["relation"])).fetchone()
                if ns == nd or dup:
                    conn.execute("DELETE FROM edges WHERE id=?", (e["id"],))
                    moved["edges_dropped"] += 1
                else:
                    conn.execute("UPDATE edges SET src_id=?, dst_id=? WHERE id=?",
                                 (ns, nd, e["id"]))
                    moved["edges"] += 1
            ts = now_iso()
            # Free the (name_norm, kind) slot so the name can be reused later.
            conn.execute("UPDATE entities SET archived_at=?, updated_at=?, "
                         "name_norm=name_norm || ? WHERE id=?", (ts, ts, f"#merged-{s}", s))
            conn.execute("UPDATE entities SET updated_at=? WHERE id=?", (ts, d))
            self._audit(conn, agent, "merge_entities", "entity", s, reason,
                        {"into": d, "name": sr["name"], **moved})
        return {"from": s, "into": d, **moved}

    # -- embeddings --------------------------------------------------------
    def iter_unembedded(self, limit=50):
        """[(table, id, text)] needing an embedding, observations first."""
        with self.read() as conn:
            out = [("observations", r["id"], r["content"]) for r in conn.execute(
                "SELECT id, content FROM observations WHERE embedding IS NULL "
                "AND archived_at IS NULL ORDER BY id LIMIT ?", (int(limit),))]
            room = int(limit) - len(out)
            if room > 0:
                out += [("entities", r["id"], (r["name"] + ". " + (r["summary"] or "")).strip())
                        for r in conn.execute(
                            "SELECT id, name, summary FROM entities WHERE embedding IS NULL "
                            "AND archived_at IS NULL ORDER BY id LIMIT ?", (room,))]
            return out

    def set_embedding(self, table, row_id, blob):
        if table not in ("observations", "entities"):
            raise ValueError(f"bad table {table!r}")
        with self.write() as conn:
            conn.execute(f"UPDATE {table} SET embedding=?, embed_model=? WHERE id=?",
                         (blob, _embed_mod.MODEL_NAME if blob else None, row_id))

    # -- status / recall ---------------------------------------------------
    def status(self) -> dict:
        with self.read() as conn:
            check_schema(conn)
            one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
            meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
            return {
                "schema": int(meta.get("graph_schema", 0)),
                "entities": one("SELECT COUNT(*) FROM entities"),
                "edges": one("SELECT COUNT(*) FROM edges"),
                "observations": one("SELECT COUNT(*) FROM observations"),
                "by_kind": {r["kind"]: r["n"] for r in conn.execute(
                    "SELECT kind, COUNT(*) n FROM observations GROUP BY kind")},
                "unembedded": one("SELECT COUNT(*) FROM observations "
                                  "WHERE embedding IS NULL AND archived_at IS NULL"),
                "embed_model": meta.get("embed_model"),
                "embedder_available": _embed_mod.embedder_available(),
                "meta": meta,
            }

    def recall(self, query, **kw):
        from lib.graph.recall import recall
        return recall(self, query, **kw)


def _embed_one(text):
    vecs = _embed_mod.embed_texts([text], budget_s=WRITE_EMBED_BUDGET_S)
    return vecs[0] if vecs else None


def graph_path(data_dir) -> Path:
    return Path(data_dir) / DB_REL


def open_graph(data_dir, create=False) -> GraphStore:
    store = GraphStore(graph_path(data_dir))
    if create:
        conn = store.connect(create=True)
        try:
            ensure_schema(conn)
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('embed_model', ?)",
                         (_embed_mod.MODEL_NAME,))
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('embed_dim', ?)",
                         (str(_embed_mod.DIM),))
        finally:
            conn.close()
    else:
        store.check()
    return store
