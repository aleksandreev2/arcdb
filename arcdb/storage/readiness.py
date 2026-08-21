from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .legacy_import import (
    export_allowed_emails,
    export_collections,
    export_custom_meta,
    export_user_data,
    export_user_uploads,
    export_users,
    state_counts,
)
from .safe_migration import discover_snapshot_files, file_fingerprint
from .sqlite_db import SCHEMA_VERSION


class ReadinessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReadinessPaths:
    meta_dir: Path
    db_path: Path
    users_path: Path
    user_data_path: Path
    collections_path: Path
    user_uploads_path: Path
    custom_meta_path: Path
    allowed_emails_path: Path


def resolve_readiness_paths(
    *,
    meta_dir: Path,
    db_path: Path,
    users_path: Path | None = None,
    user_data_path: Path | None = None,
    collections_path: Path | None = None,
    user_uploads_path: Path | None = None,
    custom_meta_path: Path | None = None,
    allowed_emails_path: Path | None = None,
) -> ReadinessPaths:
    meta = meta_dir.expanduser().resolve()
    return ReadinessPaths(
        meta_dir=meta,
        db_path=db_path.expanduser().resolve(),
        users_path=(users_path or meta / "users.json").expanduser().resolve(),
        user_data_path=(user_data_path or meta / "user_data.json").expanduser().resolve(),
        collections_path=(collections_path or meta / "collections.json").expanduser().resolve(),
        user_uploads_path=(user_uploads_path or meta / "user_uploads.json").expanduser().resolve(),
        custom_meta_path=(custom_meta_path or meta / "custom_meta.json").expanduser().resolve(),
        allowed_emails_path=(allowed_emails_path or meta / "allowed_gmails.txt")
        .expanduser()
        .resolve(),
    )


def _known_source_paths(paths: ReadinessPaths) -> tuple[Path, ...]:
    return (
        paths.users_path,
        paths.user_data_path,
        paths.collections_path,
        paths.user_uploads_path,
        paths.custom_meta_path,
        paths.allowed_emails_path,
    )


def _excluded_database_paths(db_path: Path) -> set[str]:
    return {
        str(db_path.resolve()),
        str(Path(str(db_path) + "-wal").resolve()),
        str(Path(str(db_path) + "-shm").resolve()),
    }


def _snapshot_sources(paths: ReadinessPaths) -> dict[str, dict[str, Any]]:
    known = _known_source_paths(paths)
    excluded = _excluded_database_paths(paths.db_path)
    discovered = {
        str(path.resolve()): path.resolve()
        for path in discover_snapshot_files(paths.meta_dir, known)
        if str(path.resolve()) not in excluded
    }
    for path in known:
        resolved = path.resolve()
        if str(resolved) not in excluded:
            discovered.setdefault(str(resolved), resolved)
    return {
        raw_path: file_fingerprint(path)
        for raw_path, path in sorted(discovered.items(), key=lambda item: item[0].casefold())
    }


def _comparable_fingerprint(value: dict[str, Any]) -> tuple[Any, ...]:
    return value.get("exists"), value.get("size"), value.get("sha256")


