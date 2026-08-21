from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.storage.read_observation import (  # noqa: E402
    ObservationError,
    verify_shadow_observation,
)


def write_report(path: Path, report: dict) -> None:
    destination = path.expanduser().resolve()
    if not destination.parent.is_dir():
        raise ObservationError(
            "report_parent_missing", "Report parent directory does not exist."
        )
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ObservationError(
            "report_exists", "Refusing to overwrite an existing observation report."
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify payload-free ArchiveDB shadow-read observation events from "
            "one bounded process log."
        )
    )
    parser.add_argument("--log", required=True, help="Explicit bounded-process log")
    parser.add_argument(
        "--minimum-reported-matches",
        type=int,
        default=1,
        help="Minimum reported match counter required for each state domain",
    )
    parser.add_argument(
        "--report",
        help="Optional new JSON report path; existing files are never overwritten",
    )
    args = parser.parse_args()

    report = verify_shadow_observation(
        Path(args.log),
        minimum_reported_matches=args.minimum_reported_matches,
    )
    if args.report:
        write_report(Path(args.report), report)

    print(
        "Read shadow observation: PASSED "
        f"({len(report['domains'])} domain(s), "
        f"{report['parsed_events']} payload-free event(s))."
    )
    print(
        "SQLite primary reads are NOT authorized by this report; "
        "operator review and a separately bounded canary remain required."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ObservationError as exc:
        print(
            f"Read shadow observation: FAILED [{exc.code}]: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except Exception as exc:
        print(
            "Read shadow observation: FAILED [internal_error]: "
            f"unexpected {type(exc).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
