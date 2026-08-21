from __future__ import annotations

import os
import sqlite3
import sys
import threading
from collections import defaultdict
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


class StateReadComparisonError(StateReadError):
    pass


_METRIC_NAMES = (
    "legacy_reads",
    "sqlite_reads",
    "shadow_attempts",
    "shadow_matches",
    "shadow_mismatches",
    "shadow_errors",
)
_metrics_lock = threading.Lock()
_metrics: dict[str, dict[str, int]] = defaultdict(
    lambda: {name: 0 for name in _METRIC_NAMES}
)


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise StateReadError(f"{name} must be a boolean flag, not {raw!r}.")


def _shadow_report_every() -> int:
    raw = os.environ.get("STATE_READ_SHADOW_REPORT_EVERY", "1000").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise StateReadError(
            "STATE_READ_SHADOW_REPORT_EVERY must be a positive integer."
        ) from exc
    if value < 1:
        raise StateReadError(
            "STATE_READ_SHADOW_REPORT_EVERY must be a positive integer."
        )
    return value


def _increment(domain: str, metric: str) -> int:
    with _metrics_lock:
        _metrics[domain][metric] += 1
        return _metrics[domain][metric]


def state_read_metrics() -> dict[str, dict[str, int]]:
    """Return a process-local, payload-free snapshot for diagnostics/tests."""
    with _metrics_lock:
        return {domain: values.copy() for domain, values in sorted(_metrics.items())}


def _emit_shadow_event(
    event: str,
    domain: str,
    count: int,
    *,
    error_type: str | None = None,
) -> None:
    fields = [
        "[STATE-READ-SHADOW]",
        f"event={event}",
        f"domain={domain}",
        f"count={count}",
    ]
    if error_type is not None:
        fields.append(f"error_type={error_type}")
    try:
        print(" ".join(fields), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # Observability must not break a non-strict authoritative legacy read.
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


def _read(
    domain: str,
    legacy_loader: Callable[[], T],
    exporter: Callable[[sqlite3.Connection], T],
) -> T:
    backend = state_read_backend()
    shadow_compare = _flag("STATE_READ_SHADOW_COMPARE")
    if backend == "sqlite":
        if shadow_compare:
            raise StateReadError(
                "STATE_READ_SHADOW_COMPARE requires STATE_READ_BACKEND=legacy."
            )
        value = _read_sqlite(exporter)
        _increment(domain, "sqlite_reads")
        return value

    value = legacy_loader()
    _increment(domain, "legacy_reads")
    if not shadow_compare:
        return value

    _increment(domain, "shadow_attempts")
    strict = _flag("STATE_READ_SHADOW_STRICT")
    try:
        shadow = _read_sqlite(exporter)
    except Exception as exc:
        count = _increment(domain, "shadow_errors")
        _emit_shadow_event("error", domain, count, error_type=type(exc).__name__)
        if strict:
            raise StateReadComparisonError(
                f"SQLite shadow comparison failed for {domain}."
            ) from exc
        return value

    if shadow != value:
        count = _increment(domain, "shadow_mismatches")
        _emit_shadow_event("mismatch", domain, count)
        if strict:
            raise StateReadComparisonError(
                f"Legacy/SQLite shadow comparison mismatch for {domain}."
            )
        return value

    count = _increment(domain, "shadow_matches")
    report_every = _shadow_report_every()
    if count == 1 or count % report_every == 0:
        _emit_shadow_event("match", domain, count)
    return value


def read_users(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read("users", legacy_loader, export_users)


def read_user_data(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read("user_data", legacy_loader, export_user_data)


def read_collections(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read("collections", legacy_loader, export_collections)


def read_user_uploads(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read("user_uploads", legacy_loader, export_user_uploads)


def read_custom_meta(legacy_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return _read("custom_meta", legacy_loader, export_custom_meta)


def read_allowed_emails(legacy_loader: Callable[[], set[str]]) -> set[str]:
    return _read(
        "allowed_emails",
        legacy_loader,
        lambda conn: set(export_allowed_emails(conn)),
    )
