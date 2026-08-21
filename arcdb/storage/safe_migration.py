from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .legacy_import import (
    export_allowed_emails,
    export_collections,
    export_custom_meta,
    export_user_data,
    export_user_uploads,
    export_users,
    replace_from_documents,
)
from .sqlite_db import SCHEMA_VERSION, connect_db, initialize_schema


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def discover_snapshot_files(meta_dir: Path, explicit_files: Iterable[Path]) -> list[Path]:
    discovered: dict[str, Path] = {}
    if meta_dir.exists():
        for path in meta_dir.rglob("*"):
            if path.is_file():
                discovered[str(path.resolve())] = path.resolve()
    for path in explicit_files:
        resolved = path.resolve()
        if resolved.exists() and resolved.is_file():
            discovered[str(resolved)] = resolved
    return sorted(discovered.values(), key=lambda p: str(p).casefold())


def snapshot_fingerprints(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    return {str(path.resolve()): file_fingerprint(path.resolve()) for path in paths}


def assert_sources_unchanged(
    before: dict[str, dict[str, Any]],
    *,
    meta_dir: Path,
    explicit_files: Iterable[Path],
) -> None:
    changed: list[str] = []
    for raw_path, expected in before.items():
        current = file_fingerprint(Path(raw_path))
        comparable_expected = {k: expected.get(k) for k in ("exists", "size", "sha256")}
        comparable_current = {k: current.get(k) for k in ("exists", "size", "sha256")}
        if comparable_current != comparable_expected:
            changed.append(raw_path)

    expected_existing = {path for path, fp in before.items() if fp.get("exists")}
    current_existing = {str(path.resolve()) for path in discover_snapshot_files(meta_dir, explicit_files)}
    if current_existing != expected_existing:
        added = sorted(current_existing - expected_existing)
        removed = sorted(expected_existing - current_existing)
        changed.extend([f"added:{p}" for p in added])
        changed.extend([f"removed:{p}" for p in removed])

    if changed:
        raise RuntimeError(
            "Legacy source state changed during migration; candidate will not be promoted: "
            + ", ".join(changed)
        )


def create_verified_snapshot(
    *,
    backup_dir: Path,
    meta_dir: Path,
    explicit_files: Iterable[Path],
) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=False)
    files_dir = backup_dir / "legacy-files"
    files_dir.mkdir(parents=True, exist_ok=False)

    explicit = [path.resolve() for path in explicit_files]
    source_files = discover_snapshot_files(meta_dir, explicit)
    tracked_paths = {str(path.resolve()): path.resolve() for path in source_files}
    for path in explicit:
        tracked_paths.setdefault(str(path), path)
    before = snapshot_fingerprints(tracked_paths.values())
    entries: list[dict[str, Any]] = []

    meta_root = meta_dir.resolve()
    for index, source in enumerate(source_files):
        resolved = source.resolve()
        try:
            relative = Path("metadata") / resolved.relative_to(meta_root)
        except ValueError:
            relative = Path("external") / f"{index:04d}-{resolved.name}"
        destination = files_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, destination)
        src_fp = before[str(resolved)]
        dst_fp = file_fingerprint(destination)
        if src_fp["size"] != dst_fp["size"] or src_fp["sha256"] != dst_fp["sha256"]:
            raise RuntimeError(f"Backup verification failed for {resolved}")
        entries.append(
            {
                "source": str(resolved),
                "backup": str(destination),
                "size": src_fp["size"],
                "sha256": src_fp["sha256"],
                "mtime_ns": src_fp["mtime_ns"],
            }
        )

    manifest = {
        "format": 1,
        "created_at_unix": time.time(),
        "metadata_root": str(meta_root),
        "files": entries,
        "tracked_sources": before,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"manifest": manifest, "fingerprints": before}


