"""Bounded, payload-free component timings for the active Flask request."""

from __future__ import annotations

from contextlib import contextmanager
import time
from typing import Iterator

from flask import g, has_request_context


COMPONENT_NAMES = ("sqlite", "filesystem", "epub", "job")


def reset_request_component_timings() -> None:
    """Initialize timing storage for the current request."""

    if has_request_context():
        g.arcdb_component_timings_ms = {}


@contextmanager
def observe_request_component(name: str) -> Iterator[None]:
    """Accumulate one known component duration without recording payload context."""

    if name not in COMPONENT_NAMES:
        raise ValueError(f"Unknown request timing component: {name}")
    started = time.perf_counter()
    try:
        yield
    finally:
        if has_request_context():
            elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
            timings = getattr(g, "arcdb_component_timings_ms", None)
            if not isinstance(timings, dict):
                timings = {}
                g.arcdb_component_timings_ms = timings
            timings[name] = float(timings.get(name, 0.0)) + elapsed_ms


def request_component_timings() -> dict[str, float]:
    """Return known positive timings for the current request."""

    if not has_request_context():
        return {}
    timings = getattr(g, "arcdb_component_timings_ms", {})
    if not isinstance(timings, dict):
        return {}
    return {
        name: max(0.0, float(timings[name]))
        for name in COMPONENT_NAMES
        if name in timings
    }
