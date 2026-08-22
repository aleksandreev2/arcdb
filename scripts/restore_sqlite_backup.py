from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.sqlite_backup import (  # noqa: E402
    SQLiteBackupError,
    restore_backup_to_new_target,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify an ArchiveDB SQLite backup and restore it to a new database "
            "path. Existing database files and WAL/SHM sidecars are never overwritten."
        )
    )
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument(
        "--restore-temp-parent",
        help="Optional explicit parent for the pre-restore verification copy",
    )
    args = parser.parse_args()
    result = restore_backup_to_new_target(
        backup_dir=Path(args.backup_dir),
        target_db=Path(args.target_db),
        verify_temp_parent=(
            None
            if not args.restore_temp_parent
            else Path(args.restore_temp_parent)
        ),
    )
    rows = result["database"]["row_counts"]
    print(
        "SQLite restore: VERIFIED NEW TARGET "
        f"({rows['users']} user row(s), "
        f"{rows['user_novel_state']} user-state row(s))."
    )
    print("No active database or legacy file was replaced.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLiteBackupError as exc:
        print(f"SQLite restore: FAILED [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(
            "SQLite restore: FAILED [internal_error]: "
            f"unexpected {type(exc).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
