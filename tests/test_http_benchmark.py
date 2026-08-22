from __future__ import annotations

import unittest

from arcdb.http_benchmark import (
    BenchmarkSample,
    build_sanitized_report,
    normalize_loopback_base_url,
    parse_server_timing,
)


class HttpBenchmarkTests(unittest.TestCase):
    def test_parses_only_bounded_server_timing_components(self) -> None:
        self.assertEqual(
            parse_server_timing(
                "sqlite;dur=1.250, filesystem;dur=2, private_user;dur=99, job;desc=x;dur=4"
            ),
            {"sqlite": 1.25, "filesystem": 2.0},
        )

    def test_builds_path_and_identity_free_percentile_report(self) -> None:
        samples = [
            BenchmarkSample(
                scenario="library_page",
                route="/api/library",
                method="POST",
                status=200,
                duration_ms=duration,
                components_ms={"sqlite": duration / 2},
            )
            for duration in (1.0, 10.0, 20.0)
        ]
        report = build_sanitized_report(
            samples,
            profile="local-legacy",
            repetitions=3,
            warmups=1,
        )
        self.assertEqual(report["samples"], 3)
        scenario = report["scenarios"][0]
        self.assertEqual(
            scenario["duration_ms"], {"p50": 10.0, "p95": 20.0, "p99": 20.0}
        )
        self.assertEqual(scenario["components_ms"]["sqlite"]["count"], 3)
        encoded = str(report)
        self.assertNotIn("novel_id", encoded)
        self.assertNotIn("email", encoded)
        self.assertNotIn("base_url", encoded)

    def test_rejects_unbounded_profile_labels(self) -> None:
        with self.assertRaises(ValueError):
            build_sanitized_report(
                [], profile="Production / private path", repetitions=3, warmups=0
            )

    def test_accepts_only_credential_free_loopback_origin(self) -> None:
        self.assertEqual(
            normalize_loopback_base_url("http://127.0.0.1:5004/"),
            "http://127.0.0.1:5004",
        )
        for value in (
            "https://127.0.0.1:5004",
            "http://example.test:5004",
            "http://user:secret@127.0.0.1:5004",
            "http://127.0.0.1:5004/private/path",
            "http://127.0.0.1",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_loopback_base_url(value)


if __name__ == "__main__":
    unittest.main()
