from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SHADOW_DOMAINS = (
    "users",
    "user_data",
    "collections",
    "user_uploads",
    "custom_meta",
    "allowed_emails",
)

_EVENT_MARKER = "[STATE-READ-SHADOW]"
_EVENT_PATTERN = re.compile(
    r"\[STATE-READ-SHADOW\] "
    r"event=(match|mismatch|error) "
    r"domain=([a-z_]+) "
    r"count=([1-9][0-9]*)"
    r"(?: error_type=([A-Za-z_][A-Za-z0-9_.]*))?"
)


class ObservationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def verify_shadow_observation(
    log_path: Path,
    *,
    minimum_reported_matches: int = 1,
) -> dict[str, Any]:
    """Verify one process-local shadow-observation log without exporting payloads."""
    if minimum_reported_matches < 1:
        raise ObservationError(
            "invalid_minimum", "Minimum reported matches must be a positive integer."
        )

    source = log_path.expanduser().resolve()
    if not source.is_file():
        raise ObservationError(
            "log_missing", "Explicit shadow-observation log is missing."
        )

    domains = {
        domain: {"match_events": 0, "last_reported_match_count": 0}
        for domain in SHADOW_DOMAINS
    }
    parsed_events = 0
    try:
        with source.open("r", encoding="utf-8", errors="strict") as handle:
            for line in handle:
                marker_at = line.find(_EVENT_MARKER)
                if marker_at < 0:
                    continue
                event_text = line[marker_at:].strip()
                match = _EVENT_PATTERN.fullmatch(event_text)
                if match is None:
                    raise ObservationError(
                        "event_invalid", "A shadow-observation event is malformed."
                    )

                event, domain, raw_count, error_type = match.groups()
                if domain not in domains:
                    raise ObservationError(
                        "domain_unknown", "A shadow-observation event has an unknown domain."
                    )
                if event == "error" and error_type is None:
                    raise ObservationError(
                        "event_invalid", "A shadow error event is missing its error type."
                    )
                if event != "error" and error_type is not None:
                    raise ObservationError(
                        "event_invalid", "A non-error shadow event has an error type."
                    )

                parsed_events += 1
                if event != "match":
                    raise ObservationError(
                        f"shadow_{event}",
                        f"Shadow observation recorded {event} for 1 domain.",
                    )

                count = int(raw_count)
                previous = domains[domain]["last_reported_match_count"]
                if count <= previous:
                    raise ObservationError(
                        "counter_invalid",
                        "A shadow match counter did not increase within the observation boundary.",
                    )
                domains[domain]["match_events"] += 1
                domains[domain]["last_reported_match_count"] = count
    except ObservationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ObservationError(
            "log_unreadable", "Shadow-observation log could not be read as UTF-8."
        ) from exc

    missing = sum(
        1
        for values in domains.values()
        if values["last_reported_match_count"] < minimum_reported_matches
    )
    if missing:
        raise ObservationError(
            "coverage_incomplete",
            f"Shadow observation lacks required match coverage for {missing} domain(s).",
        )

    return {
        "format": "archivedb-read-shadow-observation-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "observation_passed",
        "parsed_events": parsed_events,
        "minimum_reported_matches": minimum_reported_matches,
        "domains": domains,
        "decision": {
            "observation_gate_passed": True,
            "primary_read_authorized": False,
            "next_step": "operator_review_then_bounded_sqlite_canary",
        },
    }
