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
    r"duration_ms=(?P<duration>[0-9]+(?:\.[0-9]+)?)"
    r"(?P<components>(?: (?:sqlite|filesystem|epub|job)_ms="
    r"[0-9]+(?:\.[0-9]+)?){0,4})$"
)

COMPONENT_EVENT_RE = re.compile(
    r"(?P<name>sqlite|filesystem|epub|job)_ms="
    r"(?P<duration>[0-9]+(?:\.[0-9]+)?)"
)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("Cannot calculate a percentile without samples")
    ordered = sorted(values)
    position = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[position], 3)


def summarize_request_events(lines: Iterable[str]) -> dict:
    groups: dict[tuple[str, str, int], dict] = defaultdict(
        lambda: {"duration": [], "components": defaultdict(list)}
    )
    for raw in lines:
        match = REQUEST_EVENT_RE.fullmatch(raw.strip())
        if match is None:
            continue
        key = (
            match.group("route"),
            match.group("method"),
            int(match.group("status")),
        )
        groups[key]["duration"].append(float(match.group("duration")))
        for component_match in COMPONENT_EVENT_RE.finditer(match.group("components")):
            groups[key]["components"][component_match.group("name")].append(
                float(component_match.group("duration"))
            )

    routes = []
    for (route, method, status), measurements in sorted(groups.items()):
        values = measurements["duration"]
        record = {
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
        components = {}
        for name, component_values in sorted(measurements["components"].items()):
            components[name] = {
                "count": len(component_values),
                "p50": _percentile(component_values, 0.50),
                "p95": _percentile(component_values, 0.95),
                "p99": _percentile(component_values, 0.99),
            }
        if components:
            record["components_ms"] = components
        routes.append(record)
    return {
        "status": "ok" if routes else "no_samples",
        "samples": sum(len(values["duration"]) for values in groups.values()),
        "routes": routes,
    }
