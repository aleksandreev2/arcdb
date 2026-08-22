"""Payload-free request timing aggregation."""

from __future__ import annotations

from collections import defaultdict
import math
import re
from typing import Iterable


REQUEST_EVENT_RE = re.compile(
    r"^\[REQUEST\] request_id=[0-9a-f]{32} "
    r"route=(?P<route>\S+) method=(?P<method>[A-Z]+) "
    r"status=(?P<status>[1-5][0-9]{2}) "
    r"duration_ms=(?P<duration>[0-9]+(?:\.[0-9]+)?)$"
)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("Cannot calculate a percentile without samples")
    ordered = sorted(values)
    position = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[position], 3)


def summarize_request_events(lines: Iterable[str]) -> dict:
    groups: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for raw in lines:
        match = REQUEST_EVENT_RE.fullmatch(raw.strip())
        if match is None:
            continue
        key = (
            match.group("route"),
            match.group("method"),
            int(match.group("status")),
        )
        groups[key].append(float(match.group("duration")))

    routes = []
    for (route, method, status), values in sorted(groups.items()):
        routes.append(
            {
                "route": route,
                "method": method,
                "status": status,
                "count": len(values),
                "duration_ms": {
                    "p50": _percentile(values, 0.50),
                    "p95": _percentile(values, 0.95),
                    "p99": _percentile(values, 0.99),
                },
            }
        )
    return {
        "status": "ok" if routes else "no_samples",
        "samples": sum(len(values) for values in groups.values()),
        "routes": routes,
    }
