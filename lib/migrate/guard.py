"""Schema stamp: read, write, and the refuse-to-start check.

    python3 lib/migrate/guard.py check [DATA_DIR]
    python3 lib/migrate/guard.py stamp --fresh [DATA_DIR]
"""
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
from paths import workspace_root  # noqa: E402
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # executed as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lib.migrate import (EXIT_GUARD, EXIT_USAGE, PACKAGE_VERSION,  # noqa: E402
                         SCHEMA_VERSION, STAMP_NAME)

MSG_OLDER = "schema {n} is older than this release needs; run: karakos migrate"
MSG_UNSTAMPED = "this data directory is from Karakos 1.x; run: karakos migrate"


def stamp_path(data_dir) -> Path:
    return Path(data_dir) / STAMP_NAME


def read_stamp(data_dir):
    """Return the stamp dict, or None when absent. Corrupt counts as absent
    (caller treats it as unstamped)."""
    try:
        d = json.loads(stamp_path(data_dir).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("schema"), int) else None


def write_stamp(data_dir, migrated_from=None) -> dict:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = {"schema": SCHEMA_VERSION, "package": PACKAGE_VERSION,
             "migrated_from": migrated_from,
             "stamped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    tmp = data_dir / (STAMP_NAME + ".tmp")
    tmp.write_text(json.dumps(stamp) + "\n")
    os.replace(tmp, stamp_path(data_dir))
    return stamp


def _non_empty(data_dir: Path) -> bool:
    return data_dir.is_dir() and any(data_dir.iterdir())


def check(data_dir):
    """Return (ok, message). Pure; no writes."""
    data_dir = Path(data_dir)
    stamp = read_stamp(data_dir)
    if stamp is not None:
        if stamp["schema"] >= SCHEMA_VERSION:
            return True, ""
        return False, MSG_OLDER.format(n=stamp["schema"])
    if _non_empty(data_dir):
        return False, MSG_UNSTAMPED
    return True, ""  # fresh install; setup stamps it


def require_stamp(data_dir) -> None:
    """Exit 78 unless data_dir is stamped current (or a fresh empty install)."""
    if os.environ.get("KARAKOS_SKIP_STAMP_CHECK") == "1":
        if os.environ.get("KARAKOS_ENV") == "production":
            print("KARAKOS_SKIP_STAMP_CHECK is refused when KARAKOS_ENV=production",
                  file=sys.stderr)
            sys.exit(EXIT_GUARD)
        return
    ok, msg = check(data_dir)
    if not ok:
        print(msg, file=sys.stderr)
        sys.exit(EXIT_GUARD)


def stamp_fresh(data_dir) -> bool:
    """Stamp an empty/absent data dir. Refuses (False) on a non-empty unstamped one."""
    data_dir = Path(data_dir)
    if read_stamp(data_dir) is not None:
        return True
    if _non_empty(data_dir):
        return False
    write_stamp(data_dir, migrated_from=None)
    return True


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    default = os.path.join(workspace_root(), "data")
    if argv[:1] == ["check"]:
        require_stamp(argv[1] if len(argv) > 1 else default)
        return 0
    if argv[:2] == ["stamp", "--fresh"]:
        if stamp_fresh(argv[2] if len(argv) > 2 else default):
            return 0
        print(MSG_UNSTAMPED, file=sys.stderr)
        return EXIT_GUARD
    print("usage: guard.py check [DATA_DIR] | stamp --fresh [DATA_DIR]", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
