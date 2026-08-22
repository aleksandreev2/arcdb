from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .storage.safe_migration import sha256_file


PRIVATE_INVENTORY_FORMAT = "archivedb-production-inventory-private-v1"
PUBLIC_INVENTORY_FORMAT = "archivedb-production-inventory-report-v1"
PRIVATE_RECONCILIATION_FORMAT = "archivedb-production-reconciliation-private-v1"
PUBLIC_RECONCILIATION_FORMAT = "archivedb-production-reconciliation-report-v1"

KNOWN_METADATA_FILES = {
    "access_revocations.jsonl",
    "allowed_gmails.txt",
    "bookmarks.json",
    "collections.json",
    "community.json",
    "custom_meta.json",
    "descriptions.txt",
    "download_abuse.jsonl",
    "download_log.jsonl",
    "ip_email_map.json",
    "ip_exemptions.json",
    "novels_full.json",
    "tags_en.txt",
    "titles_en.txt",
    "user_data.json",
    "user_uploads.json",
    "users.json",
}

EXCLUDED_SOURCE_DIRS = {
    ".git",
    ".runtime",
    ".venv",
    "__pycache__",
    "data",
    "migration-backups",
    "node_modules",
}

EXCLUDED_SOURCE_NAMES = {
    ".baseline.sha256",
    ".env",
    ".runtime.sha256",
    "cookies.txt",
}

EXCLUDED_SOURCE_SUFFIXES = {
    ".crt",
    ".db",
    ".epub",
    ".key",
    ".log",
    ".pem",
    ".session",
    ".sqlite",
    ".sqlite3",
    ".zip",
}

ALLOWED_CONTENT_LABELS = {
    "batched_epubs",
    "cache",
    "chapters",
    "covers",
    "epubs",
    "library",
    "output",
    "packaging",
    "structured_output",
    "temp",
    "uploads",
}

ALLOWED_METADATA_LABELS = {
    "allowlist",
    "collections",
    "community",
    "custom_meta",
    "raw_master_csv",
    "translated_csv",
    "user_data",
    "user_uploads",
    "users",
}

CLOUDFLARED_PUBLIC_KEYS = {
    "credentials-file",
    "ingress",
    "loglevel",
    "metrics",
    "no-autoupdate",
    "originRequest",
    "protocol",
    "tunnel",
    "warp-routing",
}


class InventoryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InventoryPaths:
    app_root: Path
    meta_dir: Path
    sqlite_db: Path
    metadata_files: tuple[tuple[str, Path], ...] = ()
    content_roots: tuple[tuple[str, Path], ...] = ()
    systemd_units: tuple[Path, ...] = ()
    cloudflared_config: Path | None = None
    mountinfo_path: Path = Path("/proc/self/mountinfo")


