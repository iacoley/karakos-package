"""Logic behind the `memory` and `graph` MCP tools (ANDURIL 4.2).

mcp/tools-server.py stays a thin dispatcher; everything testable lives here.
Both entry points take (args, store, agent) and return a plain dict; failures
are {"error": ...}, never exceptions.
"""
import json
import re
import sys

from lib.graph import embed as _embed
from lib.graph.schema import GraphNotInitialised

NOT_INITIALISED = ("memory graph is not initialised; fresh install: run python3 -m lib.graph init; "
                  "upgrading 1.x data: run karakos migrate")

CONTENT_MAX = 4000
WRITE_EMBED_BUDGET_S = 10.0
RECALL_MAX = 50
EDGES_PER_ENTITY = 5
ENTITIES_MAX = 5
KINDS = ("fact", "episode", "pattern")
DEFAULT_IMPORTANCE = {"fact": 8.0, "episode": 5.0, "pattern": 6.0}
RECALL_MODES = ("auto", "keyword", "recent")
_RELATION_OK = re.compile(r"^[a-z0-9_]+$")


class _Bad(Exception):
    """Validation failure; message goes back to the caller as the error."""


def _num(args, key, default, lo, hi, clamp=True):
    v = args.get(key, default)
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _Bad(f"'{key}' must be a number")
    v = float(v)
    if clamp:
        return max(lo, min(hi, v))
    if not lo <= v <= hi:
        raise _Bad(f"'{key}' must be between {lo:g} and {hi:g}")
    return v


def _str_list(args, key):
    v = args.get(key)
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
        raise _Bad(f"'{key}' must be an array of strings")
    return [x.strip() for x in v if x.strip()]


def _ref(v, field):
    """Entity reference: numeric id (int or digit string) or a non-empty name."""
    if isinstance(v, bool):
        raise _Bad(f"'{field}' must be an entity name or id")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            raise _Bad(f"'{field}' is required")
        return int(s) if s.isdigit() else s
    raise _Bad(f"'{field}' must be an entity name or id")


# --------------------------------------------------------------------------
# memory
# --------------------------------------------------------------------------

def memory_tool(args, store, agent) -> dict:
    action = args.get("action", "recent")
    try:
        if action == "write":
            return _write(args, store, agent)
        if action == "recall":
            return _recall(args, store)
        if action == "status":
            return _status(store)
        if action == "forget":
            return _forget(args, store, agent)
        if action == "supersede":
            return _supersede(args, store, agent)
        if action == "remember":
            return _remember(args, store, agent)
        if action == "facts":
            return _facts(args, store)
        if action == "recent":
            return _recent(args, store)
        return {"error": f"Unknown tool or action: memory.{action}"}
    except _Bad as e:
        return {"error": str(e)}
    except GraphNotInitialised:
        return {"error": NOT_INITIALISED}


def _write(args, store, agent, label="write") -> dict:
    content = args.get("content")
    if content is not None and not isinstance(content, str):
        raise _Bad("'content' must be a string")
    content = (content or "").strip()
    if not content:
        raise _Bad(f"{label} requires non-empty 'content'")
    if len(content) > CONTENT_MAX:
        raise _Bad(f"'content' is too long ({len(content)} > {CONTENT_MAX} chars)")
    kind = args.get("kind") or "fact"
    if kind not in KINDS:
        raise _Bad(f"'kind' must be one of: {list(KINDS)}")
    subkind = args.get("subkind")
    if subkind is not None and not isinstance(subkind, str):
        raise _Bad("'subkind' must be a string")
    subkind = (subkind or "").strip().lower()[:32] or None
    if kind == "pattern" and not subkind:
        raise _Bad("kind 'pattern' requires 'subkind'")
    subject = args.get("subject")
    if subject is not None and not isinstance(subject, str):
        raise _Bad("'subject' must be a string")
    subject = (subject or "").strip()[:200] or None
    importance = _num(args, "importance", DEFAULT_IMPORTANCE[kind], 1, 10)
    confidence = _num(args, "confidence", 0.8, 0, 1)
    domain = args.get("domain")
    if domain is not None and not isinstance(domain, str):
        raise _Bad("'domain' must be a string")
    domain = (domain or "").strip() or "general"
    tags = _str_list(args, "tags")

    entity_id = None
    if subject:
        with store.read() as conn:
            row = store._find_entity(conn, subject)
        entity_id = row["id"] if row else store.add_entity(subject, "topic")[0]

    # Insert without embedding, then embed with the tool's own budget so a
    # slow model load never holds the write transaction open.
    obs_id, created = store.add_observation(
        content, kind=kind, entity=entity_id, importance=importance,
        confidence=confidence, domain=domain, agent=agent or None,
        tags=tags or None, source="write", embed=False)
    embedded = None
    if created:
        if subkind:
            with store.write() as conn:
                conn.execute("UPDATE observations SET subkind=? WHERE id=?", (subkind, obs_id))
        vecs = _embed.embed_texts([content], budget_s=WRITE_EMBED_BUDGET_S)
        embedded = bool(vecs)
        if embedded:
            store.set_embedding("observations", obs_id, vecs[0])
    else:
        with store.read() as conn:
            r = conn.execute("SELECT embedding FROM observations WHERE id=?",
                             (obs_id,)).fetchone()
        embedded = bool(r and r["embedding"])
    out = {"status": "ok", "id": obs_id, "entity_id": entity_id, "created": created,
           "embedded": embedded}
    return out


