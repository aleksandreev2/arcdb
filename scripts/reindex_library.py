"""Explicitly rebuild or verify the persistent ArchiveDB library index."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_local_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()

from arcdb.app import LIBRARY_INDEX, rebuild_library_index  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a candidate library index, validate it, and publish atomically."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Validate the active index without scanning content or changing files.",
    )
    args = parser.parse_args()
    if args.verify_only:
        verified = LIBRARY_INDEX.verify()
        report = {
            "status": "verified",
            "items": int(verified["items"]),
            "chapters": int(verified["chapters"]),
            "images": int(verified["images"]),
            "fts5_trigram": verified.get("fts5_trigram") == "1",
            "generation_id": verified.get("generation_id"),
        }
    else:
        report = rebuild_library_index()
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
