"""Consistent backup/restore of a Karakos install (data, config, agents, .env)."""
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_SKIP_SUFFIXES = ("-wal", "-shm", "-journal")
MANIFEST = "MANIFEST.json"


class BackupError(Exception):
    pass



def _copy(src, dst) -> None:
    """Copy contents, then metadata best effort. A container user that does not own an existing
    destination (a bind mount) may write it but not set its times or mode: copy2 raised EPERM
    from utime there and aborted the restore half way."""
    shutil.copyfile(src, dst)
    try:
        shutil.copystat(src, dst)
    except PermissionError:
        pass

def _overlay_git(label, root, p) -> bool:
    """The overlay's own .git is not backed up: its objects are read-only, so a restore onto
    the live overlay would fail, and its history lives on the overlay's remote."""
    return label == "overlay" and ".git" in p.relative_to(root).parts


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Online backup: consistent even with WAL and a live writer."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    from lib.migrate.detect import ro_uri
    s = sqlite3.connect(ro_uri(src), uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _sources(data_dir: Path, config_dir: Path):
    """(label, path) roots to capture. Labels are the top-level names in the backup."""
    roots = [("data", data_dir), ("config", config_dir),
             ("agents", config_dir.parent / "agents"),
             ("overlay", config_dir.parent / "overlay")]
    out = [(l, p) for l, p in roots if p.is_dir()]
    env = config_dir / ".env"
    if not env.is_file():
        env = config_dir.parent / ".env"
    return out, (env if env.is_file() else None)


def _copy_file(src: Path, dst: Path) -> None:
    if src.suffix in _DB_SUFFIXES:
        _sqlite_copy(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy(src, dst)


def backup(data_dir, config_dir, dest) -> Path:
    """Copy into <dest>/pre-2.0-<UTC ts>/ with a MANIFEST.json. Returns that dir.
    On any failure the partial backup is removed and BackupError is raised;
    the source tree is only ever read."""
    data_dir, config_dir, dest = Path(data_dir), Path(config_dir), Path(dest)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = dest / f"pre-2.0-{ts}"
    try:
        out.mkdir(parents=True)
        roots, env = _sources(data_dir, config_dir)
        files = []
        for label, root in roots:
            for p in sorted(root.rglob("*")):
                if p.is_file() and not p.name.endswith(_SKIP_SUFFIXES) \
                        and p.name != ".schema-version.tmp" \
                        and dest not in p.parents and out not in p.parents \
                        and not _overlay_git(label, root, p):
                    files.append((f"{label}/{p.relative_to(root).as_posix()}", p))
        if env:
            files.append((".env", env))
        dirs = []
        for label, root in roots:
            for p in sorted(root.rglob("*")):
                if p.is_dir() and dest not in p.parents and p != dest \
                        and out not in p.parents and p != out \
                        and not _overlay_git(label, root, p):
                    dirs.append(f"{label}/{p.relative_to(root).as_posix()}")
        entries = []
        for rel, src in files:
            target = out / "files" / rel
            _copy_file(src, target)
            entries.append({"path": rel, "source": str(src),
                            "size": target.stat().st_size, "sha256": _sha256(target)})
        roots_map = {l: str(p) for l, p in roots}
        if env:
            roots_map[".env"] = str(env)
        (out / MANIFEST).write_text(json.dumps(
            {"created": ts, "roots": roots_map, "files": entries, "dirs": dirs}, indent=1))
    except Exception as e:
        shutil.rmtree(out, ignore_errors=True)
        raise BackupError(f"backup failed: {e}") from e
    return out


def verify(backup_dir) -> dict:
    backup_dir = Path(backup_dir)
    try:
        m = json.loads((backup_dir / MANIFEST).read_text())
    except (OSError, ValueError) as e:
        raise BackupError(f"unreadable manifest in {backup_dir}: {e}") from e
    for e in m["files"]:
        f = backup_dir / "files" / e["path"]
        if not f.is_file() or _sha256(f) != e["sha256"]:
            raise BackupError(f"manifest mismatch: {e['path']}")
    return m


def restore(backup_dir, data_dir=None, config_dir=None) -> None:
    """Verify the manifest, then put every file back. Roots default to where
    they came from. Files are overwritten; stale -wal/-shm are removed so the
    restored DB is not replayed against old journal state; files under the
    restored roots that the manifest does not list are removed."""
    backup_dir = Path(backup_dir)
    m = verify(backup_dir)
    roots = {k: Path(v) for k, v in m["roots"].items()}
    if data_dir:
        roots["data"] = Path(data_dir)
    if config_dir:
        roots["config"] = Path(config_dir)
    kept = set()
    for d in m.get("dirs", []):
        label, _, rest = d.partition("/")
        if label in roots:
            (roots[label] / rest).mkdir(parents=True, exist_ok=True)
            kept.add(roots[label] / rest)
    for e in m["files"]:
        label, _, rest = e["path"].partition("/")
        dst = roots[".env"] if e["path"] == ".env" else roots[label] / rest
        kept.add(dst)
        if dst.suffix in _DB_SUFFIXES:
            for sfx in _SKIP_SUFFIXES:
                Path(str(dst) + sfx).unlink(missing_ok=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy(backup_dir / "files" / e["path"], dst)
    _prune(m, roots, kept, backup_dir)
    # a restore returns the data dir to its pre-migration state: no stamp
    (roots["data"] / ".schema-version").unlink(missing_ok=True)


def _prune(manifest, roots, kept, backup_dir: Path) -> None:
    """Remove files a failed or partial run created under the restored roots
    (agents.yaml, graph.db, *.pre-2.0 ...), so the tree equals the manifest.
    The backup directory and its parent (the backups root) are never touched."""
    protect = {backup_dir.resolve(), backup_dir.resolve().parent}
    for label in ("data", "config", "agents"):
        root = roots.get(label)
        if root is None or not root.is_dir():
            continue
        # A protected dir that contains this root (the backup mounted at /restore has parent /)
        # would shield every file under it; only dirs inside the root can need shielding.
        rr = root.resolve()
        guard = {q for q in protect if q != rr and q not in rr.parents}
        for p in sorted(root.rglob("*"), reverse=True):
            rp = p.resolve()
            if any(rp == q or q in rp.parents for q in guard):
                continue
            if p.is_file() or p.is_symlink():
                if p not in kept:
                    p.unlink()
            elif p.is_dir() and p not in kept and not any(p.iterdir()):
                p.rmdir()
