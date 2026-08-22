from __future__ import annotations

import unittest

from arcdb.observability import summarize_request_events


class RequestObservabilityTests(unittest.TestCase):
    def test_summarizes_route_timings_without_request_ids(self) -> None:
        lines = [
            "[REQUEST] request_id=" + "a" * 32
            + " route=/api/library method=POST status=200 duration_ms=1.000",
            "[REQUEST] request_id=" + "b" * 32
            + " route=/api/library method=POST status=200 duration_ms=10.000",
            "[REQUEST] request_id=" + "c" * 32
            + " route=/api/library method=POST status=200 duration_ms=20.000",
            "[ACCESS] email=private@example.test ip=192.0.2.1",
            "payload=super-secret-token",
        ]
        report = summarize_request_events(lines)
        self.assertEqual(report["samples"], 3)
        self.assertEqual(report["routes"], [
            {
                "route": "/api/library",
                "method": "POST",
                "status": 200,
                "count": 3,
                "duration_ms": {"p50": 10.0, "p95": 20.0, "p99": 20.0},
            }
        ])
        self.assertNotIn("request_id", str(report))
        self.assertNotIn("private@example.test", str(report))
        self.assertNotIn("super-secret-token", str(report))

    def test_ignores_malformed_or_unbounded_labels(self) -> None:
        report = summarize_request_events([
            "[REQUEST] request_id=bad route=/x method=GET status=200 duration_ms=1",
            "[REQUEST] request_id=" + "d" * 32
            + " route=/user/identity with-space method=GET status=200 duration_ms=1",
        ])
        self.assertEqual(report, {"status": "no_samples", "samples": 0, "routes": []})


if __name__ == "__main__":
    unittest.main()
