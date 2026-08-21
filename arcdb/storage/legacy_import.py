from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(payload: str) -> Any:
    return json.loads(payload)


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


def replace_from_documents(
    conn: sqlite3.Connection,
    *,
    users: dict[str, Any],
    user_data: dict[str, Any],
    collections: dict[str, Any],
    user_uploads: dict[str, Any],
    custom_meta: dict[str, Any],
    allowed_emails: list[str],
) -> dict[str, int]:
    now = time.time()
    snapshots = {
        "users.json": users,
        "user_data.json": user_data,
        "collections.json": collections,
        "user_uploads.json": user_uploads,
        "custom_meta.json": custom_meta,
        "allowed_gmails.txt": allowed_emails,
    }

    with conn:
        for table in (
            "collection_items",
            "collections",
            "collection_users",
            "user_novel_state",
            "user_state_users",
            "users",
            "user_uploads",
            "custom_metadata",
            "allowed_emails",
            "legacy_documents",
        ):
            conn.execute(f"DELETE FROM {table}")

        conn.executemany(
            "INSERT INTO legacy_documents(name, payload_json, imported_at) VALUES(?, ?, ?)",
            [(name, _dump(value), now) for name, value in snapshots.items()],
        )

        for email, raw in users.items():
            record = raw if isinstance(raw, dict) else {"_legacy_value": raw}
            conn.execute(
                """
                INSERT INTO users(
                    email, pwd_hash, verified, created_at,
                    code_hash, code_expires, code_attempts,
                    reset_code_hash, reset_code_expires, reset_code_attempts,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(email),
                    record.get("pwd_hash"),
                    1 if record.get("verified") else 0,
                    _float_or_none(record.get("created_at")),
                    record.get("code_hash"),
                    _float_or_none(record.get("code_expires")),
                    _int_or_none(record.get("code_attempts")),
                    record.get("reset_code_hash"),
                    _float_or_none(record.get("reset_code_expires")),
                    _int_or_none(record.get("reset_code_attempts")),
                    _dump(raw),
                ),
            )

        for email, raw_states in user_data.items():
            if isinstance(raw_states, dict):
                conn.execute(
                    "INSERT OR IGNORE INTO user_state_users(user_email) VALUES (?)",
                    (str(email),),
                )
            else:
                continue
            for novel_key, raw in raw_states.items():
                record = raw if isinstance(raw, dict) else {"_legacy_value": raw}
                conn.execute(
                    """
                    INSERT INTO user_novel_state(
                        user_email, novel_key, status, progress, last_read,
                        download_count, last_download, hidden, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(email),
                        str(novel_key),
                        record.get("status"),
                        _int_or_none(record.get("progress")),
                        _float_or_none(record.get("last_read")),
                        _int_or_none(record.get("dl")),
                        _float_or_none(record.get("last_dl")),
                        1 if record.get("hidden") else 0,
                        _dump(raw),
                    ),
                )
                for collection_id in record.get("collections") or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO collection_items(user_email, collection_id, novel_key) "
                        "VALUES (?, ?, ?)",
                        (str(email), str(collection_id), str(novel_key)),
                    )

        for email, raw_collections in collections.items():
            if not isinstance(raw_collections, list):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO collection_users(user_email) VALUES (?)",
                (str(email),),
            )
            for position, raw in enumerate(raw_collections):
                if not isinstance(raw, dict) or raw.get("id") is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO collections(user_email, collection_id, name, sort_order, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(email),
                        str(raw.get("id")),
                        raw.get("name"),
                        position,
                        _dump(raw),
                    ),
                )

        for upload_id, raw in user_uploads.items():
            record = raw if isinstance(raw, dict) else {"_legacy_value": raw}
            conn.execute(
                """
                INSERT INTO user_uploads(upload_id, uploader_email, approved, upload_date, title, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(upload_id),
                    record.get("uploader_email"),
                    1 if record.get("approved") else 0,
                    record.get("upload_date"),
                    record.get("title_en") or record.get("raw_title") or record.get("title"),
                    _dump(raw),
                ),
            )

        for filename, raw in custom_meta.items():
            conn.execute(
                "INSERT INTO custom_metadata(filename, payload_json) VALUES (?, ?)",
                (str(filename), _dump(raw)),
            )

        conn.executemany(
            "INSERT OR IGNORE INTO allowed_emails(email) VALUES (?)",
            [(str(email).strip().lower(),) for email in allowed_emails if str(email).strip()],
        )

    return state_counts(conn)


def state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "users",
        "user_state_users",
        "user_novel_state",
        "collection_users",
        "collections",
        "collection_items",
        "user_uploads",
        "custom_metadata",
        "allowed_emails",
    )
    return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def export_users(conn: sqlite3.Connection) -> dict[str, Any]:
    return {row["email"]: _load(row["payload_json"]) for row in conn.execute("SELECT email, payload_json FROM users")}


def export_user_data(conn: sqlite3.Connection) -> dict[str, Any]:
    source = snapshot_document(conn, "user_data.json")
    result: dict[str, Any] = {}
    if isinstance(source, dict):
        for email, raw in source.items():
            result[str(email)] = {} if isinstance(raw, dict) else raw
    for row in conn.execute("SELECT user_email FROM user_state_users ORDER BY user_email"):
        result.setdefault(row["user_email"], {})
    for row in conn.execute(
        "SELECT user_email, novel_key, payload_json FROM user_novel_state ORDER BY user_email, novel_key"
    ):
        bucket = result.setdefault(row["user_email"], {})
        if isinstance(bucket, dict):
            bucket[row["novel_key"]] = _load(row["payload_json"])
    return result


def export_collections(conn: sqlite3.Connection) -> dict[str, Any]:
    source = snapshot_document(conn, "collections.json")
    result: dict[str, Any] = {}
    if isinstance(source, dict):
        for email, raw in source.items():
            if not isinstance(raw, list):
                result[str(email)] = raw
    for row in conn.execute("SELECT user_email FROM collection_users ORDER BY user_email"):
        result.setdefault(row["user_email"], [])
    for row in conn.execute(
        "SELECT user_email, payload_json FROM collections ORDER BY user_email, sort_order"
    ):
        bucket = result.setdefault(row["user_email"], [])
        if isinstance(bucket, list):
            bucket.append(_load(row["payload_json"]))
    return result


def export_user_uploads(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        row["upload_id"]: _load(row["payload_json"])
        for row in conn.execute("SELECT upload_id, payload_json FROM user_uploads")
    }


def export_custom_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        row["filename"]: _load(row["payload_json"])
        for row in conn.execute("SELECT filename, payload_json FROM custom_metadata")
    }


def export_allowed_emails(conn: sqlite3.Connection) -> list[str]:
    return [row["email"] for row in conn.execute("SELECT email FROM allowed_emails ORDER BY email")]


def snapshot_document(conn: sqlite3.Connection, name: str) -> Any:
    row = conn.execute("SELECT payload_json FROM legacy_documents WHERE name=?", (name,)).fetchone()
    if row is None:
        raise KeyError(name)
    return _load(row["payload_json"])
