from __future__ import annotations

import argparse
import base64
import hashlib
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = ROOT / "baseline"
PARTS_DIR = BASELINE_DIR / "parts"
MANIFEST = BASELINE_DIR / "manifest.txt"
DEFAULT_TARGET = ROOT / ".runtime" / "source"


def read_manifest() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        if "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def bundle_bytes(manifest: dict[str, str]) -> bytes:
    filenames = [name.strip() for name in manifest["files"].split(",") if name.strip()]
    if not filenames:
        raise RuntimeError("Baseline manifest contains no bundle parts.")

    chunks: list[str] = []
    for name in filenames:
        path = PARTS_DIR / name
        if not path.is_file():
            raise RuntimeError(f"Baseline bundle part is missing: {name}")
        chunks.append(path.read_text(encoding="ascii").strip())
    return base64.b64decode("".join(chunks), validate=True)


def materialize(target: Path, force: bool = False) -> Path:
    manifest = read_manifest()
    expected = manifest["sha256"]
    expected_size = int(manifest["size"])

    marker = target / ".baseline.sha256"
    if not force and marker.exists() and marker.read_text(encoding="ascii").strip() == expected:
        return target

    payload = bundle_bytes(manifest)
    if len(payload) != expected_size:
        raise RuntimeError(
            f"Baseline bundle size mismatch: expected {expected_size}, got {len(payload)}"
        )

    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise RuntimeError(f"Baseline bundle checksum mismatch: expected {expected}, got {actual}")

    archive = ROOT / ".runtime" / "arcdb_baseline.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(payload)

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(target)
    marker.write_text(expected + "\n", encoding="ascii")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify and extract the ArchiveDB source baseline.")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    target = args.target if args.target.is_absolute() else ROOT / args.target
    materialize(target, force=args.force)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
