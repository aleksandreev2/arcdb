from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.state_parity import (  # noqa: E402
    verify_collections_parity,
    verify_metadata_domains_parity,
    verify_user_data_parity,
    verify_users_parity,
)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def resolve_path(raw: str | None, fallback: Path) -> Path:
    path = Path(raw) if raw else fallback
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def main() -> int:
    env = os.environ.copy()
    env.update(parse_env(ROOT / ".env"))
    meta_dir = resolve_path(env.get("META_DIR"), ROOT / "data" / "metadata")
    users_path = resolve_path(env.get("USERS_PATH"), meta_dir / "users.json")
    user_data_path = resolve_path(env.get("USER_DATA_PATH"), meta_dir / "user_data.json")
    collections_path = resolve_path(
        env.get("COLLECTIONS_PATH"), meta_dir / "collections.json"
    )
    user_uploads_path = resolve_path(
        env.get("USER_UPLOADS_PATH"), meta_dir / "user_uploads.json"
    )
    custom_meta_path = resolve_path(
        env.get("CUSTOM_META_PATH"), meta_dir / "custom_meta.json"
    )
    allowed_emails_path = resolve_path(
        env.get("ALLOWED_EMAILS_PATH"), meta_dir / "allowed_gmails.txt"
    )
    db_path = resolve_path(env.get("SQLITE_DB_PATH"), ROOT / "data" / "arcdb.sqlite3")

    auth_counts = verify_users_parity(users_path=users_path, db_path=db_path)
    user_counts = verify_user_data_parity(user_data_path=user_data_path, db_path=db_path)
    collection_counts = verify_collections_parity(
        collections_path=collections_path, db_path=db_path
    )
    metadata_counts = verify_metadata_domains_parity(
        user_uploads_path=user_uploads_path,
        custom_meta_path=custom_meta_path,
        allowed_emails_path=allowed_emails_path,
        db_path=db_path,
    )
    print(
        "State parity: OK "
        f"({auth_counts['users']} auth users; "
        f"{user_counts['users']} user-state containers, "
        f"{user_counts['records']} per-novel records; "
        f"{user_counts['memberships']} memberships; "
        f"{collection_counts['users']} collection containers, "
        f"{collection_counts['collections']} collections; "
        f"{metadata_counts['uploads']} uploads, "
        f"{metadata_counts['custom_metadata']} custom metadata entries, "
        f"{metadata_counts['allowed_emails']} allowed emails)"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"State parity: FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
