from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.safe_migration import (  # noqa: E402
    assert_sources_unchanged,
    build_verified_candidate,
    create_verified_snapshot,
    file_fingerprint,
    promote_candidate,
)


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        result[key.strip()] = value
    return result


def resolve_path(raw: str | None, fallback: Path) -> Path:
    path = Path(raw) if raw else fallback
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_allowed(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(
        {
            line.strip().lower()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    )


def update_manifest(path: Path, **values) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(values)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely import ArchiveDB legacy state into a verified SQLite WAL candidate. "
            "Legacy sources are snapshotted and never modified."
        )
    )
    parser.add_argument("--db", help="SQLite target path. Defaults to SQLITE_DB_PATH or ./data/arcdb.sqlite3")
    parser.add_argument(
        "--backup-root",
        help="Directory under which a timestamped migration backup is created. Defaults beside SQLite target.",
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        help="Build/verify candidate and backup only; leave the current SQLite target unchanged.",
    )
    parser.add_argument(
        "--require-core",
        action="store_true",
        help="Fail if core users/user_data/collections files are missing. Recommended for production.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Compatibility flag. Verification is now mandatory on every migration run.",
    )
    args = parser.parse_args()

    env = os.environ.copy()
    env.update(parse_env(ROOT / ".env"))
    meta_dir = resolve_path(env.get("META_DIR"), ROOT / "data" / "metadata")
    db_path = resolve_path(args.db or env.get("SQLITE_DB_PATH"), ROOT / "data" / "arcdb.sqlite3")

    users_path = resolve_path(env.get("USERS_PATH"), meta_dir / "users.json")
    user_data_path = resolve_path(env.get("USER_DATA_PATH"), meta_dir / "user_data.json")
    collections_path = resolve_path(env.get("COLLECTIONS_PATH"), meta_dir / "collections.json")
    uploads_path = resolve_path(env.get("USER_UPLOADS_PATH"), meta_dir / "user_uploads.json")
    custom_meta_path = resolve_path(env.get("CUSTOM_META_PATH"), meta_dir / "custom_meta.json")
    allowed_path = resolve_path(env.get("ALLOWED_EMAILS_PATH"), meta_dir / "allowed_gmails.txt")
    translated_csv = resolve_path(
        env.get("TRANSLATED_CSV_PATH"), ROOT / "data" / "uploaded_novels_tracker.csv"
    )
    raw_csv = resolve_path(env.get("RAW_MASTER_CSV_PATH"), ROOT / "data" / "master_library_index.csv")
    package_jobs_db = resolve_path(
        env.get("PACKAGE_JOBS_DB_PATH"), meta_dir / "package_jobs.sqlite3"
    )
    library_index_db = resolve_path(
        env.get("LIBRARY_INDEX_DB_PATH"), meta_dir / "library_index.sqlite3"
    )
    derived_databases = [db_path, package_jobs_db, library_index_db]

    core_paths = [users_path, user_data_path, collections_path]
    if args.require_core:
        missing = [str(path) for path in core_paths if not path.exists()]
        if missing:
            raise RuntimeError("Required production state files are missing: " + ", ".join(missing))

    # Explicit paths are tracked even when absent. All existing files under META_DIR
    # are also copied recursively so unknown legacy state is preserved.
    explicit_snapshot_files = [
        users_path,
        user_data_path,
        collections_path,
        uploads_path,
        custom_meta_path,
        allowed_path,
        translated_csv,
        raw_csv,
    ]

    docs = {
        "users": read_json(users_path, {}),
        "user_data": read_json(user_data_path, {}),
        "collections": read_json(collections_path, {}),
        "user_uploads": read_json(uploads_path, {}),
        "custom_meta": read_json(custom_meta_path, {}),
        "allowed_emails": read_allowed(allowed_path),
    }

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{os.getpid()}"
    backup_root = resolve_path(
        args.backup_root,
        db_path.parent / "migration-backups",
    )
    backup_dir = backup_root / stamp
    candidate_path = db_path.parent / f"{db_path.name}.candidate-{stamp}"

    snapshot = create_verified_snapshot(
        backup_dir=backup_dir,
        meta_dir=meta_dir,
        explicit_files=explicit_snapshot_files,
        excluded_files=derived_databases,
    )
    manifest_path = backup_dir / "manifest.json"
    update_manifest(
        manifest_path,
        migration={
            "status": "snapshot_verified",
            "target_db": str(db_path),
            "candidate_db": str(candidate_path),
            "promote_requested": not args.no_promote,
        },
    )

    counts, checks = build_verified_candidate(candidate_path=candidate_path, docs=docs)

    # If the web app or another process changed any metadata/CSV while the candidate
    # was being built, fail closed. The candidate and backup remain for inspection.
    assert_sources_unchanged(
        snapshot["fingerprints"],
        meta_dir=meta_dir,
        explicit_files=explicit_snapshot_files,
        excluded_files=derived_databases,
    )

    candidate_fp = file_fingerprint(candidate_path)
    update_manifest(
        manifest_path,
        migration={
            "status": "candidate_verified",
            "target_db": str(db_path),
            "candidate_db": str(candidate_path),
            "candidate": candidate_fp,
            "row_counts": counts,
            "sqlite_checks": checks,
            "sources_unchanged": True,
            "promote_requested": not args.no_promote,
        },
    )

    previous_backup = None
    if not args.no_promote:
        previous_backup = promote_candidate(candidate_path, db_path, backup_dir)
        target_fp = file_fingerprint(db_path)
        update_manifest(
            manifest_path,
            migration={
                "status": "promoted",
                "target_db": str(db_path),
                "target": target_fp,
                "candidate": candidate_fp,
                "previous_sqlite_backup": str(previous_backup) if previous_backup else None,
                "row_counts": counts,
                "sqlite_checks": checks,
                "sources_unchanged": True,
                "promote_requested": True,
            },
        )

    print(f"Legacy snapshot: {backup_dir}")
    print(f"Manifest: {manifest_path}")
    print(f"Verified candidate: {candidate_path if args.no_promote else db_path}")
    print("Imported rows:")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    print("SQLite quick_check: OK")
    print("SQLite integrity_check: OK")
    print("SQLite foreign_key_check: OK")
    print("Legacy source hashes unchanged: OK")
    if args.no_promote:
        print("Promotion: SKIPPED (--no-promote). Existing SQLite target was not changed.")
    else:
        print("Promotion: OK")
        if previous_backup:
            print(f"Previous SQLite backup: {previous_backup}")
    print("Legacy files were not modified or deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
