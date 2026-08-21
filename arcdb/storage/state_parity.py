from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .legacy_import import export_collections, export_user_data
from .runtime_state import ShadowStateError
from .sqlite_db import SCHEMA_VERSION


def load_legacy_user_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ShadowStateError(f"Legacy user_data is not an object: {path}")
    return data


def load_legacy_collections(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ShadowStateError(f"Legacy collections is not an object: {path}")
    return data


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _legacy_memberships(legacy: dict[str, Any]) -> set[tuple[str, str, str]]:
    memberships: set[tuple[str, str, str]] = set()
    for email, raw_states in legacy.items():
        if not isinstance(raw_states, dict):
            continue
        for novel_key, raw in raw_states.items():
            if not isinstance(raw, dict):
                continue
            collection_ids = raw.get("collections") or []
            if not isinstance(collection_ids, list):
                continue
            memberships.update(
                (str(email), str(collection_id), str(novel_key))
                for collection_id in collection_ids
                if collection_id is not None
            )
    return memberships


def verify_user_data_parity(*, user_data_path: Path, db_path: Path) -> dict[str, int]:
    legacy = load_legacy_user_data(user_data_path)
    if not db_path.is_file():
        raise ShadowStateError(f"SQLite shadow database is missing: {db_path}")

    conn = _connect_readonly(db_path)
    try:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if version is None or str(version[0]) != str(SCHEMA_VERSION):
            raise ShadowStateError(
                f"SQLite shadow schema mismatch: expected {SCHEMA_VERSION}, "
                f"got {None if version is None else version[0]}."
            )
        shadow = export_user_data(conn)
        shadow_memberships = {
            (row["user_email"], row["collection_id"], row["novel_key"])
            for row in conn.execute(
                "SELECT user_email, collection_id, novel_key FROM collection_items"
            )
        }
    finally:
        conn.close()

    if shadow != legacy:
        legacy_users = set(legacy)
        shadow_users = set(shadow)
        differing_users = sorted(
            email
            for email in legacy_users | shadow_users
            if legacy.get(email) != shadow.get(email)
        )
        sample = ", ".join(differing_users[:10])
        extra = "" if len(differing_users) <= 10 else f" (+{len(differing_users) - 10} more)"
        raise ShadowStateError(
            "Legacy/SQLite user_data parity failed for "
            f"{len(differing_users)} user(s): {sample}{extra}"
        )

    legacy_memberships = _legacy_memberships(legacy)
    if shadow_memberships != legacy_memberships:
        missing = sorted(legacy_memberships - shadow_memberships)
        extra_rows = sorted(shadow_memberships - legacy_memberships)
        raise ShadowStateError(
            "Legacy/SQLite collection_items parity failed: "
            f"{len(missing)} missing and {len(extra_rows)} extra membership(s); "
            f"sample missing={missing[:3]}, extra={extra_rows[:3]}"
        )

    rows = sum(len(value) for value in legacy.values() if isinstance(value, dict))
    return {
        "users": len(legacy),
        "records": rows,
        "memberships": len(legacy_memberships),
    }


def verify_collections_parity(*, collections_path: Path, db_path: Path) -> dict[str, int]:
    legacy = load_legacy_collections(collections_path)
    if not db_path.is_file():
        raise ShadowStateError(f"SQLite shadow database is missing: {db_path}")

    conn = _connect_readonly(db_path)
    try:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if version is None or str(version[0]) != str(SCHEMA_VERSION):
            raise ShadowStateError(
                f"SQLite shadow schema mismatch: expected {SCHEMA_VERSION}, "
                f"got {None if version is None else version[0]}."
            )
        shadow = export_collections(conn)
    finally:
        conn.close()

    if shadow != legacy:
        legacy_users = set(legacy)
        shadow_users = set(shadow)
        differing_users = sorted(
            email
            for email in legacy_users | shadow_users
            if legacy.get(email) != shadow.get(email)
        )
        sample = ", ".join(differing_users[:10])
        extra = "" if len(differing_users) <= 10 else f" (+{len(differing_users) - 10} more)"
        raise ShadowStateError(
            "Legacy/SQLite collections parity failed for "
            f"{len(differing_users)} user(s): {sample}{extra}"
        )

    rows = sum(len(value) for value in legacy.values() if isinstance(value, list))
    return {"users": len(legacy), "collections": rows}
