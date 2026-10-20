"""Persona snapshots (#241): copy an agent's voice and prompt aside, put them back.

A snapshot is `data/persona-snapshots/<agent>/<id>/` holding `persona/` (a copy of
`agents/<agent>/persona/`), `SYSTEM_PROMPT.md` (when present) and `meta.json`
(`reason`, `ts`, `digest`). The newest KEEP are kept. Ids are UTC timestamps.

`restore` first snapshots the current state, so a restore can itself be undone.
Nothing here edits a protected path: persona and prompt files are the agent's own.
"""
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

KEEP = 10
ID_RE = re.compile(r"^\d{8}T\d{6}Z(-\d+)?$")
PROMPT = "SYSTEM_PROMPT.md"


def _agent_dir(ws, agent) -> Path:
    return Path(ws) / "agents" / agent


def _root(ws, agent) -> Path:
    return Path(ws) / "data" / "persona-snapshots" / agent


def _files(base: Path):
    """(relative name, path) for the prompt and every file under persona/."""
    out = []
    if (base / PROMPT).is_file():
        out.append((PROMPT, base / PROMPT))
    pdir = base / "persona"
    if pdir.is_dir():
        for p in sorted(pdir.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append(("persona/" + p.relative_to(pdir).as_posix(), p))
    return out


def digest(base: Path) -> str:
    h = hashlib.sha256()
    for name, p in _files(base):
        h.update(name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _new_id(root: Path) -> str:
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sid, n = base, 1
    while (root / sid).exists():
        n += 1
        sid = f"{base}-{n}"
    return sid


def list_snapshots(ws, agent) -> list:
    root = _root(ws, agent)
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and ID_RE.match(d.name):
                try:
                    meta = json.loads((d / "meta.json").read_text())
                except (OSError, ValueError):
                    meta = {}
                out.append({"id": d.name, "reason": meta.get("reason", ""), "digest": meta.get("digest")})
    return sorted(out, key=lambda m: m["id"])


def snapshot(ws, agent, reason="manual") -> dict:
    src = _agent_dir(ws, agent)
    if not src.is_dir():
        raise LookupError(f"no agent directory for {agent!r}")
    root = _root(ws, agent)
    root.mkdir(parents=True, exist_ok=True)
    sid = _new_id(root)
    dest = root / sid
    dest.mkdir()
    for name, p in _files(src):
        t = dest / name
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, t)
    (dest / "persona").mkdir(exist_ok=True)
    meta = {"reason": str(reason)[:200], "ts": datetime.now(timezone.utc).isoformat(),
            "digest": digest(src)}
    (dest / "meta.json").write_text(json.dumps(meta))
    pruned = []
    for old in list_snapshots(ws, agent)[:-KEEP]:
        shutil.rmtree(root / old["id"], ignore_errors=True)
        pruned.append(old["id"])
    return {"agent": agent, "snapshot_id": sid, "files": len(_files(src)), "pruned": pruned}


def snapshot_if_changed(ws, agent, reason) -> dict | None:
    """Snapshot only when persona or prompt differ from the newest snapshot."""
    src = _agent_dir(ws, agent)
    if not src.is_dir() or not _files(src):
        return None
    snaps = list_snapshots(ws, agent)
    if snaps and snaps[-1].get("digest") == digest(src):
        return None
    return snapshot(ws, agent, reason)


def restore(ws, agent, snapshot_id) -> dict:
    if not isinstance(snapshot_id, str) or not ID_RE.match(snapshot_id):
        have = [s["id"] for s in list_snapshots(ws, agent)]
        raise ValueError(f"snapshot_id must look like 20260101T000000Z; available: {have}")
    snap = _root(ws, agent) / snapshot_id
    if not snap.is_dir():
        have = [s["id"] for s in list_snapshots(ws, agent)]
        raise LookupError(f"no snapshot {snapshot_id!r} for {agent!r}; available: {have}")
    before = snapshot(ws, agent, f"before restore of {snapshot_id}")
    dest = _agent_dir(ws, agent)
    prompt = dest / PROMPT
    if (snap / PROMPT).is_file():
        shutil.copy2(snap / PROMPT, prompt)
    elif prompt.exists():
        prompt.unlink()
    pdir = dest / "persona"
    if pdir.is_dir():
        shutil.rmtree(pdir)
    shutil.copytree(snap / "persona", pdir)
    return {"agent": agent, "restored": snapshot_id, "undo_snapshot": before["snapshot_id"],
            "note": "call reload_agent to load it"}