def resolve_inventory_paths(
    *,
    app_root: Path,
    meta_dir: Path,
    sqlite_db: Path,
    metadata_files: Iterable[tuple[str, Path]] = (),
    content_roots: Iterable[tuple[str, Path]] = (),
    systemd_units: Iterable[Path] = (),
    cloudflared_config: Path | None = None,
    mountinfo_path: Path | None = None,
) -> InventoryPaths:
    normalized_metadata: list[tuple[str, Path]] = []
    metadata_labels: set[str] = set()
    for raw_label, raw_path in metadata_files:
        label = raw_label.strip().lower()
        if label not in ALLOWED_METADATA_LABELS:
            raise InventoryError(
                "invalid_metadata_label",
                "Metadata-file label is not one of the approved state categories.",
            )
        if label in metadata_labels:
            raise InventoryError(
                "duplicate_metadata_label", f"Duplicate metadata-file label: {label}."
            )
        metadata_labels.add(label)
        normalized_metadata.append((label, raw_path.expanduser().resolve()))

    normalized_roots: list[tuple[str, Path]] = []
    labels: set[str] = set()
    for raw_label, raw_path in content_roots:
        label = raw_label.strip().lower()
        if label not in ALLOWED_CONTENT_LABELS:
            raise InventoryError(
                "invalid_content_label",
                "Content-root label is not one of the approved storage categories.",
            )
        if label in labels:
            raise InventoryError(
                "duplicate_content_label", f"Duplicate content-root label: {label}."
            )
        labels.add(label)
        normalized_roots.append((label, raw_path.expanduser().resolve()))

    return InventoryPaths(
        app_root=app_root.expanduser().resolve(),
        meta_dir=meta_dir.expanduser().resolve(),
        sqlite_db=sqlite_db.expanduser().resolve(),
        metadata_files=tuple(normalized_metadata),
        content_roots=tuple(normalized_roots),
        systemd_units=tuple(path.expanduser().resolve() for path in systemd_units),
        cloudflared_config=(
            None
            if cloudflared_config is None
            else cloudflared_config.expanduser().resolve()
        ),
        mountinfo_path=(mountinfo_path or Path("/proc/self/mountinfo"))
        .expanduser()
        .resolve(),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_excluded_source(relative: Path) -> bool:
    if any(part in EXCLUDED_SOURCE_DIRS for part in relative.parts):
        return True
    name = relative.name.casefold()
    if name in EXCLUDED_SOURCE_NAMES or name.startswith(".env."):
        return True
    if name.endswith("-wal") or name.endswith("-shm"):
        return True
    if relative.suffix.casefold() in EXCLUDED_SOURCE_SUFFIXES:
        return True
    return False


def _fingerprint_tree(
    root: Path,
    *,
    exclude_source_state: bool,
    max_files: int = 100_000,
) -> tuple[list[dict[str, Any]], int]:
    if not root.is_dir():
        raise InventoryError("root_missing", "An explicit inventory root is missing.")
    files: list[dict[str, Any]] = []
    excluded = 0
    for path in sorted(root.rglob("*"), key=lambda value: str(value).casefold()):
        if not path.is_symlink() and not path.is_file():
            continue
        relative = path.relative_to(root)
        if exclude_source_state and _is_excluded_source(relative):
            excluded += 1
            continue
        files.append(_fingerprint_path(path, relative.as_posix()))
        if len(files) > max_files:
            raise InventoryError(
                "file_limit_exceeded",
                f"Inventory exceeded the configured {max_files}-file safety limit.",
            )
    return files, excluded


def _fingerprint_path(path: Path, relative_path: str) -> dict[str, Any]:
    if path.is_symlink():
        target = os.readlink(path)
        target_bytes = os.fsencode(target)
        return {
            "relative_path": relative_path,
            "type": "symlink",
            "size": len(target_bytes),
            "sha256": hashlib.sha256(target_bytes).hexdigest(),
            "link_target": target,
        }
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise InventoryError(
            "file_changed_during_inventory",
            "A fingerprinted file changed during inventory collection.",
        )
    return {
        "relative_path": relative_path,
        "type": "file",
        "size": after.st_size,
        "sha256": digest,
    }


def _aggregate_files(files: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(
            (
                f"{item['relative_path']}\0{item.get('type', 'file')}\0"
                f"{item['size']}\0{item['sha256']}\n"
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _git_revision(app_root: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(app_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        dirty_output = subprocess.run(
            ["git", "-C", str(app_root), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return {"available": False, "revision": None, "tracked_changes": None}
    return {
        "available": bool(re.fullmatch(r"[0-9a-fA-F]{40}", revision)),
        "revision": revision or None,
        "tracked_changes": len([line for line in dirty_output.splitlines() if line]),
    }


def _directory_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "file_count": 0, "total_bytes": 0}
    if not path.is_dir():
        return {"exists": True, "is_directory": False, "file_count": 0, "total_bytes": 0}
    count = 0
    total = 0
    file_types: dict[str, int] = {}
    for item in path.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        count += 1
        total += item.stat().st_size
        suffix = item.suffix.casefold()
        if suffix == ".epub":
            category = "epub"
        elif suffix in {".html", ".htm", ".xhtml"}:
            category = "html"
        elif suffix in {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}:
            category = "image"
        elif suffix in {".7z", ".tar", ".tgz", ".zip"}:
            category = "archive"
        else:
            category = "other"
        file_types[category] = file_types.get(category, 0) + 1
    return {
        "exists": True,
        "is_directory": True,
        "file_count": count,
        "total_bytes": total,
        "file_types": dict(sorted(file_types.items())),
    }


def _metadata_inventory(
    meta_dir: Path, configured_files: tuple[tuple[str, Path], ...]
) -> dict[str, Any]:
    files, _ = _fingerprint_tree(
        meta_dir, exclude_source_state=False, max_files=100_000
    )
    for item in files:
        item["known"] = Path(item["relative_path"]).name in KNOWN_METADATA_FILES
    configured: dict[str, dict[str, Any]] = {}
    tree_by_resolved = {
        str((meta_dir / item["relative_path"]).resolve()): item for item in files
    }
    for label, path in configured_files:
        value: dict[str, Any] = {
            "path": str(path),
            "exists": path.is_file() or path.is_symlink(),
        }
        if path.is_file() or path.is_symlink():
            fingerprint = _fingerprint_path(path, f"external/{label}")
            value.update(
                {
                    "type": fingerprint["type"],
                    "size": fingerprint["size"],
                    "sha256": fingerprint["sha256"],
                }
            )
            existing = tree_by_resolved.get(str(path.resolve()))
            if existing is not None:
                existing["known"] = True
            else:
                files.append({**fingerprint, "known": True})
        configured[label] = value
    files.sort(key=lambda item: str(item["relative_path"]).casefold())
    return {
        "path": str(meta_dir),
        "files": files,
        "configured_files": configured,
        "aggregate_sha256": _aggregate_files(files),
        "unknown_files": sum(1 for item in files if not item["known"]),
        "total_bytes": sum(int(item["size"]) for item in files),
    }


def _sqlite_inventory(path: Path) -> dict[str, Any]:
    sidecars = {
        suffix.removeprefix("-"): Path(str(path) + suffix).is_file()
        for suffix in ("-wal", "-shm")
    }
    if not path.is_file():
        return {"path": str(path), "exists": False, "size": 0, "sidecars": sidecars}
    return {
        "path": str(path),
        "exists": True,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "sidecars": sidecars,
    }


def _parse_systemd_unit(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InventoryError("systemd_unit_missing", "An explicit systemd unit is missing.")
    section = ""
    values: dict[str, list[str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise InventoryError(
            "systemd_unit_unreadable", "A systemd unit could not be read as UTF-8."
        ) from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Service" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip(), []).append(value.strip())

    exec_start = " ".join(values.get("ExecStart", []))
    gunicorn = _gunicorn_summary(exec_start)
    environment_keys: set[str] = set()
    for declaration in values.get("Environment", []):
        try:
            tokens = shlex.split(declaration)
        except ValueError:
            tokens = []
        for token in tokens:
            if "=" in token:
                environment_keys.add(token.split("=", 1)[0])

    return {
        "unit_path": str(path),
        "unit_name": path.name,
        "user": (values.get("User") or [None])[-1],
        "group": (values.get("Group") or [None])[-1],
        "working_directory": (values.get("WorkingDirectory") or [None])[-1],
        "environment_files": values.get("EnvironmentFile", []),
        "environment_keys": sorted(environment_keys),
        "restart": (values.get("Restart") or [None])[-1],
        "gunicorn": gunicorn,
    }


def _gunicorn_summary(command: str) -> dict[str, Any] | None:
    if "gunicorn" not in command.casefold():
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return {"detected": True, "parse_ok": False}
    result: dict[str, Any] = {"detected": True, "parse_ok": True}
    value_flags = {
        "--workers": "workers",
        "-w": "workers",
        "--threads": "threads",
        "--timeout": "timeout_seconds",
        "-t": "timeout_seconds",
        "--worker-class": "worker_class",
        "-k": "worker_class",
        "--bind": "bind",
        "-b": "bind",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        matched = False
        for flag, key in value_flags.items():
            if token == flag and index + 1 < len(tokens):
                result[key] = tokens[index + 1]
                index += 2
                matched = True
                break
            if token.startswith(flag + "="):
                result[key] = token.split("=", 1)[1]
                index += 1
                matched = True
                break
        if matched:
            continue
        index += 1
    return result


def _public_gunicorn_summary(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "detected": bool(value.get("detected")),
        "parse_ok": bool(value.get("parse_ok")),
    }
    for key in ("workers", "threads", "timeout_seconds"):
        raw = value.get(key)
        if raw is not None and str(raw).isdigit():
            allowed[key] = int(str(raw))
    worker_class = value.get("worker_class")
    if worker_class and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(worker_class)):
        allowed["worker_class"] = str(worker_class)
    bind = value.get("bind")
    if bind:
        raw_bind = str(bind).casefold()
        if raw_bind.startswith("unix:"):
            allowed["bind_type"] = "unix"
        elif ":" in raw_bind:
            allowed["bind_type"] = "tcp"
        else:
            allowed["bind_type"] = "other"
    return allowed


def _cloudflared_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"supplied": False, "exists": False}
    if not path.is_file():
        return {"supplied": True, "exists": False, "config_path": str(path)}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InventoryError(
            "cloudflared_config_unreadable",
            "The explicit cloudflared config could not be read as UTF-8.",
        ) from exc
    top_level_keys: set[str] = set()
    ingress_rules = 0
    service_schemes: set[str] = set()
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        top_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):", raw_line)
        if top_match and not raw_line.startswith((" ", "\t", "-")):
            top_level_keys.add(top_match.group(1))
        if re.match(r"^-\s+(?:hostname|service):", stripped):
            ingress_rules += 1
        service_match = re.match(r"^-?\s*service:\s*([A-Za-z][A-Za-z0-9+.-]*):", stripped)
        if service_match:
            service_schemes.add(service_match.group(1).lower())
    return {
        "supplied": True,
        "exists": True,
        "config_path": str(path),
        "top_level_keys": sorted(top_level_keys),
        "ingress_entries": ingress_rules,
        "service_schemes": sorted(service_schemes),
        "secret_values_collected": False,
    }


def _public_cloudflared_summary(value: dict[str, Any]) -> dict[str, Any]:
    keys = set(value.get("top_level_keys", []))
    return {
        "supplied": bool(value.get("supplied")),
        "exists": bool(value.get("exists")),
        "recognized_keys": sorted(keys & CLOUDFLARED_PUBLIC_KEYS),
        "unrecognized_key_count": len(keys - CLOUDFLARED_PUBLIC_KEYS),
        "ingress_entries": int(value.get("ingress_entries", 0)),
        "service_schemes": value.get("service_schemes", []),
        "secret_values_collected": False,
    }


def _decode_mountinfo_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _parse_mountinfo(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    mounts: list[dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    for line in lines:
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 2:
            continue
        mounts.append(
            {
                "mount_point": _decode_mountinfo_path(left_fields[4]),
                "mount_options": left_fields[5],
                "filesystem": right_fields[0],
                "source": right_fields[1],
            }
        )
    return mounts


def _mount_for(target: Path, mounts: list[dict[str, str]]) -> dict[str, Any] | None:
    raw_target = str(target)
    candidates = []
    for item in mounts:
        mount_point = item["mount_point"]
        try:
            if os.path.commonpath((raw_target, mount_point)) == mount_point:
                candidates.append(item)
        except ValueError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item["mount_point"]))


def collect_production_inventory(
    paths: InventoryPaths,
    *,
    source_revision: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not paths.app_root.is_dir():
        raise InventoryError("application_root_missing", "Explicit application root is missing.")
    if not paths.meta_dir.is_dir():
        raise InventoryError("metadata_root_missing", "Explicit metadata root is missing.")

    source_files, excluded_source_files = _fingerprint_tree(
        paths.app_root, exclude_source_state=True
    )
    revision = _git_revision(paths.app_root)
    if source_revision:
        revision = {
            "available": True,
            "revision": source_revision.strip(),
            "tracked_changes": revision.get("tracked_changes"),
            "operator_supplied": True,
        }
    metadata = _metadata_inventory(paths.meta_dir, paths.metadata_files)
    sqlite = _sqlite_inventory(paths.sqlite_db)
    content = {
        label: {"path": str(path), **_directory_summary(path)}
        for label, path in paths.content_roots
    }
    units = [_parse_systemd_unit(path) for path in paths.systemd_units]
    cloudflared = _cloudflared_summary(paths.cloudflared_config)
    mounts = _parse_mountinfo(paths.mountinfo_path)
    path_mounts = {
        "application": _mount_for(paths.app_root, mounts),
        "metadata": _mount_for(paths.meta_dir, mounts),
        "sqlite": _mount_for(paths.sqlite_db, mounts),
        "content": {
            label: _mount_for(path, mounts) for label, path in paths.content_roots
        },
    }
    generated_at = _utc_now()
    private = {
        "format": PRIVATE_INVENTORY_FORMAT,
        "generated_at_utc": generated_at,
        "status": "inventory_collected",
        "paths": {
            "application_root": str(paths.app_root),
            "metadata_root": str(paths.meta_dir),
            "sqlite_database": str(paths.sqlite_db),
        },
        "source": {
            "revision": revision,
            "files": source_files,
            "excluded_files": excluded_source_files,
            "aggregate_sha256": _aggregate_files(source_files),
        },
        "metadata": metadata,
        "sqlite": sqlite,
        "content_roots": content,
        "runtime": {"systemd_units": units, "cloudflared": cloudflared},
        "mounts": path_mounts,
        "safety": {
            "read_only_collection": True,
            "payloads_collected": False,
            "environment_values_collected": False,
            "cloudflared_secret_values_collected": False,
        },
    }

    gunicorn = [unit["gunicorn"] for unit in units if unit["gunicorn"]]
    mount_values = [
        value
        for value in (
            path_mounts["application"],
            path_mounts["metadata"],
            path_mounts["sqlite"],
            *path_mounts["content"].values(),
        )
        if value is not None
    ]
    public = {
        "format": PUBLIC_INVENTORY_FORMAT,
        "generated_at_utc": generated_at,
        "status": "inventory_collected",
        "coverage": {
            "application_source": True,
            "metadata": True,
            "sqlite": True,
            "content_roots": len(content),
            "systemd_units": len(units),
            "cloudflared_config": cloudflared["supplied"],
            "mounts_resolved": len(mount_values),
        },
        "source": {
            "revision_known": revision["available"],
            "revision": (
                revision["revision"]
                if revision["revision"]
                and re.fullmatch(r"[0-9a-fA-F]{40}", str(revision["revision"]))
                else None
            ),
            "tracked_changes": revision["tracked_changes"],
            "file_count": len(source_files),
            "excluded_file_count": excluded_source_files,
            "total_bytes": sum(int(item["size"]) for item in source_files),
            "aggregate_sha256": private["source"]["aggregate_sha256"],
        },
        "storage": {
            "metadata": {
                "file_count": len(metadata["files"]),
                "unknown_file_count": metadata["unknown_files"],
                "total_bytes": metadata["total_bytes"],
                "aggregate_sha256": metadata["aggregate_sha256"],
                "configured_files": {
                    label: {
                        key: raw
                        for key, raw in value.items()
                        if key in {"exists", "type", "size"}
                    }
                    for label, value in metadata["configured_files"].items()
                },
            },
            "sqlite": {
                key: value for key, value in sqlite.items() if key != "path" and key != "sha256"
            },
            "content_roots": {
                label: {key: value for key, value in value.items() if key != "path"}
                for label, value in content.items()
            },
            "mount_filesystems": sorted(
                {value["filesystem"] for value in mount_values if value.get("filesystem")}
            ),
        },
        "runtime": {
            "systemd_unit_count": len(units),
            "gunicorn": [_public_gunicorn_summary(value) for value in gunicorn],
            "cloudflared": _public_cloudflared_summary(cloudflared),
        },
        "safety": private["safety"],
        "decision": {
            "reconciliation_required": True,
            "readiness_preflight_authorized": False,
            "bounded_canary_authorized": False,
            "primary_read_authorized": False,
            "next_step": "reconcile_private_inventory_against_materialized_repository_source",
        },
    }
    return private, public


def reconcile_production_inventory(
    inventory: dict[str, Any], reference_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    if inventory.get("format") != PRIVATE_INVENTORY_FORMAT:
        raise InventoryError(
            "inventory_format_invalid", "Expected a private production inventory v1 document."
        )
    reference = reference_root.expanduser().resolve()
    reference_files, excluded = _fingerprint_tree(
        reference, exclude_source_state=True
    )
    live_files = inventory.get("source", {}).get("files")
    if not isinstance(live_files, list):
        raise InventoryError("inventory_invalid", "Inventory source file list is missing.")
    _validate_inventory_files(live_files, label="source")
    expected_source_aggregate = inventory.get("source", {}).get("aggregate_sha256")
    if expected_source_aggregate != _aggregate_files(live_files):
        raise InventoryError(
            "inventory_checksum_mismatch",
            "Private inventory source aggregate does not match its file records.",
        )
    metadata_files = inventory.get("metadata", {}).get("files")
    if not isinstance(metadata_files, list):
        raise InventoryError("inventory_invalid", "Inventory metadata file list is missing.")
    _validate_inventory_files(metadata_files, label="metadata")
    expected_metadata_aggregate = inventory.get("metadata", {}).get(
        "aggregate_sha256"
    )
    if expected_metadata_aggregate != _aggregate_files(metadata_files):
        raise InventoryError(
            "inventory_checksum_mismatch",
            "Private inventory metadata aggregate does not match its file records.",
        )
    live_by_path = {str(item["relative_path"]): item for item in live_files}
    reference_by_path = {str(item["relative_path"]): item for item in reference_files}
    missing = sorted(set(reference_by_path) - set(live_by_path))
    unknown = sorted(set(live_by_path) - set(reference_by_path))
    changed = sorted(
        path
        for path in set(live_by_path) & set(reference_by_path)
        if (
            live_by_path[path].get("type", "file"),
            live_by_path[path].get("size"),
            live_by_path[path].get("sha256"),
        )
        != (
            reference_by_path[path].get("type", "file"),
            reference_by_path[path].get("size"),
            reference_by_path[path].get("sha256"),
        )
    )
    source_matches = not missing and not unknown and not changed
    metadata_unknown = [
        item["relative_path"]
        for item in metadata_files
        if not item.get("known")
    ]
    generated_at = _utc_now()
    private = {
        "format": PRIVATE_RECONCILIATION_FORMAT,
        "generated_at_utc": generated_at,
        "status": "reconciliation_complete",
        "reference_root": str(reference),
        "reference": {
            "file_count": len(reference_files),
            "excluded_files": excluded,
            "aggregate_sha256": _aggregate_files(reference_files),
        },
        "source_diff": {
            "matches": source_matches,
            "missing_from_production": missing,
            "changed_in_production": changed,
            "unknown_in_production": unknown,
        },
        "metadata_review": {"unknown_files": sorted(metadata_unknown)},
        "decision": {
            "operator_review_required": not source_matches or bool(metadata_unknown),
            "readiness_preflight_authorized": False,
            "bounded_canary_authorized": False,
            "primary_read_authorized": False,
        },
    }
    public = {
        "format": PUBLIC_RECONCILIATION_FORMAT,
        "generated_at_utc": generated_at,
        "status": "reconciliation_complete",
        "source": {
            "matches_reference": source_matches,
            "reference_file_count": len(reference_files),
            "production_file_count": len(live_files),
            "missing_file_count": len(missing),
            "changed_file_count": len(changed),
            "unknown_file_count": len(unknown),
            "reference_aggregate_sha256": private["reference"]["aggregate_sha256"],
            "production_aggregate_sha256": inventory["source"]["aggregate_sha256"],
        },
        "metadata": {"unknown_file_count": len(metadata_unknown)},
        "decision": private["decision"],
    }
    return private, public


def _validate_inventory_files(files: list[Any], *, label: str) -> None:
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise InventoryError(
                "inventory_invalid", f"Inventory {label} file record is invalid."
            )
        relative = item.get("relative_path")
        if not isinstance(relative, str):
            raise InventoryError(
                "inventory_invalid", f"Inventory {label} relative path is invalid."
            )
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
            raise InventoryError(
                "inventory_invalid", f"Inventory {label} relative path is unsafe."
            )
        if relative in seen:
            raise InventoryError(
                "inventory_invalid", f"Inventory {label} contains duplicate paths."
            )
        seen.add(relative)
        if item.get("type", "file") not in {"file", "symlink"}:
            raise InventoryError(
                "inventory_invalid", f"Inventory {label} file type is invalid."
            )
        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(size, int) or size < 0 or not isinstance(digest, str):
            raise InventoryError(
                "inventory_invalid", f"Inventory {label} fingerprint is invalid."
            )
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InventoryError(
                "inventory_invalid", f"Inventory {label} checksum is invalid."
            )


def write_new_json(path: Path, value: dict[str, Any], *, private: bool) -> None:
    destination = path.expanduser().resolve()
    if destination.exists():
        raise InventoryError("report_exists", "Refusing to overwrite an existing report.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o600 if private else 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
