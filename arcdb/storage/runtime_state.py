from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .sqlite_db import SCHEMA_VERSION, connect_db

ROOT = Path(__file__).resolve().parents[2]


class ShadowStateError(RuntimeError):
    pass


def _enabled() -> bool:
    return os.environ.get("STATE_DUAL_WRITE", "0") == "1"


def _verify_enabled() -> bool:
    return os.environ.get("STATE_DUAL_WRITE_VERIFY", "1") == "1"


def _db_path() -> Path:
    raw = os.environ.get("SQLITE_DB_PATH", "./data/arcdb.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_fields(raw: Any) -> tuple[Any, ...]:
    record = raw if isinstance(raw, dict) else {"_legacy_value": raw}
    return (
        record.get("status"),
        _int_or_none(record.get("progress")),
        _float_or_none(record.get("last_read")),
        _int_or_none(record.get("dl")),
        _float_or_none(record.get("last_dl")),
        1 if record.get("hidden") else 0,
        _payload(raw),
    )


def _memberships(raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return []
    values = raw.get("collections") or []
    if not isinstance(values, list):
        return []
    return sorted({str(value) for value in values if value is not None})


def _validated_collections(raw_collections: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_collections, list):
        raise ShadowStateError("Dual-write expects a legacy collection bucket to be a list.")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_collections):
        if not isinstance(raw, dict) or raw.get("id") is None:
            raise ShadowStateError(
                f"Legacy collection at position {position} is not an object with an id."
            )
        collection_id = str(raw["id"])
        if collection_id in seen:
            raise ShadowStateError(f"Duplicate legacy collection id: {collection_id}")
        seen.add(collection_id)
        result.append(raw)
    return result


def _assert_ready(conn) -> None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise ShadowStateError("SQLite shadow database has no schema_version; run the safe migration first.")
    if str(row[0]) != str(SCHEMA_VERSION):
        raise ShadowStateError(
            f"SQLite shadow schema mismatch: expected {SCHEMA_VERSION}, got {row[0]}."
        )


def _sync_one(conn, user_email: str, novel_key: str, raw: Any | None) -> None:
    if raw is None:
        conn.execute(
            "DELETE FROM collection_items WHERE user_email=? AND novel_key=?",
            (user_email, novel_key),
        )
        conn.execute(
            "DELETE FROM user_novel_state WHERE user_email=? AND novel_key=?",
            (user_email, novel_key),
        )
        return

    fields = _record_fields(raw)
    conn.execute(
        """
        INSERT INTO user_novel_state(
            user_email, novel_key, status, progress, last_read,
            download_count, last_download, hidden, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_email, novel_key) DO UPDATE SET
            status=excluded.status,
            progress=excluded.progress,
            last_read=excluded.last_read,
            download_count=excluded.download_count,
            last_download=excluded.last_download,
            hidden=excluded.hidden,
            payload_json=excluded.payload_json
        """,
        (user_email, novel_key, *fields),
    )
    conn.execute(
        "DELETE FROM collection_items WHERE user_email=? AND novel_key=?",
        (user_email, novel_key),
    )
    memberships = _memberships(raw)
    if memberships:
        conn.executemany(
            "INSERT INTO collection_items(user_email, collection_id, novel_key) VALUES (?, ?, ?)",
            [(user_email, collection_id, novel_key) for collection_id in memberships],
        )


def _verify_one(conn, user_email: str, novel_key: str, expected: Any | None) -> None:
    row = conn.execute(
        """
        SELECT status, progress, last_read, download_count, last_download, hidden, payload_json
        FROM user_novel_state
        WHERE user_email=? AND novel_key=?
        """,
        (user_email, novel_key),
    ).fetchone()

    if expected is None:
        if row is not None:
            raise ShadowStateError(f"Deleted legacy state still exists in SQLite for {user_email}/{novel_key}.")
        memberships = conn.execute(
            "SELECT collection_id FROM collection_items WHERE user_email=? AND novel_key=?",
            (user_email, novel_key),
        ).fetchall()
        if memberships:
            raise ShadowStateError(
                f"Deleted legacy memberships still exist in SQLite for {user_email}/{novel_key}."
            )
        return

    if row is None:
        raise ShadowStateError(f"SQLite state row is missing for {user_email}/{novel_key}.")

    expected_fields = _record_fields(expected)
    actual_payload = json.loads(row["payload_json"])
    actual_fields = (
        row["status"],
        row["progress"],
        row["last_read"],
        row["download_count"],
        row["last_download"],
        row["hidden"],
        _payload(actual_payload),
    )
    if actual_fields != expected_fields:
        raise ShadowStateError(
            f"SQLite state mismatch for {user_email}/{novel_key}: normalized fields or payload differ."
        )

    actual_memberships = sorted(
        row["collection_id"]
        for row in conn.execute(
            "SELECT collection_id FROM collection_items WHERE user_email=? AND novel_key=? ORDER BY collection_id",
            (user_email, novel_key),
        )
    )
    if actual_memberships != _memberships(expected):
        raise ShadowStateError(
            f"SQLite collection membership mismatch for {user_email}/{novel_key}."
        )


def _sync_collection_user(
    conn, user_email: str, raw_collections: list[dict[str, Any]]
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO collection_users(user_email) VALUES (?)",
        (user_email,),
    )
    conn.execute("DELETE FROM collections WHERE user_email=?", (user_email,))
    if raw_collections:
        conn.executemany(
            """
            INSERT INTO collections(
                user_email, collection_id, name, sort_order, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    user_email,
                    str(raw["id"]),
                    raw.get("name"),
                    position,
                    _payload(raw),
                )
                for position, raw in enumerate(raw_collections)
            ],
        )


def _verify_collection_user(
    conn, user_email: str, expected: list[dict[str, Any]]
) -> None:
    container = conn.execute(
        "SELECT 1 FROM collection_users WHERE user_email=?", (user_email,)
    ).fetchone()
    if container is None:
        raise ShadowStateError(f"SQLite collection container is missing for {user_email}.")

    rows = conn.execute(
        """
        SELECT collection_id, name, sort_order, payload_json
        FROM collections
        WHERE user_email=?
        ORDER BY sort_order
        """,
        (user_email,),
    ).fetchall()
    actual = [json.loads(row["payload_json"]) for row in rows]
    if actual != expected:
        raise ShadowStateError(f"SQLite collection payload/order mismatch for {user_email}.")
    normalized = [
        (row["collection_id"], row["name"], row["sort_order"])
        for row in rows
    ]
    expected_normalized = [
        (str(raw["id"]), raw.get("name"), position)
        for position, raw in enumerate(expected)
    ]
    if normalized != expected_normalized:
        raise ShadowStateError(f"SQLite normalized collection fields mismatch for {user_email}.")


def mirror_user_changes(
    user_email: str,
    before_user: dict[str, Any],
    after_user: dict[str, Any],
    *,
    reason: str,
) -> list[str]:
    """Mirror only changed per-novel records after the legacy JSON write succeeds."""
    if not _enabled():
        return []
    if not isinstance(before_user, dict) or not isinstance(after_user, dict):
        raise ShadowStateError("Dual-write expects per-user legacy state to be dictionaries.")

    changed = sorted(
        key
        for key in set(before_user) | set(after_user)
        if before_user.get(key) != after_user.get(key)
    )
    if not changed:
        return []

    db_path = _db_path()
    if not db_path.is_file():
        raise ShadowStateError(
            f"SQLite shadow database is missing at {db_path}; run scripts/migrate_state_to_sqlite.py first."
        )

    conn = connect_db(db_path)
    try:
        _assert_ready(conn)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_state_users(user_email) VALUES (?)",
                (str(user_email),),
            )
            for novel_key in changed:
                _sync_one(conn, str(user_email), str(novel_key), after_user.get(novel_key))
        if _verify_enabled():
            for novel_key in changed:
                _verify_one(conn, str(user_email), str(novel_key), after_user.get(novel_key))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if os.environ.get("STATE_DUAL_WRITE_LOG_SUCCESS", "0") == "1":
        print(
            f"[STATE-DUAL-WRITE] {reason}: mirrored {len(changed)} record(s) for {user_email}."
        )
    return changed


def mirror_collection_user(
    user_email: str,
    raw_collections: Any,
    *,
    reason: str,
) -> int:
    """Replace one user's collection metadata after collections.json is durable."""
    if not _enabled():
        return 0
    validated = _validated_collections(raw_collections)

    db_path = _db_path()
    if not db_path.is_file():
        raise ShadowStateError(
            f"SQLite shadow database is missing at {db_path}; run scripts/migrate_state_to_sqlite.py first."
        )

    conn = connect_db(db_path)
    try:
        _assert_ready(conn)
        with conn:
            _sync_collection_user(conn, str(user_email), validated)
        if _verify_enabled():
            _verify_collection_user(conn, str(user_email), validated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if os.environ.get("STATE_DUAL_WRITE_LOG_SUCCESS", "0") == "1":
        print(
            f"[STATE-DUAL-WRITE] {reason}: mirrored {len(validated)} collection(s) "
            f"for {user_email}."
        )
    return len(validated)
