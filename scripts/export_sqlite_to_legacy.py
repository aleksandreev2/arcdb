from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.legacy_import import (  # noqa: E402
    export_allowed_emails,
    export_collections,
    export_custom_meta,
    export_user_data,
    export_user_uploads,
    export_users,
)


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export ArchiveDB SQLite state into a new legacy-format directory without overwriting files."
    )
    parser.add_argument("--db", required=True, help="SQLite database to export")
    parser.add_argument("--output-dir", required=True, help="New directory to create")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    output = Path(args.output_dir).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing export directory: {output}")
    output.mkdir(parents=True, exist_ok=False)

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        docs = {
            "users.json": export_users(conn),
            "user_data.json": export_user_data(conn),
            "collections.json": export_collections(conn),
            "user_uploads.json": export_user_uploads(conn),
            "custom_meta.json": export_custom_meta(conn),
        }
        allowed = export_allowed_emails(conn)
    finally:
        conn.close()

    for name, data in docs.items():
        write_json(output / name, data)
    (output / "allowed_gmails.txt").write_text(
        "".join(f"{email}\n" for email in allowed), encoding="utf-8"
    )

    # Read back the generated files to ensure they are valid before reporting success.
    for name, expected in docs.items():
        actual = json.loads((output / name).read_text(encoding="utf-8"))
        if actual != expected:
            raise RuntimeError(f"Export read-back verification failed: {name}")
    actual_allowed = [
        line.strip()
        for line in (output / "allowed_gmails.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if actual_allowed != allowed:
        raise RuntimeError("Export read-back verification failed: allowed_gmails.txt")

    files = []
    for path in sorted(output.iterdir()):
        if path.is_file():
            files.append({"name": path.name, "size": path.stat().st_size, "sha256": sha256(path)})
    manifest = {
        "format": 1,
        "created_at_unix": time.time(),
        "source_db": str(db_path),
        "source_db_sha256": sha256(db_path),
        "files": files,
    }
    write_json(output / "manifest.json", manifest)

    print(f"Legacy-format export created: {output}")
    print("No existing legacy files were overwritten.")
    print("Read-back verification: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