def _obs_id(args):
    v = args.get("id")
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if isinstance(v, bool) or not isinstance(v, int):
        raise _Bad("'id' must be an observation id (from memory.recall)")
    return v


def _reason(args):
    r = args.get("reason")
    if r is not None and not isinstance(r, str):
        raise _Bad("'reason' must be a string")
    return (r or "").strip()[:500] or None


def _forget(args, store, agent) -> dict:
    oid = _obs_id(args)
    try:
        store.forget_observation(oid, _reason(args), agent or None)
    except (KeyError, ValueError) as e:
        raise _Bad(str(e.args[0]))
    return {"status": "ok", "id": oid, "forgotten": True}


def _supersede(args, store, agent) -> dict:
    oid = _obs_id(args)
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _Bad("supersede requires non-empty 'text'")
    if len(text.strip()) > CONTENT_MAX:
        raise _Bad(f"'text' is too long ({len(text.strip())} > {CONTENT_MAX} chars)")
    try:
        new_id = store.supersede_observation(oid, text, agent or None)
    except (KeyError, ValueError) as e:
        raise _Bad(str(e.args[0]))
    return {"status": "ok", "id": new_id, "superseded": oid}


def _entity_names(store, ids):
    ids = sorted({i for i in ids if i is not None})
    if not ids:
        return {}
    with store.read() as conn:
        rows = conn.execute(
            f"SELECT id, name FROM entities WHERE id IN ({','.join('?' * len(ids))})",
            ids).fetchall()
    return {r["id"]: r["name"] for r in rows}


def _raw_recall(args, store, limit, kinds, mode):
    query = args.get("query")
    if query is not None and not isinstance(query, str):
        raise _Bad("'query' must be a string")
    query = (query or "").strip()
    if mode != "recent" and not query:
        raise _Bad("recall requires a non-empty 'query' (unless mode is 'recent')")
    entity = args.get("entity")
    if entity is not None:
        entity = _ref(entity, "entity")
    domain = args.get("domain")
    if domain is not None and not isinstance(domain, str):
        raise _Bad("'domain' must be a string")
    return store.recall(query, limit=limit, kinds=kinds or None, entity=entity,
                        domain=(domain or None), mode=mode)


def _limit(args):
    try:
        n = int(args.get("limit", 10))
    except (TypeError, ValueError):
        raise _Bad("'limit' must be an integer")
    return max(1, min(n, RECALL_MAX))


def _recall(args, store) -> dict:
    mode = args.get("mode") or "auto"
    if mode not in RECALL_MODES:
        raise _Bad(f"'mode' must be one of: {list(RECALL_MODES)}")
    kinds = _str_list(args, "kinds")
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise _Bad(f"'kinds' entries must be from: {list(KINDS)}")
    raw = _raw_recall(args, store, _limit(args), kinds, mode)

    names = _entity_names(store, [r["entity_id"] for r in raw["results"]])
    results = [{"type": "observation", "id": r["id"], "kind": r["kind"],
                "text": r["content"], "entity": names.get(r["entity_id"]),
                "importance": r["importance"], "score": r["score"],
                "signals": r["signals"], "created_at": r["created_at"]}
               for r in raw["results"]]

    ent_rows = list(raw.get("entities", []))
    seen = {e["id"] for e in ent_rows}
    for r in raw["results"]:
        eid = r["entity_id"]
        if eid is not None and eid not in seen and len(ent_rows) < ENTITIES_MAX:
            seen.add(eid)
            ent_rows.append({"id": eid, "name": names.get(eid), "kind": None})
    entities = []
    for e in ent_rows[:ENTITIES_MAX]:
        edges = [{"relation": n["relation"], "to": n["name"], "weight": n["weight"]}
                 for n in store.neighbors(e["id"], limit=EDGES_PER_ENTITY)]
        entities.append({"id": e["id"], "name": e["name"], "kind": e.get("kind"),
                         "edges": edges})
    _touch(store, [e["id"] for e in entities])

    out = {"mode": raw["mode"], "results": results, "entities": entities}
    if "reason" in raw:
        out["reason"] = raw["reason"]
    return out


