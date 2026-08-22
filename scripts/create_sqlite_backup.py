from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.sqlite_backup import (  # noqa: E402
    SQLiteBackupError,
    create_sqlite_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a WAL-aware SQLite online backup, verify integrity and perform "
            "a temporary runtime restore test before publishing the backup directory."
        )
    )
    parser.add_argument("--db", required=True, help="Explicit SQLite source database")
    parser.add_argument(
        "--backup-dir",
        required=True,
        help="New backup directory; existing paths are never overwritten",
    )
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()

    manifest = create_sqlite_backup(
        source_db=Path(args.db),
        backup_dir=Path(args.backup_dir),
        timeout_seconds=args.timeout_seconds,
    )
    rows = manifest["database"]["row_counts"]
    print(
        "SQLite backup: VERIFIED "
        f"({rows['users']} user row(s), "
        f"{rows['user_novel_state']} user-state row(s))."
    )
    print(
        "Restore test: PASSED. Legacy files were not modified and are not safe to delete."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLiteBackupError as exc:
        print(f"SQLite backup: FAILED [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(
            "SQLite backup: FAILED [internal_error]: "
            f"unexpected {type(exc).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
