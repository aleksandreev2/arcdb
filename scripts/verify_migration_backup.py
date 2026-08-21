from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.safe_migration import file_fingerprint  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an ArchiveDB migration backup manifest and copied files.")
    parser.add_argument("backup_dir", help="Timestamped migration backup directory containing manifest.json")
    parser.add_argument(
        "--check-current-sources",
        action="store_true",
        help="Also verify current source files still match their pre-migration hashes.",
    )
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir).expanduser().resolve()
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    failures: list[str] = []
    entries = manifest.get("files") or []
    if not entries:
        failures.append("manifest contains no backed-up files")

    for entry in entries:
        backup = Path(entry["backup"])
        fp = file_fingerprint(backup)
        if not fp.get("exists"):
            failures.append(f"missing backup: {backup}")
            continue
        if fp.get("size") != entry.get("size"):
            failures.append(f"size mismatch: {backup}")
        if fp.get("sha256") != entry.get("sha256"):
            failures.append(f"sha256 mismatch: {backup}")

        if args.check_current_sources:
            source = Path(entry["source"])
            source_fp = file_fingerprint(source)
            if not source_fp.get("exists"):
                failures.append(f"current source missing: {source}")
            elif source_fp.get("size") != entry.get("size") or source_fp.get("sha256") != entry.get("sha256"):
                failures.append(f"current source changed: {source}")

    if failures:
        print("Migration backup verification FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 2

    print(f"Migration backup verification OK: {backup_dir}")
    print(f"Verified files: {len(entries)}")
    if args.check_current_sources:
        print("Current legacy sources still match the snapshot: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
