from __future__ import annotations

import unittest

from arcdb.security import (
    OriginConfigurationError,
    canonical_origin,
    parse_allowed_origins,
    request_source_allowed,
)


class SameOriginTests(unittest.TestCase):
    def test_canonical_origin_and_configuration(self) -> None:
        self.assertEqual(canonical_origin("HTTPS://Example.COM:443/path"), "https://example.com")
        self.assertEqual(canonical_origin("http://[::1]:5004/"), "http://[::1]:5004")
        self.assertIsNone(canonical_origin("javascript:alert(1)"))
        self.assertIsNone(canonical_origin("https://user@example.com"))
        self.assertEqual(
            parse_allowed_origins("https://example.com, http://127.0.0.1:5004"),
            frozenset({"https://example.com", "http://127.0.0.1:5004"}),
        )
        with self.assertRaises(OriginConfigurationError):
            parse_allowed_origins("https://example.com/path")

    def test_explicit_allowlist_is_exact_and_rejects_missing_sources(self) -> None:
        allowed = frozenset({"https://library.example"})
        self.assertTrue(request_source_allowed(
            origin="https://library.example", referer=None,
            host="internal:5004", allowed_origins=allowed,
        ))
        for origin in (None, "null", "https://evil.example", "https://library.example.evil"):
            self.assertFalse(request_source_allowed(
                origin=origin, referer=None,
                host="library.example", allowed_origins=allowed,
            ))

    def test_referer_fallback_and_local_host_mode(self) -> None:
        self.assertTrue(request_source_allowed(
            origin=None, referer="http://127.0.0.1:5004/library?page=1",
            host="127.0.0.1:5004", allowed_origins=frozenset(),
        ))
        self.assertTrue(request_source_allowed(
            origin="https://library.example", referer=None,
            host="library.example", allowed_origins=frozenset(),
        ))
        self.assertFalse(request_source_allowed(
            origin="https://evil.example", referer="https://library.example/",
            host="library.example", allowed_origins=frozenset(),
        ))


if __name__ == "__main__":
    unittest.main()
