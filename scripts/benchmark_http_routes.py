#!/usr/bin/env python3
"""Benchmark seeded ArchiveDB HTTP routes on a loopback-only server."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.http_benchmark import (  # noqa: E402
    BenchmarkSample,
    SAFE_PROFILE_RE,
    build_sanitized_report,
    normalize_loopback_base_url,
    parse_server_timing,
)


WORKLOAD = (
    ("library_page", "/api/library", "/api/library", "POST", {"page": 1, "limit": 50}),
    (
        "library_search",
        "/api/library",
        "/api/library",
        "POST",
        {"page": 1, "limit": 25, "search": "fixture", "sortBy": "title", "sortOrder": "asc"},
    ),
    ("novel_detail", "/api/novel/{novel_id}", "/api/novel/<path:novel_id>", "GET", None),
    ("reader_page", "/read/{novel_id}", "/read/<novel_id>", "GET", None),
    (
        "reader_chapter",
        "/api/read/{novel_id}/chapter/{chapter_path}",
        "/api/read/<novel_id>/chapter/<path:chap_path>",
        "GET",
        None,
    ),
    ("collections", "/api/collections", "/api/collections", "GET", None),
    ("community", "/api/community/overview", "/api/community/overview", "GET", None),
)


class LocalBenchmarkClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookies)
        )

    def request(self, path: str, method: str, payload: dict | None = None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers.update(
                {"Content-Type": "application/json", "Origin": self.base_url}
            )
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        started = time.perf_counter()
        with self.opener.open(request, timeout=60) as response:
            response.read()
            duration_ms = (time.perf_counter() - started) * 1000.0
            return (
                response.status,
                duration_ms,
                parse_server_timing(response.headers.get("Server-Timing")),
            )

    def login(self, email: str, password: str) -> None:
        data = urllib.parse.urlencode({"email": email, "password": password}).encode()
        request = urllib.request.Request(
            self.base_url + "/login",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.base_url,
            },
            method="POST",
        )
        with self.opener.open(request, timeout=30) as response:
            response.read()
            if response.status != 200:
                raise RuntimeError("Benchmark login failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5004")
    parser.add_argument("--email", default="dev@arcdb.local")
    parser.add_argument("--novel-id", default="422601")
    parser.add_argument("--chapter-path", default="OEBPS/chapter1.xhtml")
    parser.add_argument("--profile", default="local-legacy")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--output")
    args = parser.parse_args()

    try:
        args.base_url = normalize_loopback_base_url(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))
    if not SAFE_PROFILE_RE.fullmatch(args.profile):
        parser.error("--profile must be a short lowercase label")
    if args.warmups < 0 or args.repetitions < 3:
        parser.error("--warmups must be non-negative and --repetitions at least 3")
    password = os.environ.get("ARCHIVEDB_BENCHMARK_PASSWORD", "")
    if not password:
        parser.error("ARCHIVEDB_BENCHMARK_PASSWORD is required")

    client = LocalBenchmarkClient(args.base_url)
    client.login(args.email, password)
    samples: list[BenchmarkSample] = []
    for scenario, path_template, route, method, payload in WORKLOAD:
        path = path_template.format(
            novel_id=urllib.parse.quote(args.novel_id, safe=""),
            chapter_path=urllib.parse.quote(args.chapter_path, safe="/"),
        )
        for _ in range(args.warmups):
            status, _duration, _components = client.request(path, method, payload)
            if status != 200:
                raise RuntimeError(f"Warmup failed for scenario {scenario}")
        for _ in range(args.repetitions):
            status, duration, components = client.request(path, method, payload)
            if status != 200:
                raise RuntimeError(f"Benchmark failed for scenario {scenario}")
            samples.append(
                BenchmarkSample(
                    scenario=scenario,
                    route=route,
                    method=method,
                    status=status,
                    duration_ms=duration,
                    components_ms=components,
                )
            )

    report = build_sanitized_report(
        samples,
        profile=args.profile,
        repetitions=args.repetitions,
        warmups=args.warmups,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        if output.exists():
            parser.error("--output must be a new path")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
