"""EPUB package job execution without importing the Flask runtime."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import time
from typing import Callable

from arcdb.epub_io import (
    EpubLimits,
    EpubSafetyError,
    normalize_archive_path,
    package_epub_streaming,
    validate_epub_archive,
)
from arcdb.jobs import Job, JobStore


PACKAGE_JOB_KIND = "epub_package"
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PACKAGE_IMAGE_RE = re.compile(r"img_\d{4,}\.(?:jpg|png|gif|webp|bmp)", re.I)


class PackageJobCancelled(Exception):
    pass


class PackageJobTimedOut(Exception):
    pass


@dataclass(frozen=True)
class PackageWorkerSettings:
    sessions_dir: Path
    limits: EpubLimits
    max_session_bytes: int
    stale_after_seconds: int = 60


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _read_json_object(path: Path, default: dict | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default
    except (OSError, json.JSONDecodeError) as exc:
        raise EpubSafetyError("The package session metadata is invalid.") from exc
    if not isinstance(value, dict):
        raise EpubSafetyError("The package session metadata is invalid.")
    return value


def _session_dir(root: Path, session_id: str) -> Path:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise EpubSafetyError("The package session identifier is invalid.")
    resolved_root = root.resolve()
    candidate = (resolved_root / session_id).resolve()
    if candidate.parent != resolved_root or candidate.is_symlink() or not candidate.is_dir():
        raise EpubSafetyError("The package session is missing or unsafe.")
    return candidate


def _resolve_child(root: Path, name: str) -> Path:
    candidate = (root / name).resolve()
    if candidate.parent != root.resolve():
        raise EpubSafetyError("The package session contains an unsafe image path.")
    return candidate


def _session_usage(session_dir: Path) -> int:
    total = 0
    for root, directories, filenames in os.walk(session_dir, followlinks=False):
        root_path = Path(root)
        directories[:] = [
            name for name in directories if not (root_path / name).is_symlink()
        ]
        for filename in filenames:
            path = root_path / filename
            if path.is_symlink():
                raise EpubSafetyError("The package session contains an unsafe link.")
            total += path.stat().st_size
    return total


def build_package(
    job: Job,
    settings: PackageWorkerSettings,
    *,
    progress: Callable[[int], None],
    cancellation_check: Callable[[], None],
) -> dict[str, str]:
    session_id = str(job.payload.get("session_id") or "")
    session_dir = _session_dir(settings.sessions_dir, session_id)
    meta = _read_json_object(session_dir / "meta.json")
    if str(meta.get("owner_email") or "").strip().lower() != job.owner_email.lower():
        raise EpubSafetyError("The package session owner is invalid.")

    base_epub = session_dir / "base.epub"
    final_epub = session_dir / "final.epub"
    images_dir = session_dir / "images"
    if base_epub.is_symlink() or not base_epub.is_file():
        raise EpubSafetyError("The base EPUB is missing or unsafe.")
    if images_dir.is_symlink() or not images_dir.is_dir():
        raise EpubSafetyError("The package image directory is missing or unsafe.")

    cancellation_check()
    summary = validate_epub_archive(base_epub, settings.limits, cancellation_check)
    progress(5)
    opf_dir = summary.opf_path.rsplit("/", 1)[0] if "/" in summary.opf_path else ""
    target_images_dir = f"{opf_dir}/Images" if opf_dir else "Images"
    injected_files: dict[str, Path] = {}

    structured_dir_raw = str(meta.get("struct_novel_dir") or "").strip()
    if structured_dir_raw:
        structured_dir = Path(structured_dir_raw)
        if structured_dir.is_dir() and not structured_dir.is_symlink():
            for folder_name in ("images", "Images"):
                local_images = structured_dir / folder_name
                if not local_images.is_dir() or local_images.is_symlink():
                    continue
                for image_path in sorted(local_images.iterdir(), key=lambda value: value.name):
                    if image_path.is_symlink() or not image_path.is_file():
                        continue
                    if image_path.suffix.casefold() not in {
                        ".webp", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".bmp"
                    }:
                        continue
                    target, _ = normalize_archive_path(
                        f"{target_images_dir}/{image_path.name}"
                    )
                    injected_files[target] = image_path

    url_map = _read_json_object(session_dir / "url_map.json", {})
    safe_url_map: dict[str, str] = {}
    for original_url, raw_filename in url_map.items():
        filename = str(raw_filename or "").strip()
        if not isinstance(original_url, str) or not _PACKAGE_IMAGE_RE.fullmatch(filename):
            raise EpubSafetyError("The package session contains an unsafe image mapping.")
        image_path = _resolve_child(images_dir, filename)
        if image_path.is_symlink() or not image_path.is_file():
            raise EpubSafetyError("A package session image is missing or unsafe.")
        target, _ = normalize_archive_path(f"{target_images_dir}/{filename}")
        injected_files[target] = image_path
        safe_url_map[original_url] = filename

    current_bytes = _session_usage(session_dir)
    if final_epub.is_file() and not final_epub.is_symlink():
        current_bytes -= final_epub.stat().st_size
    remaining_bytes = settings.max_session_bytes - current_bytes
    if remaining_bytes <= 0:
        raise EpubSafetyError("The package session exceeds its storage limit.")

    def on_package_progress(completed: int, total: int) -> None:
        cancellation_check()
        progress(5 + int(85 * completed / max(1, total)))

    package_epub_streaming(
        base_epub,
        final_epub,
        injected_files=injected_files,
        url_map=safe_url_map,
        limits=settings.limits,
        max_output_bytes=remaining_bytes,
        progress_callback=on_package_progress,
        cancellation_check=cancellation_check,
    )
    progress(95)
    return {
        "session_id": session_id,
        "download_url": f"/api/epub_package/download/{session_id}",
    }


def run_one_job(
    store: JobStore,
    settings: PackageWorkerSettings,
    *,
    worker_id: str | None = None,
) -> Job | None:
    identity = worker_id or worker_identity()
    job = store.claim_next(
        worker_id=identity,
        stale_after_seconds=settings.stale_after_seconds,
        kind=PACKAGE_JOB_KIND,
    )
    if job is None:
        return None
    started = time.monotonic()
    last_progress = 1
    last_heartbeat_at = 0.0

    def heartbeat(progress: int) -> None:
        nonlocal last_progress, last_heartbeat_at
        now = time.monotonic()
        if now - started > job.timeout_seconds:
            raise PackageJobTimedOut()
        last_progress = max(last_progress, progress)
        if now - last_heartbeat_at < 1.0 and last_progress < 95:
            return
        if not store.heartbeat(job.job_id, identity, last_progress):
            raise PackageJobCancelled()
        last_heartbeat_at = now

    def cancellation_check() -> None:
        heartbeat(last_progress)

    try:
        result = build_package(
            job,
            settings,
            progress=heartbeat,
            cancellation_check=cancellation_check,
        )
        if not store.complete(job.job_id, identity, result):
            store.mark_cancelled(job.job_id, identity)
    except PackageJobCancelled:
        store.mark_cancelled(job.job_id, identity)
    except PackageJobTimedOut:
        store.fail(
            job.job_id,
            identity,
            error_code="timeout",
            error_message="The package job exceeded its time limit.",
            retryable=True,
        )
    except EpubSafetyError:
        store.fail(
            job.job_id,
            identity,
            error_code="invalid_input",
            error_message="The EPUB package input failed safety validation.",
            retryable=False,
        )
    except Exception:
        store.fail(
            job.job_id,
            identity,
            error_code="worker_error",
            error_message="The package worker could not complete this attempt.",
            retryable=True,
        )
    return store.get(job.job_id)


def cleanup_expired_job_artifacts(store: JobStore, sessions_dir: Path) -> int:
    removed = 0
    for job in store.cleanup_expired():
        session_id = str(job.payload.get("session_id") or "")
        if not _SESSION_ID_RE.fullmatch(session_id):
            continue
        if store.get_active_by_dedupe(f"epub_package:{session_id}") is not None:
            continue
        session_dir = sessions_dir.resolve() / session_id
        if session_dir.parent != sessions_dir.resolve() or session_dir.is_symlink():
            continue
        if session_dir.is_dir():
            shutil.rmtree(session_dir, ignore_errors=True)
            removed += 1
    return removed
