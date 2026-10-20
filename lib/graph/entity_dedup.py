"""Entity dedup by embedding similarity (#238 build B).

Finds live entities of the same kind whose embeddings are nearly identical and
folds the weaker into the stronger with `GraphStore.merge_entities` (soft: the
merged-away entity is archived, its name and aliases keep resolving). Dry-run
unless `live=True`; a live run merges at most MAX_MERGES pairs.
"""
from __future__ import annotations

import numpy as np

from lib.graph import embed as _embed

THRESHOLD = 0.92
MAX_MERGES = 50
AGENT = "graph-dedup"


def candidates(store, threshold=THRESHOLD) -> list:
    """Pairs to merge, best score first; each entity appears in at most one pair."""
    with store.read() as conn:
        rows = conn.execute(
            "SELECT e.id, e.name, e.kind, e.embedding, e.created_at, "
            "(SELECT COUNT(*) FROM observations o WHERE o.entity_id=e.id "
            " AND o.archived_at IS NULL AND o.superseded_by IS NULL) AS obs "
            "FROM entities e WHERE e.archived_at IS NULL AND e.embedding IS NOT NULL "
            "AND e.embed_model=? ORDER BY e.id", (_embed.MODEL_NAME,)).fetchall()
    by_kind = {}
    for r in rows:
        v = _embed.decode(r["embedding"])
        n = float(np.linalg.norm(v)) if v is not None else 0.0
        if n > 0:
            by_kind.setdefault(r["kind"], []).append((r, v / n))
    scored = []
    for items in by_kind.values():
        if len(items) < 2:
            continue
        m = np.vstack([v for _, v in items])
        sims = m @ m.T
        ii, jj = np.nonzero(np.triu(sims >= threshold, k=1))
        for i, j in zip(ii, jj):
            scored.append((float(sims[i, j]), items[int(i)][0], items[int(j)][0]))
    scored.sort(key=lambda t: (-t[0], t[1]["id"], t[2]["id"]))
    used, out = set(), []
    for score, a, b in scored:
        if a["id"] in used or b["id"] in used:
            continue
        # keep the entity with more live observations; tie: the older one
        keep, drop = (a, b) if (a["obs"], -a["id"]) >= (b["obs"], -b["id"]) else (b, a)
        used.update((a["id"], b["id"]))
        out.append({"from": drop["id"], "from_name": drop["name"],
                    "into": keep["id"], "into_name": keep["name"],
                    "kind": a["kind"], "score": round(score, 4)})
    return out


def run(store, live=False, threshold=THRESHOLD, max_merges=MAX_MERGES) -> dict:
    cap = max(0, min(int(max_merges), MAX_MERGES))
    found = candidates(store, threshold)
    batch = found[:cap]
    out = {"live": bool(live), "threshold": threshold, "candidates": len(found),
           "capped": len(found) > cap, "pairs": batch, "merged": 0}
    if live:
        for p in batch:
            store.merge_entities(p["from"], p["into"],
                                 reason=f"graph-dedup cosine {p['score']}", agent=AGENT)
            out["merged"] += 1
    return out
