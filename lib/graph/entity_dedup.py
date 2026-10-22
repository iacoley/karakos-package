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


def _shortened(a, b) -> bool:
    """Same number of words; every word is equal or is a one- or two-letter
    initial of the other's word ("c" for "hartley"), at least one word differs and
    at least one word matches exactly ("ian c." against "Ian Hartley"). Longer
    prefixes ("git"/"github", "Cole"/"Hartley") are not abbreviations."""
    if len(a) != len(b) or a == b:
        return False

    def fits(x, y):
        s, l = sorted((x, y), key=len)
        return x == y or (len(s) <= 2 and l.startswith(s))

    return all(fits(x, y) for x, y in zip(a, b)) and any(x == y for x, y in zip(a, b))


def verdict(name_a, name_b, cosine, threshold=THRESHOLD):
    """The second signal. Embeddings of bare names score unrelated things high
    (two dates, two version numbers) and prefixes of longer names too ("Mike" and
    "Mike Tyson"), so a merge also needs the names to agree. Returns:

    - "merge": same number of words, identical number tokens, cosine at the
      threshold and a shared word or close spelling;
    - "abbrev": same number of words, identical number tokens, cosine at
      ABBREV_FLOOR and the words differ only by shortening;
    - "report": the names are related but one has fewer words (never merged);
    - None: not a candidate."""
    ta, tb = _tokens(name_a), _tokens(name_b)
    if not ta or not tb or _digits(ta) != _digits(tb) or cosine < min(threshold, ABBREV_FLOOR):
        return None
    if len(ta) != len(tb):
        related = set(ta) & set(tb) or _shortened(ta[:len(tb)], tb[:len(ta)])
        return "report" if related else None
    if cosine >= threshold:
        ratio = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
        if ratio >= LEX_RATIO or set(ta) & set(tb):
            return "merge"
    return "abbrev" if _shortened(ta, tb) else None


def _scan(store, threshold):
    """(merge pairs, report-only pairs), each best score first."""
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
    found = []  # (score, a, b, verdict)
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
                v = verdict(a_["name"], b_["name"], score, threshold)
                if v:
                    found.append((score, a_, b_, v))
    # an abbreviation that fits two entities ("ian c." -> Ian Hartley, Ian Cooper) is
    # a guess: report it, merge neither
    partners = {}
    for _, a, b, v in found:
        if v == "abbrev":
            partners.setdefault(a["id"], set()).add(b["id"])
            partners.setdefault(b["id"], set()).add(a["id"])
    ambiguous = {i for i, ps in partners.items() if len(ps) > 1}
    scored, reported = [], []
    for score, a, b, v in found:
        if v == "abbrev" and (a["id"] in ambiguous or b["id"] in ambiguous):
            v = "ambiguous"
        (scored if v in ("merge", "abbrev") else reported).append((score, a, b, v))
    key = lambda t: (-t[0], t[1]["id"], t[2]["id"])  # noqa: E731
    scored.sort(key=key)
    reported.sort(key=key)
    used, out = set(), []
    for score, a, b, _ in scored:
        if a["id"] in used or b["id"] in used:
            continue
        # keep the entity with more live observations; tie: the older one
        keep, drop = (a, b) if (a["obs"], -a["id"]) >= (b["obs"], -b["id"]) else (b, a)
        used.update((a["id"], b["id"]))
        out.append({"from": drop["id"], "from_name": drop["name"],
                    "into": keep["id"], "into_name": keep["name"],
                    "kind": a["kind"], "score": round(score, 4)})
    rep = [{"a": a["id"], "a_name": a["name"], "b": b["id"], "b_name": b["name"],
            "kind": a["kind"], "score": round(score, 4), "why": v}
           for score, a, b, v in reported]
    return out, rep


def candidates(store, threshold=THRESHOLD) -> list:
    """Pairs to merge, best score first; each entity appears in at most one pair."""
    return _scan(store, threshold)[0]


def report_only(store, threshold=THRESHOLD) -> list:
    """Related pairs that are listed but never merged: one name has fewer words
    ("why": "report"), or an abbreviation fits several entities ("ambiguous")."""
    return _scan(store, threshold)[1]


def run(store, live=False, threshold=THRESHOLD, max_merges=MAX_MERGES) -> dict:
    cap = max(0, min(int(max_merges), MAX_MERGES))
    found, rep = _scan(store, threshold)
    batch = found[:cap]
    out = {"live": bool(live), "threshold": threshold, "candidates": len(found),
           "capped": len(found) > cap, "pairs": batch, "merged": 0,
           "report_only": rep[:MAX_MERGES], "report_only_count": len(rep)}
    if live:
        for p in batch:
            store.merge_entities(p["from"], p["into"],
                                 reason=f"graph-dedup cosine {p['score']}", agent=AGENT)
            out["merged"] += 1
    return out