def _touch(store, ids):
    """Best effort: a recall must never fail because the bookkeeping did."""
    if not ids:
        return
    try:
        from lib.graph.store import now_iso
        with store.write() as conn:
            conn.execute(
                f"UPDATE entities SET last_seen_at=? WHERE id IN ({','.join('?' * len(ids))})",
                [now_iso(), *ids])
    except Exception as e:  # noqa: BLE001
        print(f"[graph] last_seen_at touch failed ({e})", file=sys.stderr)


def _status(store) -> dict:
    with store.read() as conn:
        from lib.graph.schema import check_schema
        check_schema(conn)
        one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        by_kind = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM observations WHERE archived_at IS NULL "
            "GROUP BY kind")}
        counts = {k: by_kind.get(k, 0) for k in KINDS}
        counts["archived"] = one("SELECT COUNT(*) FROM observations "
                                 "WHERE archived_at IS NOT NULL")
        embedded = one("SELECT COUNT(*) FROM observations "
                       "WHERE embedding IS NOT NULL AND archived_at IS NULL")
        unembedded = one("SELECT COUNT(*) FROM observations "
                         "WHERE embedding IS NULL AND archived_at IS NULL")
        entities = one("SELECT COUNT(*) FROM entities WHERE archived_at IS NULL")
        edges = one("SELECT COUNT(*) FROM edges")

    def js(key):
        try:
            return json.loads(meta[key]) if meta.get(key) else None
        except (TypeError, ValueError):
            return None

    mig = js("migration")
    try:
        db_bytes = store.path.stat().st_size
    except OSError:
        db_bytes = None
    try:
        dim = int(meta.get("embed_dim") or _embed.DIM)
    except ValueError:
        dim = _embed.DIM
    return {"observations": counts, "entities": entities, "edges": edges,
            "embedded": embedded, "unembedded": unembedded,
            "embed_model": meta.get("embed_model"), "embed_dim": dim,
            "embedder_available": _embed.embedder_available(probe=False),
            "db_bytes": db_bytes, "schema": int(meta.get("graph_schema", 0)),
            "last_consolidation": js("last_consolidation"),
            "migrated_from": (mig or {}).get("package_from") if isinstance(mig, dict) else None}


# -- deprecated 1.x aliases (removed in 2.1) -------------------------------

def _remember(args, store, agent) -> dict:
    subject = args.get("subject")
    if subject is not None and not isinstance(subject, str):
        raise _Bad("'subject' must be a string")
    if not (subject or "").strip():
        raise _Bad("remember requires a non-empty 'subject'")
    if args.get("content") is not None and not isinstance(args.get("content"), str):
        raise _Bad("'content' must be a string")
    if not (args.get("content") or "").strip():
        raise _Bad("remember requires non-empty 'content'")
    w = _write({**args, "kind": "fact"}, store, agent, label="remember")
    if "error" in w:
        return w
    with store.read() as conn:
        r = conn.execute("SELECT content, confidence, domain FROM observations WHERE id=?",
                         (w["id"],)).fetchone()
    return {"status": "ok", "id": w["id"], "subject": subject.strip(),
            "content": r["content"], "confidence": r["confidence"], "domain": r["domain"],
            "deprecated": "use memory.write"}


def _facts(args, store) -> dict:
    raw = _raw_recall({**args, "query": args.get("query") or ""}, store, _limit(args),
                      ["fact"], "auto")
    ids = [r["id"] for r in raw["results"]]
    conf = {}
    if ids:
        with store.read() as conn:
            conf = {r["id"]: r["confidence"] for r in conn.execute(
                f"SELECT id, confidence FROM observations "
                f"WHERE id IN ({','.join('?' * len(ids))})", ids)}
    names = _entity_names(store, [r["entity_id"] for r in raw["results"]])
    return {"facts": [{"id": r["id"], "subject": names.get(r["entity_id"]),
                       "content": r["content"], "confidence": conf.get(r["id"]),
                       "domain": r["domain"]} for r in raw["results"]],
            "deprecated": "use memory.recall"}


