from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .legacy_import import export_user_data
from .runtime_state import ShadowStateError
from .sqlite_db import SCHEMA_VERSION, connect_db


def load_legacy_user_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ShadowStateError(f"Legacy user_data is not an object: {path}")
    return data


def verify_user_data_parity(*, user_data_path: Path, db_path: Path) -> dict[str, int]:
    legacy = load_legacy_user_data(user_data_path)
    if not db_path.is_file():
        raise ShadowStateError(f"SQLite shadow database is missing: {db_path}")

    conn = connect_db(db_path)
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

    rows = sum(len(value) for value in legacy.values() if isinstance(value, dict))
    return {"users": len(legacy), "records": rows}
