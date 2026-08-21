from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.state_parity import verify_user_data_parity  # noqa: E402


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
    user_data_path = resolve_path(env.get("USER_DATA_PATH"), meta_dir / "user_data.json")
    db_path = resolve_path(env.get("SQLITE_DB_PATH"), ROOT / "data" / "arcdb.sqlite3")

    counts = verify_user_data_parity(user_data_path=user_data_path, db_path=db_path)
    print(
        f"User state parity: OK ({counts['users']} users, {counts['records']} per-novel records)"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"User state parity: FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
