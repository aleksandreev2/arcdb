from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, TypeVar

from .legacy_import import (
    export_allowed_emails,
    export_collections,
    export_custom_meta,
    export_user_data,
    export_user_uploads,
    export_users,
)
from .sqlite_db import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[2]
T = TypeVar("T")


class StateReadError(RuntimeError):
    pass


def state_read_backend() -> str:
    backend = os.environ.get("STATE_READ_BACKEND", "legacy").strip().lower()
    if backend not in {"legacy", "sqlite"}:
        raise StateReadError(
            "STATE_READ_BACKEND must be 'legacy' or 'sqlite', "
            f"not {backend!r}."
        )
    return backend


def _db_path() -> Path:
    raw = os.environ.get("SQLITE_DB_PATH", "./data/arcdb.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _read_sqlite(exporter: Callable[[sqlite3.Connection], T]) -> T:
    db_path = _db_path()
    if not db_path.is_file():
        raise StateReadError(
            f"SQLite read backend is missing at {db_path}; switch to legacy or run the safe migration."
        )
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if version is None or str(version[0]) != str(SCHEMA_VERSION):
            raise StateReadError(
                f"SQLite read schema mismatch: expected {SCHEMA_VERSION}, "
                f"got {None if version is None else version[0]}."
            )
        return exporter(conn)
    except sqlite3.Error as exc:
        raise StateReadError(f"SQLite read backend failed: {exc}") from exc
    finally:
        conn.close()


def _read(legacy_loader: Callable[[], T], exporter: Callable[[sqlite3.Connection], T]) -> T:
    if state_read_backend() == "legacy":
        return legacy_loader()
    return _read_sqlite(exporter)


def read_users(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read(legacy_loader, export_users)


def read_user_data(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read(legacy_loader, export_user_data)


def read_collections(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read(legacy_loader, export_collections)


def read_user_uploads(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read(legacy_loader, export_user_uploads)


def read_custom_meta(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read(legacy_loader, export_custom_meta)


def read_allowed_emails(legacy_loader: Callable[[], set[str]]) -> set[str]:
    values = _read(legacy_loader, export_allowed_emails)
    return set(values)
