"""Measure persistent library-index rebuild and query latency with synthetic data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import tempfile
import time

from arcdb.library_index import LibraryIndex


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "p99_ms": round(_percentile(values, 0.99), 3),
    }


def _items(count: int) -> list[dict]:
    return [
        {
            "id": str(number),
            "filename": f"novel-{number:06d}.epub",
            "_library_key": f"synthetic:{number}",
            "title_en": f"Synthetic Novel {number:06d}",
            "title_kr": f"테스트 {number:06d}",
            "author": f"Author {number % 100:03d}",
            "language": "ko" if number % 2 else "en",
            "tags": ["Synthetic", f"Group-{number % 20:02d}"],
            "chapters": number % 500,
            "views": count - number,
            "likes": number % 1000,
            "complete": number % 2,
            "upload_date": f"2026-{number % 12 + 1:02d}-{number % 28 + 1:02d}",
            "tg_link": "https://example.invalid/fixture",
        }
        for number in range(count)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=10_000)
    parser.add_argument("--repetitions", type=int, default=200)
    args = parser.parse_args()
    if args.items < 100 or args.repetitions < 5:
        parser.error("--items must be >= 100 and --repetitions must be >= 5")

    items = _items(args.items)
    filters = {
        "upload_source": "all",
        "search": "Synthetic Novel 0001",
        "includes": {"synthetic"},
        "excludes": set(),
        "reading_status": "all",
        "translated_chapter": "all",
        "audience": "all",
        "status": "all",
        "author": "",
        "language": "all",
        "min_chapters": 0,
        "max_chapters": 999999,
        "tag_match": "and",
        "collection": "all",
        "updated_after": "",
        "updated_before": "",
    }
    with tempfile.TemporaryDirectory(prefix="arcdb-library-benchmark-") as temporary:
        index = LibraryIndex(Path(temporary) / "library.sqlite3")
        started = time.perf_counter()
        report = index.rebuild(items)
        rebuild_ms = (time.perf_counter() - started) * 1000
        index.query(
            filters=filters,
            user_data={},
            sort_by="views",
            sort_order="desc",
            page=1,
            limit=30,
        )
        measurements = []
        for _ in range(args.repetitions):
            started = time.perf_counter()
            result = index.query(
                filters=filters,
                user_data={},
                sort_by="views",
                sort_order="desc",
                page=1,
                limit=30,
            )
            measurements.append((time.perf_counter() - started) * 1000)
        output = {
            "items": args.items,
            "repetitions": args.repetitions,
            "rebuild_ms": round(rebuild_ms, 3),
            "matched_items": result["total"],
            "fts5_trigram": report["fts5_trigram"],
            "query": _summary(measurements),
        }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
