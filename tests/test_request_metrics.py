from __future__ import annotations

import unittest

from flask import Flask

from arcdb.request_metrics import (
    observe_request_component,
    request_component_timings,
    reset_request_component_timings,
)


class RequestComponentMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__)

    def test_accumulates_known_components_only_inside_request(self) -> None:
        self.assertEqual(request_component_timings(), {})
        with self.app.test_request_context("/"):
            reset_request_component_timings()
            with observe_request_component("sqlite"):
                pass
            with observe_request_component("sqlite"):
                pass
            with observe_request_component("filesystem"):
                pass
            timings = request_component_timings()
            self.assertEqual(set(timings), {"sqlite", "filesystem"})
            self.assertGreaterEqual(timings["sqlite"], 0.0)

    def test_rejects_unknown_component(self) -> None:
        with self.assertRaises(ValueError):
            with observe_request_component("payload"):
                pass

    def test_does_not_suppress_exceptions_outside_request(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with observe_request_component("filesystem"):
                raise RuntimeError("boom")


if __name__ == "__main__":
    unittest.main()
