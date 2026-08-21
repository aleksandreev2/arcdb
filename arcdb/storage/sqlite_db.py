from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_documents (
    name TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    imported_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY COLLATE NOCASE,
    pwd_hash TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at REAL,
    code_hash TEXT,
    code_expires REAL,
    code_attempts INTEGER,
    reset_code_hash TEXT,
    reset_code_expires REAL,
    reset_code_attempts INTEGER,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_state_users (
    user_email TEXT PRIMARY KEY COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS user_novel_state (
    user_email TEXT NOT NULL COLLATE NOCASE,
    novel_key TEXT NOT NULL,
    status TEXT,
    progress INTEGER,
    last_read REAL,
    download_count INTEGER,
    last_download REAL,
    hidden INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (user_email, novel_key)
);

CREATE TABLE IF NOT EXISTS collection_users (
    user_email TEXT PRIMARY KEY COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS collections (
    user_email TEXT NOT NULL COLLATE NOCASE,
    collection_id TEXT NOT NULL,
    name TEXT,
    sort_order INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (user_email, collection_id)
);

CREATE TABLE IF NOT EXISTS collection_items (
    user_email TEXT NOT NULL COLLATE NOCASE,
    collection_id TEXT NOT NULL,
    novel_key TEXT NOT NULL,
    PRIMARY KEY (user_email, collection_id, novel_key)
);

CREATE TABLE IF NOT EXISTS user_uploads (
    upload_id TEXT PRIMARY KEY,
    uploader_email TEXT COLLATE NOCASE,
    approved INTEGER NOT NULL DEFAULT 0,
    upload_date TEXT,
    title TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_metadata (
    filename TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allowed_emails (
    email TEXT PRIMARY KEY COLLATE NOCASE
);

CREATE INDEX IF NOT EXISTS idx_user_state_status
    ON user_novel_state(user_email, status);
CREATE INDEX IF NOT EXISTS idx_user_state_last_read
    ON user_novel_state(user_email, last_read DESC);
CREATE INDEX IF NOT EXISTS idx_user_state_novel
    ON user_novel_state(novel_key);
CREATE INDEX IF NOT EXISTS idx_collection_items_novel
    ON collection_items(user_email, novel_key);
CREATE INDEX IF NOT EXISTS idx_user_uploads_approved
    ON user_uploads(approved, upload_date);
"""


def connect_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    has_schema_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if has_schema_meta is not None:
        existing = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if existing is not None and str(existing[0]) != str(SCHEMA_VERSION):
            raise RuntimeError(
                "Refusing an in-place SQLite schema version change from "
                f"{existing[0]} to {SCHEMA_VERSION}; rebuild a verified candidate "
                "from authoritative legacy state."
            )
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
