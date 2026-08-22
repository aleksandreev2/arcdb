#!/usr/bin/env python3
"""Run the dedicated ArchiveDB EPUB packager worker."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.epub_io import EpubLimits
from arcdb.jobs import JobStore
from arcdb.package_worker import (
    PackageWorkerSettings,
    cleanup_expired_job_artifacts,
    run_one_job,
    worker_identity,
)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setting(values: dict[str, str], name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or values.get(name, "").strip() or default


def integer(values: dict[str, str], name: str, default: int) -> int:
    raw = setting(values, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}.") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be positive.")
    return value


def resolve_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job.")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0 or args.poll_seconds > 60:
        parser.error("--poll-seconds must be greater than 0 and at most 60.")

    values = read_env_file(args.env_file)
    meta_dir = resolve_path(setting(values, "META_DIR", "/home/ubuntu/metadata"))
    sessions_dir = resolve_path(
        setting(
            values,
            "EPUB_PACKAGE_SESSIONS_DIR",
            str(meta_dir / "epub_package_sessions"),
        )
    )
    jobs_db = resolve_path(
        setting(values, "PACKAGE_JOBS_DB_PATH", str(meta_dir / "package_jobs.sqlite3"))
    )
    sessions_dir.mkdir(parents=True, exist_ok=True)
    settings = PackageWorkerSettings(
        sessions_dir=sessions_dir,
        limits=EpubLimits(
            max_entries=integer(values, "MAX_EPUB_FILES", 10_000),
            max_entry_bytes=integer(values, "MAX_EPUB_ENTRY_BYTES", 128 * 1024 * 1024),
            max_total_uncompressed_bytes=integer(
                values, "MAX_EPUB_UNCOMPRESSED_BYTES", 750 * 1024 * 1024
            ),
            max_compression_ratio=integer(values, "MAX_EPUB_COMPRESSION_RATIO", 250),
            max_text_entry_bytes=integer(
                values, "MAX_EPUB_TEXT_ENTRY_BYTES", 8 * 1024 * 1024
            ),
        ),
        max_session_bytes=integer(
            values, "MAX_EPUB_PACKAGE_SESSION_BYTES", 256 * 1024 * 1024
        ),
        stale_after_seconds=integer(values, "PACKAGE_JOB_STALE_SECONDS", 60),
    )
    store = JobStore(jobs_db)
    identity = worker_identity()
    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"[PACKAGER] worker={identity} queue={jobs_db}", flush=True)

    last_cleanup = 0.0
    while not stopping:
        job = run_one_job(store, settings, worker_id=identity)
        if job is not None:
            print(
                f"[PACKAGER] job={job.job_id} state={job.state} attempts={job.attempts}",
                flush=True,
            )
        now = time.monotonic()
        if now - last_cleanup >= 300:
            removed = cleanup_expired_job_artifacts(store, sessions_dir)
            if removed:
                print(f"[PACKAGER] cleaned_sessions={removed}", flush=True)
            last_cleanup = now
        if args.once:
            return 0 if job is not None and job.state in {"done", "cancelled"} else 1
        if job is None:
            time.sleep(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
