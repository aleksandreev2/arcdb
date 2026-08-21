from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arcdb.storage.read_observation import (
    SHADOW_DOMAINS,
    ObservationError,
    verify_shadow_observation,
)
from scripts.verify_read_shadow_observation import write_report


def _events(*, count: int = 1) -> str:
    return "".join(
        f"[STATE-READ-SHADOW] event=match domain={domain} count={count}\n"
        for domain in SHADOW_DOMAINS
    )


class ReadObservationTests(unittest.TestCase):
    def test_complete_observation_is_payload_and_path_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "secret-reader@example.test reset-token-value"
            log = root / "private-server.log"
            log.write_text(
                secret + "\n" + _events(count=1) + _events(count=25),
                encoding="utf-8",
            )

            report = verify_shadow_observation(
                log, minimum_reported_matches=25
            )

            self.assertEqual(report["status"], "observation_passed")
            self.assertEqual(report["parsed_events"], len(SHADOW_DOMAINS) * 2)
            self.assertTrue(
                all(values["match_events"] == 2 for values in report["domains"].values())
            )
            self.assertTrue(report["decision"]["observation_gate_passed"])
            self.assertFalse(report["decision"]["primary_read_authorized"])
            encoded = json.dumps(report)
            self.assertNotIn(secret, encoded)
            self.assertNotIn(str(log), encoded)

    def test_mismatch_and_error_fail_without_exposing_sensitive_lines(self) -> None:
        cases = (
            ("mismatch", "", "shadow_mismatch"),
            ("error", " error_type=StateReadError", "shadow_error"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for event, suffix, expected_code in cases:
                log = root / f"{event}.log"
                log.write_text(
                    "private@example.test token-value\n"
                    f"[STATE-READ-SHADOW] event={event} domain=users count=1{suffix}\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ObservationError) as raised:
                    verify_shadow_observation(log)
                self.assertEqual(raised.exception.code, expected_code)
                self.assertNotIn("private@example.test", str(raised.exception))
                self.assertNotIn("token-value", str(raised.exception))

    def test_missing_domain_and_minimum_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server.log"
            log.write_text(
                _events(count=9).replace(
                    "[STATE-READ-SHADOW] event=match domain=users count=9\n", ""
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ObservationError) as raised:
                verify_shadow_observation(log, minimum_reported_matches=10)
            self.assertEqual(raised.exception.code, "coverage_incomplete")

    def test_unknown_malformed_and_non_increasing_events_fail_closed(self) -> None:
        cases = (
            (
                "[STATE-READ-SHADOW] event=match domain=future_domain count=1\n",
                "domain_unknown",
            ),
            ("[STATE-READ-SHADOW] event=match domain=users count=0\n", "event_invalid"),
            (
                "[STATE-READ-SHADOW] event=match domain=users count=2\n"
                "[STATE-READ-SHADOW] event=match domain=users count=1\n",
                "counter_invalid",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, (contents, expected_code) in enumerate(cases):
                log = root / f"case-{index}.log"
                log.write_text(contents, encoding="utf-8")
                with self.assertRaises(ObservationError) as raised:
                    verify_shadow_observation(log)
                self.assertEqual(raised.exception.code, expected_code)

    def test_missing_log_and_invalid_minimum_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.log"
            with self.assertRaises(ObservationError) as raised:
                verify_shadow_observation(missing)
            self.assertEqual(raised.exception.code, "log_missing")
            missing.write_text(_events(), encoding="utf-8")
            with self.assertRaises(ObservationError) as raised:
                verify_shadow_observation(missing, minimum_reported_matches=0)
            self.assertEqual(raised.exception.code, "invalid_minimum")

    def test_report_writer_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "observation.json"
            report = {"status": "observation_passed"}
            write_report(report_path, report)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)
            with self.assertRaises(ObservationError) as raised:
                write_report(report_path, report)
            self.assertEqual(raised.exception.code, "report_exists")


if __name__ == "__main__":
    unittest.main()
