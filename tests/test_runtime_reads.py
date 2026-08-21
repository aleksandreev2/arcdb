from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from arcdb.storage.legacy_import import replace_from_documents
from arcdb.storage.runtime_reads import (
    StateReadError,
    read_allowed_emails,
    read_collections,
    read_custom_meta,
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
        with patch.dict(os.environ, {"STATE_READ_BACKEND": "legacy"}, clear=False):
            self.assertEqual(read_users(loader), {"legacy": True})
        loader.assert_called_once_with()

    def test_sqlite_exports_every_state_domain_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            docs = self._seed(db)
            loader = Mock(side_effect=AssertionError("legacy loader used"))
            env = {"STATE_READ_BACKEND": "sqlite", "SQLITE_DB_PATH": str(db)}
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
        with patch.dict(os.environ, {"STATE_READ_BACKEND": "invalid"}, clear=False):
            with self.assertRaises(StateReadError):
                read_users(lambda: {})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.sqlite3"
            with patch.dict(
                os.environ,
                {"STATE_READ_BACKEND": "sqlite", "SQLITE_DB_PATH": str(missing)},
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
                {"STATE_READ_BACKEND": "sqlite", "SQLITE_DB_PATH": str(stale)},
                clear=False,
            ):
                with self.assertRaises(StateReadError):
                    read_users(lambda: {})


if __name__ == "__main__":
    unittest.main()
