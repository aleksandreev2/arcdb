from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.legacy_import import (  # noqa: E402
    export_collections,
    export_custom_meta,
    export_user_data,
    export_user_uploads,
    export_users,
    replace_from_documents,
)
from arcdb.storage.sqlite_db import connect_db, initialize_schema  # noqa: E402


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        result[key.strip()] = value
    return result


def resolve_path(raw: str | None, fallback: Path) -> Path:
    path = Path(raw) if raw else fallback
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_allowed(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Import ArchiveDB legacy JSON state into SQLite WAL storage.")
    parser.add_argument("--db", help="SQLite path. Defaults to SQLITE_DB_PATH or ./data/arcdb.sqlite3")
    parser.add_argument("--verify", action="store_true", help="Verify normalized SQLite exports against JSON inputs.")
    args = parser.parse_args()

    env = os.environ.copy()
    env.update(parse_env(ROOT / ".env"))
    meta_dir = resolve_path(env.get("META_DIR"), ROOT / "data" / "metadata")
    db_path = resolve_path(args.db or env.get("SQLITE_DB_PATH"), ROOT / "data" / "arcdb.sqlite3")

    users_path = resolve_path(env.get("USERS_PATH"), meta_dir / "users.json")
    uploads_path = resolve_path(env.get("USER_UPLOADS_PATH"), meta_dir / "user_uploads.json")
    allowed_path = resolve_path(env.get("ALLOWED_EMAILS_PATH"), meta_dir / "allowed_gmails.txt")

    docs = {
        "users": read_json(users_path, {}),
        "user_data": read_json(meta_dir / "user_data.json", {}),
        "collections": read_json(meta_dir / "collections.json", {}),
        "user_uploads": read_json(uploads_path, {}),
        "custom_meta": read_json(meta_dir / "custom_meta.json", {}),
        "allowed_emails": read_allowed(allowed_path),
    }

    conn = connect_db(db_path)
    try:
        initialize_schema(conn)
        counts = replace_from_documents(conn, **docs)
        if args.verify:
            checks = {
                "users": export_users(conn) == docs["users"],
                "user_data": export_user_data(conn) == docs["user_data"],
                "collections": export_collections(conn) == docs["collections"],
                "user_uploads": export_user_uploads(conn) == docs["user_uploads"],
                "custom_meta": export_custom_meta(conn) == docs["custom_meta"],
            }
            failed = [name for name, ok in checks.items() if not ok]
            if failed:
                raise RuntimeError("SQLite verification failed for: " + ", ".join(failed))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    print(f"SQLite state database: {db_path}")
    print(f"journal_mode={mode}")
    print("Imported rows:")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    if args.verify:
        print("Verification: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
