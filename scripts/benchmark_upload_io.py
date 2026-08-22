#!/usr/bin/env python3
"""Compare removed per-chunk fsync uploads with the current atomic copy path."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.epub_io import COPY_CHUNK_BYTES, copy_upload_limited  # noqa: E402


MIB = 1024 * 1024


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def legacy_per_chunk_fsync(source, destination: Path, max_bytes: int) -> int:
    total = 0
    with destination.open("wb") as output:
        while True:
            chunk = source.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("fixture exceeded its benchmark limit")
            output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    return total


def measure(operation, repetitions: int) -> dict[str, float]:
    durations = []
    for _ in range(repetitions):
        started = time.perf_counter()
        operation()
        durations.append((time.perf_counter() - started) * 1000.0)
    return {
        "p50": round(statistics.median(durations), 3),
        "p95": round(percentile(durations, 0.95), 3),
        "p99": round(percentile(durations, 0.99), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-mib", type=int, default=16)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.payload_mib <= 0 or args.repetitions < 3:
        parser.error("--payload-mib must be positive and --repetitions at least 3")

    payload_bytes = args.payload_mib * MIB
    max_bytes = payload_bytes + MIB
    with tempfile.TemporaryDirectory(prefix="arcdb-upload-benchmark-") as temporary:
        root = Path(temporary)
        source_path = root / "source.bin"
        with source_path.open("wb") as source:
            remaining = payload_bytes
            block = b"ArchiveDB benchmark\0" * 4096
            while remaining:
                chunk = block[: min(len(block), remaining)]
                source.write(chunk)
                remaining -= len(chunk)

        def legacy_operation() -> None:
            with source_path.open("rb") as source:
                legacy_per_chunk_fsync(source, root / "legacy.bin", max_bytes)

        def atomic_operation() -> None:
            with source_path.open("rb") as source:
                copy_upload_limited(source, root / "atomic.bin", max_bytes)

        chunks = (payload_bytes + COPY_CHUNK_BYTES - 1) // COPY_CHUNK_BYTES
        result = {
            "payload_mib": args.payload_mib,
            "repetitions": args.repetitions,
            "legacy_per_chunk_fsync": {
                "fsync_calls_per_copy": chunks,
                "duration_ms": measure(legacy_operation, args.repetitions),
            },
            "atomic_single_fsync": {
                "fsync_calls_per_copy": 1,
                "duration_ms": measure(atomic_operation, args.repetitions),
            },
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
