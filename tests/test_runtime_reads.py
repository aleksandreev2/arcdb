from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from arcdb.storage.legacy_import import replace_from_documents
from arcdb.storage.runtime_reads import (
    StateReadComparisonError,
    StateReadError,
    read_allowed_emails,
    read_collections,
    read_custom_meta,
    state_read_metrics,
    read_user_data,
    read_user_uploads,
    read_users,
)
from arcdb.storage.sqlite_db import connect_db, initialize_schema


class RuntimeReadBackendTests(unittest.TestCase):
    def _seed(self, db: Path) -> dict:
        docs = {
            "users": {"reader@example.test": {"verified": True, "future": 1}},
            "user_data": {"empty@example.test": {}, "reader@example.test": {"42": {"progress": 7}}},
            "collections": {"empty@example.test": [], "reader@example.test": [{"id": "x", "name": "X"}]},
            "user_uploads": {"u1": {"approved": True, "future": {"x": 1}}},
            "custom_meta": {"book.epub": {"title": "Fixture"}},
            "allowed_emails": ["reader@example.test"],
        }
        conn = connect_db(db)
        try:
            initialize_schema(conn)
            replace_from_documents(conn, **docs)
        finally:
            conn.close()
        return docs

    def test_legacy_default_never_opens_sqlite(self) -> None:
        loader = Mock(return_value={"legacy": True})
        with patch.dict(
            os.environ,
            {"STATE_READ_BACKEND": "legacy", "STATE_READ_SHADOW_COMPARE": "0"},
            clear=False,
        ):
            self.assertEqual(read_users(loader), {"legacy": True})
        loader.assert_called_once_with()

    def test_sqlite_exports_every_state_domain_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            docs = self._seed(db)
            loader = Mock(side_effect=AssertionError("legacy loader used"))
            env = {
                "STATE_READ_BACKEND": "sqlite",
                "STATE_READ_SHADOW_COMPARE": "0",
                "SQLITE_DB_PATH": str(db),
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(read_users(loader), docs["users"])
                self.assertEqual(read_user_data(loader), docs["user_data"])
                self.assertEqual(read_collections(loader), docs["collections"])
                self.assertEqual(read_user_uploads(loader), docs["user_uploads"])
                self.assertEqual(read_custom_meta(loader), docs["custom_meta"])
                self.assertEqual(
                    read_allowed_emails(loader), set(docs["allowed_emails"])
                )
            loader.assert_not_called()

    def test_invalid_missing_and_stale_sqlite_fail_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"STATE_READ_BACKEND": "invalid", "STATE_READ_SHADOW_COMPARE": "0"},
            clear=False,
        ):
            with self.assertRaises(StateReadError):
                read_users(lambda: {})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.sqlite3"
            with patch.dict(
                os.environ,
                {
                    "STATE_READ_BACKEND": "sqlite",
                    "STATE_READ_SHADOW_COMPARE": "0",
                    "SQLITE_DB_PATH": str(missing),
                },
                clear=False,
            ):
                with self.assertRaises(StateReadError):
                    read_users(lambda: {})
            stale = root / "stale.sqlite3"
            conn = sqlite3.connect(stale)
            try:
                conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO schema_meta VALUES('schema_version', '2')")
                conn.commit()
            finally:
                conn.close()
            with patch.dict(
                os.environ,
                {
                    "STATE_READ_BACKEND": "sqlite",
                    "STATE_READ_SHADOW_COMPARE": "0",
                    "SQLITE_DB_PATH": str(stale),
                },
                clear=False,
            ):
                with self.assertRaises(StateReadError):
                    read_users(lambda: {})

    def test_shadow_compare_matches_every_domain_and_records_safe_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            docs = self._seed(db)
            before = state_read_metrics()
            env = {
                "STATE_READ_BACKEND": "legacy",
                "STATE_READ_SHADOW_COMPARE": "1",
                "STATE_READ_SHADOW_STRICT": "1",
                "STATE_READ_SHADOW_REPORT_EVERY": "1000",
                "SQLITE_DB_PATH": str(db),
            }
            stream = StringIO()
            with patch.dict(os.environ, env, clear=False), redirect_stderr(stream):
                self.assertEqual(read_users(lambda: docs["users"]), docs["users"])
                self.assertEqual(
                    read_user_data(lambda: docs["user_data"]), docs["user_data"]
                )
                self.assertEqual(
                    read_collections(lambda: docs["collections"]), docs["collections"]
                )
                self.assertEqual(
                    read_user_uploads(lambda: docs["user_uploads"]),
                    docs["user_uploads"],
                )
                self.assertEqual(
                    read_custom_meta(lambda: docs["custom_meta"]), docs["custom_meta"]
                )
                self.assertEqual(
                    read_allowed_emails(lambda: set(docs["allowed_emails"])),
                    set(docs["allowed_emails"]),
                )

            output = stream.getvalue()
            after = state_read_metrics()
            domains = (
                "users",
                "user_data",
                "collections",
                "user_uploads",
                "custom_meta",
                "allowed_emails",
            )
            for domain in domains:
                self.assertIn(f"event=match domain={domain}", output)
                previous = before.get(domain, {}).get("shadow_matches", 0)
                self.assertEqual(after[domain]["shadow_matches"], previous + 1)
                self.assertEqual(after[domain]["shadow_mismatches"], 0)
                self.assertEqual(after[domain]["shadow_errors"], 0)

    def test_shadow_mismatch_is_fail_safe_and_does_not_log_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            self._seed(db)
            legacy = {
                "secret@example.test": {
                    "reset_code_hash": "super-secret-token",
                    "future": {"private": True},
                }
            }
            env = {
                "STATE_READ_BACKEND": "legacy",
                "STATE_READ_SHADOW_COMPARE": "1",
                "STATE_READ_SHADOW_STRICT": "0",
                "SQLITE_DB_PATH": str(db),
            }
            stream = StringIO()
            with patch.dict(os.environ, env, clear=False), redirect_stderr(stream):
                self.assertEqual(read_users(lambda: legacy), legacy)
            output = stream.getvalue()
            self.assertIn("event=mismatch domain=users", output)
            self.assertNotIn("secret@example.test", output)
            self.assertNotIn("super-secret-token", output)

    def test_shadow_strict_mismatch_fails_with_sanitized_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            self._seed(db)
            env = {
                "STATE_READ_BACKEND": "legacy",
                "STATE_READ_SHADOW_COMPARE": "1",
                "STATE_READ_SHADOW_STRICT": "1",
                "SQLITE_DB_PATH": str(db),
            }
            with patch.dict(os.environ, env, clear=False), self.assertRaises(
                StateReadComparisonError
            ) as raised:
                read_users(lambda: {"secret@example.test": "super-secret-token"})
            self.assertEqual(
                str(raised.exception),
                "Legacy/SQLite shadow comparison mismatch for users.",
            )

    def test_shadow_sqlite_error_is_fail_safe_unless_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.sqlite3"
            stale = root / "stale.sqlite3"
            conn = sqlite3.connect(stale)
            try:
                conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO schema_meta VALUES('schema_version', '2')")
                conn.commit()
            finally:
                conn.close()
            legacy = {"legacy": True}
            env = {
                "STATE_READ_BACKEND": "legacy",
                "STATE_READ_SHADOW_COMPARE": "1",
                "STATE_READ_SHADOW_STRICT": "0",
                "SQLITE_DB_PATH": str(missing),
            }
            for unavailable in (missing, stale):
                env["SQLITE_DB_PATH"] = str(unavailable)
                stream = StringIO()
                with patch.dict(os.environ, env, clear=False), redirect_stderr(stream):
                    self.assertEqual(read_users(lambda: legacy), legacy)
                self.assertIn("event=error domain=users", stream.getvalue())
                self.assertNotIn(str(unavailable), stream.getvalue())

            env["STATE_READ_SHADOW_STRICT"] = "1"
            env["SQLITE_DB_PATH"] = str(missing)
            with patch.dict(os.environ, env, clear=False), self.assertRaises(
                StateReadComparisonError
            ) as raised:
                read_users(lambda: legacy)
            self.assertEqual(
                str(raised.exception), "SQLite shadow comparison failed for users."
            )

    def test_shadow_compare_configuration_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STATE_READ_BACKEND": "legacy",
                "STATE_READ_SHADOW_COMPARE": "maybe",
            },
            clear=False,
        ):
            with self.assertRaises(StateReadError):
                read_users(lambda: {})

        with patch.dict(
            os.environ,
            {
                "STATE_READ_BACKEND": "sqlite",
                "STATE_READ_SHADOW_COMPARE": "1",
            },
            clear=False,
        ):
            with self.assertRaises(StateReadError):
                read_users(lambda: {})


if __name__ == "__main__":
    unittest.main()