def _assert_sources_stable(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> None:
    changed = set(before) ^ set(after)
    changed.update(
        path
        for path in set(before) & set(after)
        if _comparable_fingerprint(before[path]) != _comparable_fingerprint(after[path])
    )
    if changed:
        raise ReadinessError(
            "source_changed",
            f"Legacy source state changed during preflight ({len(changed)} path(s)).",
        )


def _source_summary(
    snapshot: dict[str, dict[str, Any]], paths: ReadinessPaths
) -> dict[str, Any]:
    existing = [value for value in snapshot.values() if value.get("exists")]
    digest = hashlib.sha256()
    for index, value in enumerate(existing):
        digest.update(
            f"{index}:{value['size']}:{value['sha256']}\n".encode("ascii")
        )
    known = {str(path.resolve()) for path in _known_source_paths(paths)}
    return {
        "tracked_files": len(snapshot),
        "existing_files": len(existing),
        "unknown_files": sum(
            1
            for raw_path, value in snapshot.items()
            if value.get("exists") and raw_path not in known
        ),
        "total_bytes": sum(int(value["size"]) for value in existing),
        "aggregate_sha256": digest.hexdigest(),
        "stable_during_preflight": True,
    }


def _read_object(path: Path, label: str, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ReadinessError(
                "required_source_missing", f"Required legacy {label} file is missing."
            )
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessError(
            "legacy_source_invalid", f"Legacy {label} could not be read as JSON."
        ) from exc
    if not isinstance(value, dict):
        raise ReadinessError(
            "legacy_source_invalid", f"Legacy {label} must contain a JSON object."
        )
    return value


def _read_allowed_emails(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReadinessError(
            "legacy_source_invalid", "Legacy allowlist could not be read as UTF-8."
        ) from exc
    return sorted(
        {
            line.strip().lower()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        }
    )


def _legacy_memberships(user_data: dict[str, Any]) -> set[tuple[str, str, str]]:
    result: set[tuple[str, str, str]] = set()
    for email, raw_states in user_data.items():
        if not isinstance(raw_states, dict):
            continue
        for novel_key, raw in raw_states.items():
            if not isinstance(raw, dict):
                continue
            memberships = raw.get("collections") or []
            if not isinstance(memberships, list):
                continue
            result.update(
                (str(email), str(collection_id), str(novel_key))
                for collection_id in memberships
                if collection_id is not None
            )
    return result


def _database_snapshot(db_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not db_path.is_file():
        raise ReadinessError("database_missing", "SQLite shadow database is missing.")
    try:
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise ReadinessError(
            "database_unavailable", "SQLite shadow database could not be opened read-only."
        ) from exc

    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        actual_version = None if version is None else str(version[0])
        if actual_version != str(SCHEMA_VERSION):
            raise ReadinessError(
                "schema_mismatch",
                f"SQLite schema mismatch: expected {SCHEMA_VERSION}, got {actual_version}.",
            )

        quick = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
        if quick != ["ok"]:
            raise ReadinessError("quick_check_failed", "SQLite quick_check failed.")
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise ReadinessError(
                "integrity_check_failed", "SQLite integrity_check failed."
            )
        foreign_key_rows = sum(1 for _ in conn.execute("PRAGMA foreign_key_check"))
        if foreign_key_rows:
            raise ReadinessError(
                "foreign_key_check_failed",
                f"SQLite foreign_key_check found {foreign_key_rows} violation(s).",
            )

        snapshot = {
            "users": export_users(conn),
            "user_data": export_user_data(conn),
            "collections": export_collections(conn),
            "user_uploads": export_user_uploads(conn),
            "custom_meta": export_custom_meta(conn),
            "allowed_emails": export_allowed_emails(conn),
            "memberships": {
                (str(row["user_email"]), str(row["collection_id"]), str(row["novel_key"]))
                for row in conn.execute(
                    "SELECT user_email, collection_id, novel_key FROM collection_items"
                )
            },
        }
        counts = state_counts(conn)
        return snapshot, {
            "schema_version": SCHEMA_VERSION,
            "quick_check": "ok",
            "integrity_check": "ok",
            "foreign_key_check_rows": 0,
            "row_counts": counts,
        }
    except ReadinessError:
        raise
    except Exception as exc:
        raise ReadinessError(
            "database_export_failed", "SQLite shadow state could not be exported safely."
        ) from exc
    finally:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        conn.close()


def _differing_keys(left: dict[str, Any], right: dict[str, Any]) -> int:
    return sum(
        1
        for key in set(left) | set(right)
        if (key in left) != (key in right) or left.get(key) != right.get(key)
    )


def _verify_parity(legacy: dict[str, Any], shadow: dict[str, Any]) -> None:
    domains = (
        ("users", "users_parity"),
        ("user_data", "user_data_parity"),
        ("collections", "collections_parity"),
        ("user_uploads", "user_uploads_parity"),
        ("custom_meta", "custom_meta_parity"),
    )
    for domain, code in domains:
        if shadow[domain] != legacy[domain]:
            differing = _differing_keys(legacy[domain], shadow[domain])
            raise ReadinessError(
                code,
                f"Legacy/SQLite {domain} parity failed ({differing} differing key(s)).",
            )
    if shadow["allowed_emails"] != legacy["allowed_emails"]:
        raise ReadinessError(
            "allowed_emails_parity", "Legacy/SQLite allowlist parity failed."
        )
    memberships = _legacy_memberships(legacy["user_data"])
    if shadow["memberships"] != memberships:
        missing = len(memberships - shadow["memberships"])
        extra = len(shadow["memberships"] - memberships)
        raise ReadinessError(
            "collection_items_parity",
            "Legacy/SQLite collection membership parity failed "
            f"({missing} missing, {extra} extra).",
        )


def verify_read_cutover_readiness(paths: ReadinessPaths) -> dict[str, Any]:
    if not paths.meta_dir.is_dir():
        raise ReadinessError(
            "metadata_root_missing", "Explicit metadata root is missing or not a directory."
        )

    before = _snapshot_sources(paths)
    legacy = {
        "users": _read_object(paths.users_path, "users", required=True),
        "user_data": _read_object(paths.user_data_path, "user_data", required=True),
        "collections": _read_object(
            paths.collections_path, "collections", required=True
        ),
        "user_uploads": _read_object(
            paths.user_uploads_path, "user_uploads", required=False
        ),
        "custom_meta": _read_object(
            paths.custom_meta_path, "custom_meta", required=False
        ),
        "allowed_emails": _read_allowed_emails(paths.allowed_emails_path),
    }
    shadow, database = _database_snapshot(paths.db_path)
    after = _snapshot_sources(paths)
    _assert_sources_stable(before, after)
    _verify_parity(legacy, shadow)

    return {
        "format": "archivedb-read-cutover-preflight-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "preflight_passed",
        "legacy_sources": _source_summary(after, paths),
        "database": database,
        "decision": {
            "bounded_canary_authorized": False,
            "primary_read_authorized": False,
            "next_step": "operator_review_then_bounded_shadow_observation",
        },
    }
