"""Entity dedup by embedding similarity (#238 build B).

Finds live entities of the same kind whose embeddings are nearly identical and
folds the weaker into the stronger with `GraphStore.merge_entities` (soft: the
merged-away entity is archived, its name and aliases keep resolving). Dry-run
unless `live=True`; a live run merges at most MAX_MERGES pairs.
"""
from __future__ import annotations

import difflib
import re

import numpy as np

from lib.graph import embed as _embed

THRESHOLD = 0.92
ABBREV_FLOOR = 0.6   # cosine floor for a pair whose names are abbreviations of each other
LEX_RATIO = 0.6      # minimum name similarity (difflib) for a pair to count as lexically related
CHUNK = 512          # rows scored per matrix block: memory is CHUNK x n, not n x n
MAX_MERGES = 50
AGENT = "graph-dedup"

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(name) -> list:
    return _TOKEN.findall(str(name).lower())


def _digits(toks) -> list:
    return sorted(t for t in toks if any(c.isdigit() for c in t))


def _prefixes(short, long_) -> bool:
    if not short or len(short) > len(long_):
        return False
    it = iter(long_)
    return all(any(t.startswith(s_) for t in it) for s_ in short)


def _abbrev(a, b) -> bool:
    """Every token of one name is a prefix of a token of the other, in order
    ("ian c." against "Ian Coley")."""
    return _prefixes(a, b) or _prefixes(b, a)


def same_thing(name_a, name_b, cosine, threshold=THRESHOLD) -> bool:
    """The second signal. Embeddings of bare names score unrelated things high
    (two dates, two version numbers), so a merge also needs the names to agree:
    identical number tokens, and either a lexical resemblance at the usual
    threshold or an abbreviation relation at a lower one."""
    ta, tb = _tokens(name_a), _tokens(name_b)
    if not ta or not tb or _digits(ta) != _digits(tb):
        return False
    if cosine >= threshold:
        ratio = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
        if ratio >= LEX_RATIO or set(ta) & set(tb):
            return True
    return cosine >= ABBREV_FLOOR and _abbrev(ta, tb)


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
        floor = min(threshold, ABBREV_FLOOR)
        for i0 in range(0, len(items), CHUNK):
            sims = m[i0:i0 + CHUNK] @ m.T
            ii, jj = np.nonzero(sims >= floor)
            for i, j in zip(ii, jj):
                i += i0
                if j <= i:
                    continue
                score = float(sims[i - i0, j])
                a_, b_ = items[int(i)][0], items[int(j)][0]
                if same_thing(a_["name"], b_["name"], score, threshold):
                    scored.append((score, a_, b_))
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
