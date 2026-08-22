#!/usr/bin/env python3
"""Measure local SQLite job admission latency without user payloads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.jobs import JobStore


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=200)
    args = parser.parse_args()
    if args.repetitions < 20:
        parser.error("--repetitions must be at least 20 for percentile output.")

    durations: list[float] = []
    with tempfile.TemporaryDirectory() as temporary:
        store = JobStore(Path(temporary) / "jobs.sqlite3")
        for index in range(args.repetitions):
            started = time.perf_counter()
            job, created = store.enqueue(
                kind="epub_package",
                owner_email="benchmark@example.test",
                payload={"session_id": f"{index:032x}"},
                dedupe_key=f"benchmark:{index}",
                max_attempts=3,
                timeout_seconds=900,
                retention_seconds=3600,
            )
            if not created or store.get(job.job_id) is None:
                raise RuntimeError("Queue admission verification failed.")
            durations.append((time.perf_counter() - started) * 1000)

    result = {
        "operation": "sqlite_wal_enqueue_and_owner_status_read",
        "repetitions": args.repetitions,
        "duration_ms": {
            "p50": round(statistics.median(durations), 3),
            "p95": round(percentile(durations, 0.95), 3),
            "p99": round(percentile(durations, 0.99), 3),
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
