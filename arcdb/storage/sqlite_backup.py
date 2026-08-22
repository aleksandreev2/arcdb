from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .legacy_import import state_counts
from .safe_migration import sha256_file, verify_database
from .sqlite_db import connect_db


BACKUP_FORMAT = "archivedb-sqlite-backup-v1"
BACKUP_FILENAME = "arcdb.sqlite3"
MANIFEST_FILENAME = "manifest.json"


class SQLiteBackupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SQLiteBackupError("database_missing", "SQLite database is missing.")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise SQLiteBackupError(
            "database_unavailable", "SQLite database could not be opened read-only."
        ) from exc


def verify_sqlite_file(path: Path) -> dict[str, Any]:
    database = path.expanduser().resolve()
    conn = _readonly_connection(database)
    try:
        checks = verify_database(conn)
        counts = state_counts(conn)
        application_probe = conn.execute(
            "SELECT COUNT(*) FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if application_probe is None or int(application_probe[0]) != 1:
            raise SQLiteBackupError(
                "application_probe_failed",
                "SQLite application-level schema probe failed.",
            )
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    except SQLiteBackupError:
        raise
    except Exception as exc:
        raise SQLiteBackupError(
            "database_verification_failed", "SQLite database verification failed."
        ) from exc
    finally:
        conn.close()
    return {
        "schema_version": checks["schema_version"],
        "quick_check": checks["quick_check"][0],
        "integrity_check": checks["integrity_check"][0],
        "foreign_key_check_rows": checks["foreign_key_check_rows"],
        "row_counts": counts,
        "application_probe": "ok",
        "journal_mode": journal_mode,
    }


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for fsync(). The backup artifact is
    # already complete at this point; opening it read/write does not alter it.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if path.exists():
        raise SQLiteBackupError(
            "manifest_exists", "Refusing to overwrite an existing backup manifest."
        )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _sidecar_summary(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        result[suffix.removeprefix("-")] = {
            "observed": sidecar.is_file(),
            "size": sidecar.stat().st_size if sidecar.is_file() else 0,
        }
    return result


def _copy_with_hash(source: Path, destination: Path) -> None:
    if destination.exists():
        raise SQLiteBackupError(
            "restore_target_exists", "Refusing to overwrite a restore target."
        )
    shutil.copy2(source, destination)
    _fsync_file(destination)
    if sha256_file(source) != sha256_file(destination):
        raise SQLiteBackupError(
            "copy_checksum_mismatch", "Restored SQLite copy checksum mismatch."
        )


def _runtime_restore_probe(path: Path) -> dict[str, Any]:
    conn = connect_db(path)
    try:
        checks = verify_database(conn)
        counts = state_counts(conn)
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise SQLiteBackupError(
                "application_probe_failed", "Restored SQLite schema probe failed."
            )
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise SQLiteBackupError(
                "restore_checkpoint_failed", "Restored SQLite checkpoint failed."
            )
    except SQLiteBackupError:
        raise
    except Exception as exc:
        raise SQLiteBackupError(
            "restore_probe_failed", "Restored SQLite runtime probe failed."
        ) from exc
    finally:
        conn.close()
    return {
        "schema_version": checks["schema_version"],
        "row_counts": counts,
        "application_probe": "ok",
        "runtime_wal_open": "ok",
    }


def restore_test(
    backup_file: Path,
    *,
    expected_counts: dict[str, int],
    temp_parent: Path,
) -> dict[str, Any]:
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".arcdb-restore-test-", dir=temp_parent
    ) as raw_temp:
        restored = Path(raw_temp) / BACKUP_FILENAME
        _copy_with_hash(backup_file, restored)
        read_only = verify_sqlite_file(restored)
        runtime = _runtime_restore_probe(restored)
        if read_only["row_counts"] != expected_counts:
            raise SQLiteBackupError(
                "restore_count_mismatch",
                "Restored SQLite row counts differ from the backup manifest.",
            )
        if runtime["row_counts"] != expected_counts:
            raise SQLiteBackupError(
                "restore_count_mismatch",
                "Runtime restore probe row counts differ from the backup manifest.",
            )
        return {
            "status": "passed",
            "quick_check": read_only["quick_check"],
            "integrity_check": read_only["integrity_check"],
            "foreign_key_check_rows": read_only["foreign_key_check_rows"],
            "application_probe": runtime["application_probe"],
            "runtime_wal_open": runtime["runtime_wal_open"],
        }


def create_sqlite_backup(
    *,
    source_db: Path,
    backup_dir: Path,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    source = source_db.expanduser().resolve()
    target_dir = backup_dir.expanduser().resolve()
    if timeout_seconds <= 0:
        raise SQLiteBackupError(
            "invalid_timeout", "Backup timeout must be greater than zero."
        )
    if not source.is_file():
        raise SQLiteBackupError("database_missing", "SQLite source database is missing.")
    if target_dir.exists():
        raise SQLiteBackupError(
            "backup_exists", "Refusing to overwrite an existing backup directory."
        )

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(
            prefix=f".{target_dir.name}.incomplete-", dir=target_dir.parent
        )
    )
    incomplete = staging / f".{BACKUP_FILENAME}.incomplete"
    final_database = staging / BACKUP_FILENAME
    deadline = time.monotonic() + timeout_seconds
    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        source_conn = _readonly_connection(source)
        verify_database(source_conn)
        destination_conn = sqlite3.connect(incomplete, timeout=5.0)
        destination_conn.execute("PRAGMA busy_timeout=5000")

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() > deadline:
                raise SQLiteBackupError(
                    "backup_timeout", "SQLite online backup exceeded its timeout."
                )

        source_conn.backup(
            destination_conn,
            pages=1024,
            progress=progress,
            sleep=0.05,
        )
        destination_conn.commit()
        mode = destination_conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        if mode is None or str(mode[0]).lower() != "delete":
            raise SQLiteBackupError(
                "backup_not_portable",
                "Backup could not be converted to a portable single-file database.",
            )
        destination_conn.execute("PRAGMA synchronous=FULL")
        destination_conn.close()
        destination_conn = None
        source_conn.close()
        source_conn = None

        _fsync_file(incomplete)
        os.replace(incomplete, final_database)
        backup_checks = verify_sqlite_file(final_database)
        artifact_sha256 = sha256_file(final_database)
        restore_checks = restore_test(
            final_database,
            expected_counts=backup_checks["row_counts"],
            temp_parent=staging,
        )
        manifest = {
            "format": BACKUP_FORMAT,
            "created_at_utc": _utc_now(),
            "method": "sqlite_online_backup_api",
            "source": {
                "filename": source.name,
                "sidecars_observed": _sidecar_summary(source),
            },
            "artifact": {
                "filename": BACKUP_FILENAME,
                "size": final_database.stat().st_size,
                "sha256": artifact_sha256,
                "portable_single_file": True,
            },
            "database": backup_checks,
            "restore_test": restore_checks,
            "decision": {
                "backup_verified": True,
                "restore_verified": True,
                "safe_to_delete_legacy_data": False,
            },
        }
        _write_manifest(staging / MANIFEST_FILENAME, manifest)
        # Parse the finished artifact and manifest through the independent verifier
        # before the directory becomes visible at its final name.
        verify_backup_directory(
            staging,
            restore_temp_parent=target_dir.parent,
        )
        if target_dir.exists():
            raise SQLiteBackupError(
                "backup_exists", "Backup target appeared during backup creation."
            )
        os.rename(staging, target_dir)
        staging = None
        return manifest
    except SQLiteBackupError:
        raise
    except sqlite3.Error as exc:
        raise SQLiteBackupError(
            "backup_failed", "SQLite online backup failed."
        ) from exc
    except Exception as exc:
        raise SQLiteBackupError("backup_failed", "SQLite backup failed.") from exc
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            source_conn.close()
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _load_manifest(backup_dir: Path) -> tuple[Path, dict[str, Any]]:
    directory = backup_dir.expanduser().resolve()
    manifest_path = directory / MANIFEST_FILENAME
    if (
        not directory.is_dir()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        raise SQLiteBackupError(
            "backup_manifest_missing", "SQLite backup manifest is missing."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SQLiteBackupError(
            "backup_manifest_invalid", "SQLite backup manifest is invalid."
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise SQLiteBackupError(
            "backup_manifest_invalid", "Unexpected SQLite backup manifest format."
        )
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("filename") != BACKUP_FILENAME:
        raise SQLiteBackupError(
            "backup_manifest_invalid", "SQLite backup artifact entry is invalid."
        )
    return directory, manifest


def verify_backup_directory(
    backup_dir: Path,
    *,
    restore_temp_parent: Path | None = None,
) -> dict[str, Any]:
    directory, manifest = _load_manifest(backup_dir)
    artifact = directory / BACKUP_FILENAME
    if artifact.is_symlink() or not artifact.is_file():
        raise SQLiteBackupError(
            "backup_artifact_missing", "SQLite backup artifact is missing."
        )
    expected = manifest["artifact"]
    if (
        expected.get("portable_single_file") is not True
        or not isinstance(expected.get("size"), int)
        or expected["size"] < 1
        or not isinstance(expected.get("sha256"), str)
        or len(expected["sha256"]) != 64
    ):
        raise SQLiteBackupError(
            "backup_manifest_invalid", "SQLite backup artifact metadata is invalid."
        )
    if artifact.stat().st_size != expected.get("size"):
        raise SQLiteBackupError(
            "backup_size_mismatch", "SQLite backup artifact size mismatch."
        )
    if sha256_file(artifact) != expected.get("sha256"):
        raise SQLiteBackupError(
            "backup_checksum_mismatch", "SQLite backup artifact checksum mismatch."
        )
    sidecars = [
        path
        for path in (Path(str(artifact) + "-wal"), Path(str(artifact) + "-shm"))
        if path.exists()
    ]
    if sidecars:
        raise SQLiteBackupError(
            "backup_sidecars_present",
            "Portable SQLite backup unexpectedly has WAL/SHM sidecars.",
        )
    checks = verify_sqlite_file(artifact)
    if checks["journal_mode"] != "delete":
        raise SQLiteBackupError(
            "backup_not_portable",
            "SQLite backup is not in portable single-file journal mode.",
        )
    expected_counts = manifest.get("database", {}).get("row_counts")
    if not isinstance(expected_counts, dict) or checks["row_counts"] != expected_counts:
        raise SQLiteBackupError(
            "backup_count_mismatch", "SQLite backup row counts differ from manifest."
        )
    temp_parent = (restore_temp_parent or directory.parent).expanduser().resolve()
    if temp_parent == directory or temp_parent.is_relative_to(directory):
        raise SQLiteBackupError(
            "restore_temp_inside_backup",
            "Restore-test workspace must be outside the immutable backup directory.",
        )
    restore_checks = restore_test(
        artifact,
        expected_counts=expected_counts,
        temp_parent=temp_parent,
    )
    return {
        "format": BACKUP_FORMAT,
        "status": "backup_verified",
        "database": checks,
        "restore_test": restore_checks,
        "decision": {
            "backup_verified": True,
            "restore_verified": True,
            "safe_to_delete_legacy_data": False,
        },
    }


def restore_backup_to_new_target(
    *,
    backup_dir: Path,
    target_db: Path,
    verify_temp_parent: Path | None = None,
) -> dict[str, Any]:
    directory, manifest = _load_manifest(backup_dir)
    verification = verify_backup_directory(
        directory, restore_temp_parent=verify_temp_parent
    )
    target = target_db.expanduser().resolve()
    if target == directory or target.is_relative_to(directory):
        raise SQLiteBackupError(
            "restore_target_inside_backup",
            "Restore target must be outside the immutable backup directory.",
        )
    if target.exists() or any(
        Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm")
    ):
        raise SQLiteBackupError(
            "restore_target_exists",
            "Refusing to overwrite an existing SQLite restore target or sidecar.",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    incomplete = target.parent / f".{target.name}.restore-{uuid.uuid4().hex}.incomplete"
    try:
        _copy_with_hash(directory / BACKUP_FILENAME, incomplete)
        checks = verify_sqlite_file(incomplete)
        if checks["row_counts"] != manifest["database"]["row_counts"]:
            raise SQLiteBackupError(
                "restore_count_mismatch", "Restore target row counts differ from manifest."
            )
        if target.exists():
            raise SQLiteBackupError(
                "restore_target_exists", "Restore target appeared during restoration."
            )
        os.rename(incomplete, target)
        final_checks = verify_sqlite_file(target)
        return {
            "format": BACKUP_FORMAT,
            "status": "restored_to_new_target",
            "database": final_checks,
            "source_backup_verified": verification["status"] == "backup_verified",
            "decision": {
                "active_database_replaced": False,
                "legacy_data_modified": False,
            },
        }
    finally:
        if incomplete.exists():
            incomplete.unlink()