def verify_roundtrip(conn: sqlite3.Connection, docs: dict[str, Any]) -> None:
    checks = {
        "users": export_users(conn) == docs["users"],
        "user_data": export_user_data(conn) == docs["user_data"],
        "collections": export_collections(conn) == docs["collections"],
        "user_uploads": export_user_uploads(conn) == docs["user_uploads"],
        "custom_meta": export_custom_meta(conn) == docs["custom_meta"],
        "allowed_emails": export_allowed_emails(conn) == sorted(set(docs["allowed_emails"])),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError("SQLite round-trip verification failed for: " + ", ".join(failed))


def verify_database(conn: sqlite3.Connection) -> dict[str, Any]:
    quick_rows = [row[0] for row in conn.execute("PRAGMA quick_check")]
    if quick_rows != ["ok"]:
        raise RuntimeError(f"SQLite quick_check failed: {quick_rows}")

    integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        raise RuntimeError(f"SQLite integrity_check failed: {integrity_rows}")

    foreign_rows = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    if foreign_rows:
        raise RuntimeError(f"SQLite foreign_key_check failed: {foreign_rows[:20]}")

    version_row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if version_row is None or version_row[0] != str(SCHEMA_VERSION):
        raise RuntimeError(
            f"Unexpected SQLite schema version: {None if version_row is None else version_row[0]}"
        )

    return {
        "quick_check": quick_rows,
        "integrity_check": integrity_rows,
        "foreign_key_check_rows": 0,
        "schema_version": SCHEMA_VERSION,
    }


def build_verified_candidate(
    *,
    candidate_path: Path,
    docs: dict[str, Any],
) -> tuple[dict[str, int], dict[str, Any]]:
    if candidate_path.exists():
        raise FileExistsError(candidate_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(candidate_path) + suffix)
        if sidecar.exists():
            raise FileExistsError(sidecar)

    conn = connect_db(candidate_path)
    try:
        initialize_schema(conn)
        counts = replace_from_documents(conn, **docs)
        verify_roundtrip(conn, docs)
        checks = verify_database(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        conn.commit()
    finally:
        conn.close()

    conn = connect_db(candidate_path)
    try:
        verify_roundtrip(conn, docs)
        checks = verify_database(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        conn.close()
    return counts, checks


def _active_sidecars(db_path: Path) -> list[Path]:
    return [
        sidecar
        for sidecar in (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"))
        if sidecar.exists()
    ]


def preserve_existing_database(db_path: Path, backup_dir: Path) -> Path | None:
    if not db_path.exists():
        return None
    active = _active_sidecars(db_path)
    if active:
        raise RuntimeError(
            "Refusing to replace an SQLite target with WAL/SHM sidecars present. "
            "Stop users of the database and checkpoint/close it first: "
            + ", ".join(str(path) for path in active)
        )
    previous_dir = backup_dir / "previous-sqlite"
    previous_dir.mkdir(parents=True, exist_ok=True)
    backup = previous_dir / (db_path.name + ".verified-copy")
    shutil.copy2(db_path, backup)
    if sha256_file(db_path) != sha256_file(backup):
        raise RuntimeError("Existing SQLite backup checksum mismatch")
    return backup


def promote_candidate(candidate_path: Path, db_path: Path, backup_dir: Path) -> Path | None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    previous_copy = preserve_existing_database(db_path, backup_dir)
    moved_previous: Path | None = None

    if db_path.exists():
        previous_dir = backup_dir / "previous-sqlite"
        moved_previous = previous_dir / (db_path.name + ".pre-migration-original")
        os.replace(db_path, moved_previous)

    try:
        # Candidate and target are siblings, so this rename is atomic on the same filesystem.
        os.replace(candidate_path, db_path)
    except Exception:
        if moved_previous is not None and moved_previous.exists() and not db_path.exists():
            os.replace(moved_previous, db_path)
        raise

    conn = connect_db(db_path)
    try:
        verify_database(conn)
    except Exception:
        # Restore the previous target if we had one. The verified copy remains even
        # after a successful restoration, so rollback never depends on a single file.
        if moved_previous is not None and moved_previous.exists():
            failed_target = backup_dir / "failed-promoted-sqlite.sqlite3"
            if db_path.exists():
                os.replace(db_path, failed_target)
            os.replace(moved_previous, db_path)
        raise
    finally:
        conn.close()

    return previous_copy
