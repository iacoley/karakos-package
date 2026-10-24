"""python3 -m lib.migrate [--dry-run|--auto|--force|--to-backup DIR|--backup-to DIR]"""
import argparse
import os
import sys
from pathlib import Path

from lib.migrate import EXIT_OK, EXIT_STEP_FAILED, EXIT_USAGE


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(message, file=sys.stderr)
        sys.exit(EXIT_USAGE)


def main(argv=None) -> int:
    p = _Parser(prog="karakos migrate")
    p.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    p.add_argument("--auto", action="store_true", help="non-interactive (Docker)")
    p.add_argument("--force", action="store_true", help="proceed on unknown schema")
    p.add_argument("--parity-queries", type=int, default=50, metavar="N",
                   help="memory recall-parity queries (default 50; 0 disables)")
    p.add_argument("--backup-to", metavar="DIR",
                   help="where to write the pre-migration backup (default <root>/backups)")
    p.add_argument("--import-from", metavar="DIR",
                   help="old checkout mounted read-only; copy its logs/inbox/data in (1.0 layout)")
    p.add_argument("--keep-bind", metavar="HOST_DIR",
                   help="keep the old host checkout bind-mounted for data/logs/inbox "
                        "(writes docker-compose.override.yml)")
    p.add_argument("--report-to", metavar="FILE",
                   help="with --dry-run: write the report here (outside the install)")
    p.add_argument("--to-backup", metavar="DIR", help="restore a backup directory")
    p.add_argument("--root", default=os.environ.get("WORKSPACE_ROOT") or os.environ.get("KARAKOS_HOME") or os.path.abspath(os.path.join(__file__, "..", "..", "..")),
                   help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    root = Path(a.root)
    data, config = root / "data", root / "config"

    if a.to_backup:
        from lib.migrate import backup
        try:
            backup.restore(a.to_backup, data_dir=data, config_dir=config)
        except backup.BackupError as e:
            print(e, file=sys.stderr)
            return EXIT_STEP_FAILED
        # A restore is the operator's explicit choice: the interrupted-run
        # marker must not later re-restore this (or an older) backup over
        # whatever the restored install writes from now on.
        from lib.migrate import runner
        runner.clear_marker(Path(a.backup_to) if a.backup_to else root / "backups")
        print(f"restored from {a.to_backup}")
        return EXIT_OK

    from lib.migrate import runner
    return runner.run(data, config, backup_root=a.backup_to or root / "backups",
                      dry_run=a.dry_run, force=a.force,
                      parity_queries=a.parity_queries, import_from=a.import_from,
                      keep_bind=a.keep_bind, report_to=a.report_to)


if __name__ == "__main__":
    sys.exit(main())
