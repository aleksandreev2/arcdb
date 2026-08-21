from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from arcdb.storage.sqlite_db import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def ready_shadow_path() -> Path | None:
    if os.environ.get("STATE_DUAL_WRITE", "0") != "1":
        return None
    raw = os.environ.get("SQLITE_DB_PATH", "./data/arcdb.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return path if row is not None and str(row[0]) == str(SCHEMA_VERSION) else None


def main() -> int:
    if os.environ.get("ARCHIVEDB_LOCAL_DEV") != "1":
        return 0

    email = os.environ.get("LOCAL_DEV_EMAIL", "").strip().lower()
    password = os.environ.get("LOCAL_DEV_PASSWORD", "")
    if not email or not password:
        print("[dev] LOCAL_DEV_EMAIL/PASSWORD not set; skipping local account seed.")
        return 0

    meta_dir = Path(os.environ.get("META_DIR", "./data/metadata"))
    users_path = Path(os.environ.get("USERS_PATH", str(meta_dir / "users.json")))
    allowed_path = Path(os.environ.get("ALLOWED_EMAILS_PATH", str(meta_dir / "allowed_gmails.txt")))

    users: dict = {}
    if users_path.exists():
        try:
            loaded = json.loads(users_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                users = loaded
        except (OSError, json.JSONDecodeError):
            pass

    before_users = json.loads(json.dumps(users))
    previous = users.get(email) if isinstance(users.get(email), dict) else {}
    previous_hash = str(previous.get("pwd_hash") or "")
    try:
        password_matches = bool(previous_hash) and check_password_hash(
            previous_hash, password
        )
    except (TypeError, ValueError):
        password_matches = False
    users[email] = {
        **previous,
        "pwd_hash": (
            previous_hash if password_matches else generate_password_hash(password)
        ),
        "verified": True,
        "created_at": previous.get("created_at", 0),
        "dev_managed": True,
    }
    if users != before_users:
        atomic_json_write(users_path, users)

    shadow_path = ready_shadow_path()
    if shadow_path is not None and users != before_users:
        from arcdb.storage.runtime_state import mirror_auth_users_changes

        mirror_auth_users_changes(before_users, users, reason="dev_account_seed")

    allowed_path.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if allowed_path.exists():
        existing = {
            line.strip().lower()
            for line in allowed_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    if email not in existing:
        with allowed_path.open("a", encoding="utf-8") as fh:
            if allowed_path.stat().st_size:
                fh.write("\n")
            fh.write(email + "\n")

        if shadow_path is not None:
            from arcdb.storage.runtime_state import mirror_allowed_emails

            mirror_allowed_emails(existing | {email}, reason="dev_allowlist_seed")

    print(f"[dev] Local login account ready: {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
