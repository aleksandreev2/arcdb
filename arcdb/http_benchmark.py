"""Local HTTP workload measurement and path-free report helpers."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Iterable, Mapping
from urllib.parse import urlsplit


SAFE_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
SERVER_TIMING_RE = re.compile(
    r"^(?P<name>sqlite|filesystem|epub|job);dur="
    r"(?P<duration>[0-9]+(?:\.[0-9]+)?)$"
)


@dataclass(frozen=True)
class BenchmarkSample:
    scenario: str
    route: str
    method: str
    status: int
    duration_ms: float
    components_ms: Mapping[str, float]


def normalize_loopback_base_url(value: str) -> str:
    """Validate and normalize a credential-free loopback HTTP origin."""

    parsed = urlsplit(str(value or ""))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base URL has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or port is None
    ):
        raise ValueError("base URL must be a credential-free loopback HTTP origin")
    return value.rstrip("/")


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("Cannot calculate a percentile without samples")
    position = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[position], 3)


def parse_server_timing(value: str | None) -> dict[str, float]:
    """Parse only ArchiveDB's bounded component names from Server-Timing."""

    result: dict[str, float] = {}
    if not value:
        return result
    for raw_part in value.split(","):
        match = SERVER_TIMING_RE.fullmatch(raw_part.strip())
        if match is None:
            continue
        result[match.group("name")] = float(match.group("duration"))
    return result


def _summary(values: Iterable[float]) -> dict[str, float]:
    materialized = list(values)
    return {
        "p50": percentile(materialized, 0.50),
        "p95": percentile(materialized, 0.95),
        "p99": percentile(materialized, 0.99),
    }


def build_sanitized_report(
    samples: Iterable[BenchmarkSample],
    *,
    profile: str,
    repetitions: int,
    warmups: int,
) -> dict:
    """Aggregate samples without URLs, request IDs, credentials or payloads."""

    if not SAFE_PROFILE_RE.fullmatch(profile):
        raise ValueError("profile must be a short lowercase label")
    grouped: dict[tuple[str, str, str, int], list[BenchmarkSample]] = {}
    for sample in samples:
        key = (sample.scenario, sample.route, sample.method, sample.status)
        grouped.setdefault(key, []).append(sample)
    scenarios = []
    for (scenario, route, method, status), values in sorted(grouped.items()):
        components = {}
        component_names = sorted(
            {name for sample in values for name in sample.components_ms}
        )
        for name in component_names:
            durations = [
                sample.components_ms[name]
                for sample in values
                if name in sample.components_ms
            ]
            components[name] = {"count": len(durations), **_summary(durations)}
        record = {
            "scenario": scenario,
            "route": route,
            "method": method,
            "status": status,
            "count": len(values),
            "duration_ms": _summary(sample.duration_ms for sample in values),
        }
        if components:
            record["components_ms"] = components
        scenarios.append(record)
    return {
        "status": "ok" if scenarios else "no_samples",
        "profile": profile,
        "repetitions": repetitions,
        "warmups": warmups,
        "samples": sum(len(values) for values in grouped.values()),
        "scenarios": scenarios,
    }
