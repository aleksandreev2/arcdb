from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.sqlite_backup import (  # noqa: E402
    SQLiteBackupError,
    verify_backup_directory,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Independently verify an ArchiveDB SQLite backup checksum, integrity, "
            "foreign keys, application query and temporary runtime restore."
        )
    )
    parser.add_argument("backup_dir", help="Backup directory containing manifest.json")
    parser.add_argument(
        "--restore-temp-parent",
        help="Optional explicit parent for the temporary restore test",
    )
    args = parser.parse_args()
    report = verify_backup_directory(
        Path(args.backup_dir),
        restore_temp_parent=(
            None
            if not args.restore_temp_parent
            else Path(args.restore_temp_parent)
        ),
    )
    rows = report["database"]["row_counts"]
    print(
        "SQLite backup verification: PASSED "
        f"({rows['users']} user row(s), "
        f"{rows['user_novel_state']} user-state row(s))."
    )
    print("Independent temporary restore test: PASSED.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLiteBackupError as exc:
        print(f"SQLite backup verification: FAILED [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(
            "SQLite backup verification: FAILED [internal_error]: "
            f"unexpected {type(exc).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
