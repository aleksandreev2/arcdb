"""Bounded, atomic upload and EPUB/ZIP I/O helpers.

The Flask runtime delegates untrusted archive handling here so the safety rules can
be tested without importing the application or starting its background services.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import posixpath
import re
import shutil
import stat
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Mapping
import xml.etree.ElementTree as ET


COPY_CHUNK_BYTES = 1024 * 1024
EPUB_MIMETYPE = b"application/epub+zip"
HTML_SUFFIXES = (".xhtml", ".html", ".htm")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_ARCHIVE_PATH_CHARS = 1024
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class EpubSafetyError(ValueError):
    """Raised when an archive or bounded file operation fails safety checks."""


@dataclass(frozen=True)
class EpubLimits:
    max_entries: int = 10_000
    max_entry_bytes: int = 128 * 1024 * 1024
    max_total_uncompressed_bytes: int = 750 * 1024 * 1024
    max_compression_ratio: float = 250.0
    compression_ratio_min_bytes: int = 1024 * 1024
    max_text_entry_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        values = {
            "max_entries": self.max_entries,
            "max_entry_bytes": self.max_entry_bytes,
            "max_total_uncompressed_bytes": self.max_total_uncompressed_bytes,
            "max_compression_ratio": self.max_compression_ratio,
            "compression_ratio_min_bytes": self.compression_ratio_min_bytes,
            "max_text_entry_bytes": self.max_text_entry_bytes,
        }
        invalid = [name for name, value in values.items() if value <= 0]
        if invalid:
            raise ValueError("EPUB limits must be positive: " + ", ".join(invalid))
        if self.max_text_entry_bytes > self.max_entry_bytes:
            raise ValueError("max_text_entry_bytes cannot exceed max_entry_bytes")


@dataclass(frozen=True)
class EpubArchiveSummary:
    entry_count: int
    file_count: int
    total_uncompressed_bytes: int
    html_entry_count: int
    opf_path: str


@dataclass(frozen=True)
class _ArchiveEntry:
    info: zipfile.ZipInfo
    path: str
    key: str


class _BoundedWriter:
    def __init__(self, output: BinaryIO, max_bytes: int) -> None:
        self._output = output
        self._max_bytes = max_bytes
        self._high_water = 0

    def write(self, data: bytes) -> int:
        end_position = self._output.tell() + len(data)
        if max(self._high_water, end_position) > self._max_bytes:
            raise EpubSafetyError("The packaged EPUB exceeds the session output limit.")
        written = self._output.write(data)
        self._high_water = max(self._high_water, self._output.tell())
        return written

    def __getattr__(self, name: str):
        return getattr(self._output, name)


def normalize_archive_path(raw_name: str) -> tuple[str, str]:
    """Return a portable normalized archive path and collision key."""

    name = str(raw_name or "").replace("\\", "/")
    if not name or _CONTROL_CHARS_RE.search(name):
        raise EpubSafetyError("The EPUB contains an empty or control-character path.")
    if len(name) > _MAX_ARCHIVE_PATH_CHARS:
        raise EpubSafetyError("The EPUB contains an excessively long file path.")
    if name.startswith("/") or _DRIVE_PATH_RE.match(name):
        raise EpubSafetyError("The EPUB contains an unsafe absolute file path.")

    normalized_parts: list[str] = []
    for raw_part in name.split("/"):
        if raw_part in ("", "."):
            continue
        if raw_part == "..":
            raise EpubSafetyError("The EPUB contains an unsafe parent-directory path.")
        part = unicodedata.normalize("NFC", raw_part)
        if part in ("", ".", "..") or part.endswith((" ", ".")):
            raise EpubSafetyError("The EPUB contains a non-portable file path.")
        if ":" in part:
            raise EpubSafetyError("The EPUB contains a non-portable file path.")
        reserved_stem = part.split(".", 1)[0].casefold()
        if reserved_stem in _WINDOWS_RESERVED_NAMES:
            raise EpubSafetyError("The EPUB contains a reserved file path.")
        normalized_parts.append(part)

    if not normalized_parts:
        raise EpubSafetyError("The EPUB contains an empty file path.")
    path = "/".join(normalized_parts)
    return path, unicodedata.normalize("NFC", path).casefold()


def _read_entry_limited(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    with archive.open(info, "r") as source:
        while True:
            chunk = source.read(min(COPY_CHUNK_BYTES, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise EpubSafetyError("An EPUB text/metadata entry is too large.")
            chunks.append(chunk)
    return b"".join(chunks)


def _parse_xml(data: bytes, description: str) -> ET.Element:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise EpubSafetyError(f"The EPUB {description} contains prohibited XML declarations.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise EpubSafetyError(f"The EPUB {description} is malformed.") from exc


def _inspect_archive(
    archive: zipfile.ZipFile,
    limits: EpubLimits,
) -> tuple[EpubArchiveSummary, tuple[_ArchiveEntry, ...]]:
    entries: list[_ArchiveEntry] = []
    seen: set[str] = set()
    files: set[str] = set()
    directories: set[str] = set()
    total_uncompressed = 0
    file_count = 0
    html_count = 0

    for info in archive.infolist():
        if len(entries) >= limits.max_entries:
            raise EpubSafetyError(
                f"The EPUB contains more than {limits.max_entries} entries."
            )
        path, key = normalize_archive_path(info.filename)
        if key in seen:
            raise EpubSafetyError("The EPUB contains duplicate or colliding paths.")
        seen.add(key)

        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type == stat.S_IFLNK:
            raise EpubSafetyError("The EPUB contains a symbolic link, which is not allowed.")
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise EpubSafetyError("The EPUB contains an unsupported special file.")
        if info.flag_bits & 0x1:
            raise EpubSafetyError("Password-protected EPUB files are not supported.")

        parents = path.split("/")[:-1]
        parent_key = ""
        for parent in parents:
            parent_key = f"{parent_key}/{parent}" if parent_key else parent
            folded_parent = unicodedata.normalize("NFC", parent_key).casefold()
            if folded_parent in files:
                raise EpubSafetyError("The EPUB contains a file/directory path collision.")
            directories.add(folded_parent)

        is_directory = info.is_dir() or file_type == stat.S_IFDIR
        if is_directory:
            if key in files:
                raise EpubSafetyError("The EPUB contains a file/directory path collision.")
            directories.add(key)
        else:
            if key in directories:
                raise EpubSafetyError("The EPUB contains a file/directory path collision.")
            files.add(key)
            file_count += 1
            size = max(0, int(info.file_size or 0))
            if size > limits.max_entry_bytes:
                raise EpubSafetyError("The EPUB contains an oversized entry.")
            total_uncompressed += size
            if total_uncompressed > limits.max_total_uncompressed_bytes:
                raise EpubSafetyError("The EPUB is too large after decompression.")
            compressed = max(1, int(info.compress_size or 0))
            if (
                size >= limits.compression_ratio_min_bytes
                and (size / compressed) > limits.max_compression_ratio
            ):
                raise EpubSafetyError("The EPUB has an unsafe compression ratio.")
            if path.casefold().endswith(HTML_SUFFIXES):
                html_count += 1
        entries.append(_ArchiveEntry(info=info, path=path, key=key))

    by_key = {entry.key: entry for entry in entries}
    mimetype = by_key.get("mimetype")
    if mimetype is None or mimetype.info.is_dir():
        raise EpubSafetyError("The archive is missing the EPUB mimetype entry.")
    if _read_entry_limited(archive, mimetype.info, 256) != EPUB_MIMETYPE:
        raise EpubSafetyError("The archive has an invalid EPUB mimetype entry.")

    container = by_key.get("meta-inf/container.xml")
    if container is None or container.info.is_dir():
        raise EpubSafetyError("The EPUB is missing META-INF/container.xml.")
    container_root = _parse_xml(
        _read_entry_limited(archive, container.info, limits.max_text_entry_bytes),
        "container metadata",
    )

    opf_path = ""
    for element in container_root.iter():
        if element.tag.rsplit("}", 1)[-1] != "rootfile":
            continue
        candidate = element.attrib.get("full-path", "")
        try:
            normalized, key = normalize_archive_path(candidate)
        except EpubSafetyError:
            continue
        entry = by_key.get(key)
        if entry is not None and not entry.info.is_dir():
            opf_path = normalized
            break
    if not opf_path:
        raise EpubSafetyError("The EPUB container does not reference a valid OPF package.")

    opf_entry = by_key[unicodedata.normalize("NFC", opf_path).casefold()]
    opf_root = _parse_xml(
        _read_entry_limited(archive, opf_entry.info, limits.max_text_entry_bytes),
        "OPF package",
    )
    if opf_root.tag.rsplit("}", 1)[-1] != "package":
        raise EpubSafetyError("The EPUB OPF root is not a package element.")
    if html_count <= 0:
        raise EpubSafetyError("The EPUB contains no readable HTML/XHTML chapters.")

    return (
        EpubArchiveSummary(
            entry_count=len(entries),
            file_count=file_count,
            total_uncompressed_bytes=total_uncompressed,
            html_entry_count=html_count,
            opf_path=opf_path,
        ),
        tuple(entries),
    )


def _verify_archive_contents(
    archive: zipfile.ZipFile,
    entries: tuple[_ArchiveEntry, ...],
    limits: EpubLimits,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    actual_total = 0
    for entry in entries:
        if entry.info.is_dir():
            continue
        with archive.open(entry.info, "r") as source:
            actual_size = 0
            while True:
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                actual_size += len(chunk)
                if actual_size > limits.max_entry_bytes:
                    raise EpubSafetyError("An EPUB entry exceeded its decompression limit.")
                if checkpoint is not None:
                    checkpoint()
            actual_total += actual_size
            if actual_total > limits.max_total_uncompressed_bytes:
                raise EpubSafetyError("The EPUB exceeded its decompression limit.")


def validate_epub_archive(
    path: str | os.PathLike[str],
    limits: EpubLimits,
    checkpoint: Callable[[], None] | None = None,
) -> EpubArchiveSummary:
    try:
        if not zipfile.is_zipfile(path):
            raise EpubSafetyError("The selected EPUB is not a valid ZIP/EPUB archive.")
        with zipfile.ZipFile(path, "r") as archive:
            summary, entries = _inspect_archive(archive, limits)
            _verify_archive_contents(archive, entries, limits, checkpoint)
            return summary
    except EpubSafetyError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        NotImplementedError,
        RuntimeError,
        EOFError,
        OSError,
    ) as exc:
        raise EpubSafetyError("The EPUB archive is damaged or invalid.") from exc


def copy_upload_limited(
    source: BinaryIO,
    destination: str | os.PathLike[str],
    max_bytes: int,
) -> int:
    """Stream to a sibling temp file, sync once, then atomically publish."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    destination_path = os.fspath(destination)
    os.makedirs(os.path.dirname(destination_path) or ".", exist_ok=True)
    temporary_path = f"{destination_path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    total = 0
    try:
        with open(temporary_path, "xb") as output:
            while True:
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise EpubSafetyError(
                        f"File exceeds the {max_bytes // (1024 * 1024)} MB limit."
                    )
                output.write(chunk)
            if total <= 0:
                raise EpubSafetyError("The uploaded file is empty.")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination_path)
        return total
    except Exception:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass
        raise


