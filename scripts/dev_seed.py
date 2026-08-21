from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash


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

    previous = users.get(email) if isinstance(users.get(email), dict) else {}
    users[email] = {
        **previous,
        "pwd_hash": generate_password_hash(password),
        "verified": True,
        "created_at": previous.get("created_at", 0),
        "dev_managed": True,
    }
    atomic_json_write(users_path, users)

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

    print(f"[dev] Local login: {email} / {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