def _recent(args, store) -> dict:
    raw = _raw_recall({k: v for k, v in args.items() if k != "query"}, store,
                      _limit(args), ["episode"], "recent")
    return {"episodes": [{"id": r["id"], "summary": r["content"],
                          "importance": r["importance"], "created_at": r["created_at"]}
                         for r in raw["results"]],
            "deprecated": "use memory.recall with mode=recent"}


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------

def graph_tool(args, store, agent) -> dict:
    action = args.get("action")
    try:
        if action == "add_entity":
            return _add_entity(args, store)
        if action == "add_edge":
            return _add_edge(args, store)
        if action == "delete_entity":
            return _delete_entity(args, store, agent)
        if action == "delete_edge":
            return _delete_edge(args, store, agent)
        if action == "merge_entities":
            return _merge_entities(args, store, agent)
        return {"error": f"Unknown tool or action: graph.{action}"}
    except _Bad as e:
        return {"error": str(e)}
    except GraphNotInitialised:
        return {"error": NOT_INITIALISED}


def _add_entity(args, store) -> dict:
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _Bad("add_entity requires a non-empty 'name'")
    name = name.strip()
    if len(name) > 200:
        raise _Bad("'name' is too long (max 200)")
    kind = args.get("kind")
    if kind is not None and not isinstance(kind, str):
        raise _Bad("'kind' must be a string")
    kind = (kind or "thing").strip().lower() or "thing"
    if len(kind) > 32:
        raise _Bad("'kind' is too long (max 32)")
    summary = args.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise _Bad("'summary' must be a string")
    summary = (summary or "").strip() or None
    if summary and len(summary) > 1000:
        raise _Bad("'summary' is too long (max 1000)")
    aliases = _str_list(args, "aliases")

    eid, created = store.add_entity(name, kind, summary, aliases)
    if not created and summary:
        # store keeps an existing summary; the tool replaces it when longer.
        with store.write() as conn:
            row = conn.execute("SELECT summary FROM entities WHERE id=?", (eid,)).fetchone()
            if len(summary) > len(row["summary"] or ""):
                from lib.graph.store import now_iso
                conn.execute("UPDATE entities SET summary=?, updated_at=? WHERE id=?",
                             (summary, now_iso(), eid))
    return {"id": eid, "created": created}


def _add_edge(args, store) -> dict:
    src = _ref(args.get("src"), "src")
    dst = _ref(args.get("dst"), "dst")
    rel = args.get("relation")
    if not isinstance(rel, str) or not rel.strip():
        raise _Bad("add_edge requires a non-empty 'relation'")
    rel = re.sub(r"\s+", "_", rel.strip().lower())
    if len(rel) > 64:
        raise _Bad("'relation' is too long (max 64)")
    if not _RELATION_OK.match(rel):
        raise _Bad("'relation' may only contain a-z, 0-9, spaces and underscores")
    weight = None
    if args.get("weight") is not None:
        weight = _num(args, "weight", None, 0, 5, clamp=False)
    cm = args.get("create_missing", True)
    if not isinstance(cm, bool):
        raise _Bad("'create_missing' must be a boolean")
    try:
        eid, created, w = store.add_edge(src, dst, rel, weight=weight, create_missing=cm)
    except KeyError as e:
        raise _Bad(str(e.args[0]) if e.args else "unknown entity")
    except ValueError as e:
        raise _Bad(str(e))
    return {"id": eid, "created": created, "weight": w}


def _delete_entity(args, store, agent) -> dict:
    ref = _ref(args.get("name", args.get("id")), "name")
    try:
        eid, edges = store.delete_entity(ref, _reason(args), agent or None)
    except KeyError as e:
        raise _Bad(str(e.args[0]))
    return {"status": "ok", "id": eid, "deleted": True, "edges_removed": edges}


def _delete_edge(args, store, agent) -> dict:
    src = _ref(args.get("src"), "src")
    dst = _ref(args.get("dst"), "dst")
    rel = args.get("relation")
    if not isinstance(rel, str) or not rel.strip():
        raise _Bad("delete_edge requires a non-empty 'relation'")
    rel = re.sub(r"\s+", "_", rel.strip().lower())
    try:
        eid = store.delete_edge(src, dst, rel, _reason(args), agent or None)
    except KeyError as e:
        raise _Bad(str(e.args[0]))
    return {"status": "ok", "id": eid, "deleted": True}


def _merge_entities(args, store, agent) -> dict:
    src = _ref(args.get("from"), "from")
    dst = _ref(args.get("into"), "into")
    try:
        out = store.merge_entities(src, dst, _reason(args), agent or None)
    except KeyError as e:
        raise _Bad(str(e.args[0]))
    except ValueError as e:
        raise _Bad(str(e))
    return {"status": "ok", **out}
