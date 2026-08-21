from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.readiness import (  # noqa: E402
    ReadinessError,
    resolve_readiness_paths,
    verify_read_cutover_readiness,
)


def _path(value: str | None) -> Path | None:
    return None if value is None else Path(value).expanduser().resolve()


def write_report(path: Path, report: dict) -> None:
    destination = path.expanduser().resolve()
    if not destination.parent.is_dir():
        raise ReadinessError(
            "report_parent_missing", "Report parent directory does not exist."
        )
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ReadinessError(
            "report_exists", "Refusing to overwrite an existing readiness report."
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a read-only, payload-safe ArchiveDB preflight before any "
            "production SQLite read canary."
        )
    )
    parser.add_argument("--meta-dir", required=True, help="Explicit legacy metadata root")
    parser.add_argument("--db", required=True, help="Explicit SQLite shadow database")
    parser.add_argument("--users")
    parser.add_argument("--user-data")
    parser.add_argument("--collections")
    parser.add_argument("--user-uploads")
    parser.add_argument("--custom-meta")
    parser.add_argument("--allowed-emails")
    parser.add_argument(
        "--report",
        help="Optional new JSON report path; existing files are never overwritten",
    )
    args = parser.parse_args()

    paths = resolve_readiness_paths(
        meta_dir=Path(args.meta_dir),
        db_path=Path(args.db),
        users_path=_path(args.users),
        user_data_path=_path(args.user_data),
        collections_path=_path(args.collections),
        user_uploads_path=_path(args.user_uploads),
        custom_meta_path=_path(args.custom_meta),
        allowed_emails_path=_path(args.allowed_emails),
    )
    report = verify_read_cutover_readiness(paths)
    if args.report:
        write_report(Path(args.report), report)

    rows = report["database"]["row_counts"]
    sources = report["legacy_sources"]
    print(
        "Read cutover preflight: PASSED "
        f"({sources['existing_files']} legacy file(s), "
        f"{rows['users']} auth user(s), "
        f"{rows['user_novel_state']} user-state record(s), "
        f"{rows['collections']} collection(s))."
    )
    print(
        "SQLite primary reads are NOT authorized by this report; "
        "operator review and bounded shadow observation remain required."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReadinessError as exc:
        print(
            f"Read cutover preflight: FAILED [{exc.code}]: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except Exception as exc:
        print(
            "Read cutover preflight: FAILED [internal_error]: "
            f"unexpected {type(exc).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
