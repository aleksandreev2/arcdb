#!/usr/bin/env python3
"""Summarize sanitized ArchiveDB request timing events as p50/p95/p99 JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.observability import summarize_request_events  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, help="Bounded ArchiveDB process log")
    parser.add_argument("--output", help="Optional new JSON report path")
    args = parser.parse_args()

    log_path = Path(args.log).expanduser().resolve()
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            report = summarize_request_events(handle)
    except OSError as exc:
        print(f"Request timing summary failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    if report["status"] != "ok":
        print("Request timing summary failed: no request events", file=sys.stderr)
        return 2

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        if output.exists():
            print("Request timing summary failed: output already exists", file=sys.stderr)
            return 2
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