def _copy_bounded_stream(
    source: BinaryIO,
    destination: BinaryIO,
    max_bytes: int,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    total = 0
    while True:
        chunk = source.read(COPY_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise EpubSafetyError("An EPUB entry exceeded its decompression limit.")
        destination.write(chunk)
        if checkpoint is not None:
            checkpoint()
    return total


def copy_zip_entry_atomic(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: str | os.PathLike[str],
    max_bytes: int,
    max_compression_ratio: float = 250.0,
    compression_ratio_min_bytes: int = 1024 * 1024,
) -> int:
    """Bound one ZIP member and atomically publish it without materializing it."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type == stat.S_IFLNK or info.flag_bits & 0x1:
        raise EpubSafetyError("The ZIP entry is a link or encrypted file.")
    if file_type not in (0, stat.S_IFREG) or info.is_dir() or int(info.file_size or 0) > max_bytes:
        raise EpubSafetyError("The ZIP entry is not a bounded regular file.")
    size = max(0, int(info.file_size or 0))
    compressed = max(1, int(info.compress_size or 0))
    if (
        size >= compression_ratio_min_bytes
        and (size / compressed) > max_compression_ratio
    ):
        raise EpubSafetyError("The ZIP entry has an unsafe compression ratio.")

    destination_path = os.path.abspath(os.fspath(destination))
    os.makedirs(os.path.dirname(destination_path) or ".", exist_ok=True)
    temporary_path = f"{destination_path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        with archive.open(info, "r") as source, open(temporary_path, "xb") as output:
            total = _copy_bounded_stream(source, output, max_bytes)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination_path)
        return total
    except Exception:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass
        raise


def extract_epub_safely(
    epub_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    limits: EpubLimits,
) -> EpubArchiveSummary:
    """Extract into a fresh sibling directory and publish only after success."""

    destination_path = os.path.abspath(os.fspath(destination))
    if os.path.lexists(destination_path):
        raise EpubSafetyError("The EPUB extraction destination already exists.")
    parent = os.path.dirname(destination_path) or "."
    os.makedirs(parent, exist_ok=True)
    temporary_path = f"{destination_path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    os.makedirs(temporary_path, exist_ok=False)
    actual_total = 0
    try:
        with zipfile.ZipFile(epub_path, "r") as archive:
            summary, entries = _inspect_archive(archive, limits)
            for entry in entries:
                target = os.path.abspath(
                    os.path.join(temporary_path, *entry.path.split("/"))
                )
                if os.path.commonpath((temporary_path, target)) != temporary_path:
                    raise EpubSafetyError("The EPUB contains an unsafe extraction path.")
                if entry.info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(entry.info, "r") as source, open(target, "xb") as output:
                    actual_size = _copy_bounded_stream(
                        source, output, limits.max_entry_bytes
                    )
                actual_total += actual_size
                if actual_total > limits.max_total_uncompressed_bytes:
                    raise EpubSafetyError("The EPUB exceeded its decompression limit.")
        os.replace(temporary_path, destination_path)
        return summary
    except Exception:
        shutil.rmtree(temporary_path, ignore_errors=True)
        raise


def iter_epub_text_entries(
    epub_path: str | os.PathLike[str],
    limits: EpubLimits,
) -> Iterator[tuple[str, str]]:
    """Yield one bounded HTML entry at a time from a validated EPUB."""

    with zipfile.ZipFile(epub_path, "r") as archive:
        _, entries = _inspect_archive(archive, limits)
        for entry in entries:
            if entry.path.casefold().endswith(HTML_SUFFIXES):
                content = _read_entry_limited(
                    archive, entry.info, limits.max_text_entry_bytes
                )
                yield entry.path, content.decode("utf-8", errors="ignore")


def _clone_zip_info(source: zipfile.ZipInfo, filename: str) -> zipfile.ZipInfo:
    target = zipfile.ZipInfo(filename=filename, date_time=source.date_time)
    target.compress_type = source.compress_type
    target.comment = source.comment
    target.extra = source.extra
    target.create_system = source.create_system
    target.create_version = source.create_version
    target.extract_version = source.extract_version
    target.external_attr = source.external_attr
    target.internal_attr = source.internal_attr
    target.flag_bits = source.flag_bits & ~0x1
    return target


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.casefold()
    return {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
    }.get(suffix, "image/jpeg")


def _manifest_entry(identifier: str, href: str, media_type: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", identifier)
    safe_href = (
        href.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f'    <item id="{safe_id}" href="{safe_href}" media-type="{media_type}"/>'


def package_epub_streaming(
    base_epub: str | os.PathLike[str],
    output_epub: str | os.PathLike[str],
    *,
    injected_files: Mapping[str, str | os.PathLike[str]],
    url_map: Mapping[str, str],
    limits: EpubLimits,
    max_output_bytes: int,
    progress_callback: Callable[[int, int], None] | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> EpubArchiveSummary:
    """Rewrite bounded text entries while streaming every other ZIP entry."""

    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    output_path = os.path.abspath(os.fspath(output_epub))
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    temporary_path = f"{output_path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"

    def checkpoint() -> None:
        if cancellation_check is not None:
            cancellation_check()

    normalized_injections: dict[str, tuple[str, str]] = {}
    injected_total = 0
    for raw_target, raw_source in injected_files.items():
        target, key = normalize_archive_path(raw_target)
        source_path = os.path.abspath(os.fspath(raw_source))
        if key in normalized_injections:
            raise EpubSafetyError("Injected EPUB files contain colliding paths.")
        if os.path.islink(source_path) or not os.path.isfile(source_path):
            raise EpubSafetyError("An injected EPUB file is missing or unsafe.")
        size = os.path.getsize(source_path)
        if size > limits.max_entry_bytes:
            raise EpubSafetyError("An injected EPUB file is oversized.")
        injected_total += size
        if injected_total > limits.max_total_uncompressed_bytes:
            raise EpubSafetyError("Injected EPUB files exceed the archive size limit.")
        normalized_injections[key] = (target, source_path)

    try:
        checkpoint()
        with zipfile.ZipFile(base_epub, "r") as source_archive:
            source_summary, entries = _inspect_archive(source_archive, limits)
            by_key = {entry.key: entry for entry in entries}
            replacement_keys = set(normalized_injections).intersection(by_key)
            resulting_entries = len(entries) + len(normalized_injections) - len(replacement_keys)
            if resulting_entries > limits.max_entries:
                raise EpubSafetyError("The packaged EPUB would contain too many entries.")
            replaced_size = sum(
                max(0, int(by_key[key].info.file_size or 0))
                for key in replacement_keys
                if not by_key[key].info.is_dir()
            )
            if (
                source_summary.total_uncompressed_bytes
                - replaced_size
                + injected_total
                > limits.max_total_uncompressed_bytes
            ):
                raise EpubSafetyError("The packaged EPUB exceeds its decompression limit.")

            opf_path = source_summary.opf_path
            opf_key = unicodedata.normalize("NFC", opf_path).casefold()
            opf_dir = posixpath.dirname(opf_path)
            manifest_additions = []
            for index, (target, _source_path) in enumerate(
                sorted(normalized_injections.values()), start=1
            ):
                href = posixpath.relpath(target, opf_dir or ".")
                manifest_additions.append(
                    _manifest_entry(f"arcdb_image_{index}", href, _media_type(target))
                )

            mimetype_entry = by_key["mimetype"]
            filename_targets = {
                posixpath.basename(target): target
                for target, _source_path in normalized_injections.values()
            }
            total_steps = max(1, len(entries) + len(normalized_injections))
            completed_steps = 0
            with open(temporary_path, "xb") as raw_output:
                bounded_output = _BoundedWriter(raw_output, max_output_bytes)
                with zipfile.ZipFile(bounded_output, "w", allowZip64=True) as output_archive:
                    mimetype_bytes = _read_entry_limited(
                        source_archive, mimetype_entry.info, 256
                    )
                    output_archive.writestr(
                        "mimetype", mimetype_bytes, compress_type=zipfile.ZIP_STORED
                    )
                    completed_steps += 1
                    if progress_callback is not None:
                        progress_callback(completed_steps, total_steps)

                    for entry in entries:
                        checkpoint()
                        if entry.key == "mimetype" or entry.key in normalized_injections:
                            continue
                        target_name = entry.path + "/" if entry.info.is_dir() else entry.path
                        target_info = _clone_zip_info(entry.info, target_name)
                        if entry.info.is_dir():
                            output_archive.writestr(target_info, b"")
                            completed_steps += 1
                            if progress_callback is not None:
                                progress_callback(completed_steps, total_steps)
                            continue

                        is_text = entry.path.casefold().endswith(HTML_SUFFIXES)
                        if is_text or (entry.key == opf_key and manifest_additions):
                            original_bytes = _read_entry_limited(
                                source_archive,
                                entry.info,
                                limits.max_text_entry_bytes,
                            )
                            text = original_bytes.decode("utf-8", errors="ignore")
                            changed = False
                            if is_text:
                                for original_url, safe_filename in url_map.items():
                                    target_path = filename_targets.get(safe_filename, "")
                                    if original_url and target_path and original_url in text:
                                        relative = posixpath.relpath(
                                            target_path,
                                            posixpath.dirname(entry.path) or ".",
                                        )
                                        text = text.replace(original_url, relative)
                                        changed = True
                            if entry.key == opf_key and manifest_additions:
                                existing_hrefs = set(
                                    re.findall(r'href=["\']([^"\']+)["\']', text)
                                )
                                additions = []
                                for addition in manifest_additions:
                                    match = re.search(r'href="([^"]+)"', addition)
                                    if match and match.group(1) not in existing_hrefs:
                                        additions.append(addition)
                                        existing_hrefs.add(match.group(1))
                                marker = text.find("</manifest>")
                                if marker >= 0 and additions:
                                    text = (
                                        text[:marker]
                                        + "\n".join(additions)
                                        + "\n  "
                                        + text[marker:]
                                    )
                                    changed = True
                            encoded = text.encode("utf-8") if changed else original_bytes
                            if len(encoded) > limits.max_text_entry_bytes:
                                raise EpubSafetyError(
                                    "A rewritten EPUB text entry exceeds its size limit."
                                )
                            output_archive.writestr(target_info, encoded)
                            completed_steps += 1
                            if progress_callback is not None:
                                progress_callback(completed_steps, total_steps)
                            continue

                        with source_archive.open(entry.info, "r") as source, output_archive.open(
                            target_info, "w", force_zip64=True
                        ) as destination:
                            _copy_bounded_stream(
                                source,
                                destination,
                                limits.max_entry_bytes,
                                checkpoint,
                            )
                        completed_steps += 1
                        if progress_callback is not None:
                            progress_callback(completed_steps, total_steps)

                    for target, source_path in sorted(normalized_injections.values()):
                        checkpoint()
                        target_info = zipfile.ZipInfo(target)
                        target_info.compress_type = zipfile.ZIP_DEFLATED
                        target_info.external_attr = 0o100644 << 16
                        with open(source_path, "rb") as source, output_archive.open(
                            target_info, "w", force_zip64=True
                        ) as destination:
                            _copy_bounded_stream(
                                source,
                                destination,
                                limits.max_entry_bytes,
                                checkpoint,
                            )
                        completed_steps += 1
                        if progress_callback is not None:
                            progress_callback(completed_steps, total_steps)
                raw_output.flush()
                os.fsync(raw_output.fileno())

        if os.path.getsize(temporary_path) > max_output_bytes:
            raise EpubSafetyError("The packaged EPUB exceeds the session output limit.")
        checkpoint()
        result = validate_epub_archive(temporary_path, limits, checkpoint)
        checkpoint()
        os.replace(temporary_path, output_path)
        return result
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        NotImplementedError,
        RuntimeError,
        EOFError,
        OSError,
    ) as exc:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass
        raise EpubSafetyError("The EPUB could not be processed safely.") from exc
    except Exception:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass
        raise
