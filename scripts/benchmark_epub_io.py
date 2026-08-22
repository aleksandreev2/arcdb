from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import tracemalloc
import zipfile

from arcdb.epub_io import EpubLimits, package_epub_streaming


MIB = 1024 * 1024
CONTAINER = '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
OPF = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata/><manifest><item id="c" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/></spine></package>'''


def make_fixture(path: Path, payload_bytes: int) -> None:
    with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
        archive.writestr(
            "mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED
        )
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr("OEBPS/chapter.xhtml", "<html><body>benchmark</body></html>")
        info = zipfile.ZipInfo("OEBPS/payload.bin")
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w", force_zip64=True) as output:
            remaining = payload_bytes
            while remaining:
                chunk = os.urandom(min(MIB, remaining))
                output.write(chunk)
                remaining -= len(chunk)


def legacy_materialize_package(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source, "r") as input_archive:
        entries = {name: input_archive.read(name) for name in input_archive.namelist()}
    with zipfile.ZipFile(destination, "w", allowZip64=True) as output_archive:
        output_archive.writestr(
            "mimetype", entries.pop("mimetype"), compress_type=zipfile.ZIP_STORED
        )
        for name, payload in entries.items():
            output_archive.writestr(name, payload, compress_type=zipfile.ZIP_DEFLATED)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def measure(operation, repetitions: int) -> dict[str, float]:
    durations: list[float] = []
    peaks: list[float] = []
    for _ in range(repetitions):
        tracemalloc.start()
        started = time.perf_counter()
        operation()
        durations.append((time.perf_counter() - started) * 1000)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peaks.append(peak / MIB)
    return {
        "duration_ms_p50": round(percentile(durations, 0.50), 3),
        "duration_ms_p95": round(percentile(durations, 0.95), 3),
        "duration_ms_p99": round(percentile(durations, 0.99), 3),
        "python_peak_mib_p50": round(percentile(peaks, 0.50), 3),
        "python_peak_mib_p95": round(percentile(peaks, 0.95), 3),
        "python_peak_mib_p99": round(percentile(peaks, 0.99), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare the removed whole-archive packaging pattern with streaming I/O."
    )
    parser.add_argument("--payload-mib", type=int, default=32)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.payload_mib <= 0 or args.repetitions < 3:
        parser.error("payload-mib must be positive and repetitions must be at least 3")

    with tempfile.TemporaryDirectory(prefix="arcdb-epub-benchmark-") as tmp:
        root = Path(tmp)
        source = root / "source.epub"
        legacy_output = root / "legacy.epub"
        streaming_output = root / "streaming.epub"
        make_fixture(source, args.payload_mib * MIB)
        archive_limits = EpubLimits(
            max_entries=100,
            max_entry_bytes=(args.payload_mib + 8) * MIB,
            max_total_uncompressed_bytes=(args.payload_mib + 16) * MIB,
            max_text_entry_bytes=MIB,
        )
        result = {
            "payload_mib": args.payload_mib,
            "repetitions": args.repetitions,
            "legacy_materialize": measure(
                lambda: legacy_materialize_package(source, legacy_output),
                args.repetitions,
            ),
            "streaming": measure(
                lambda: package_epub_streaming(
                    source,
                    streaming_output,
                    injected_files={},
                    url_map={},
                    limits=archive_limits,
                    max_output_bytes=(args.payload_mib + 16) * MIB,
                ),
                args.repetitions,
            ),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
