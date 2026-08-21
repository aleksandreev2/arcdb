from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arcdb.storage.legacy_import import (
    export_collections,
    export_custom_meta,
    export_user_data,
    export_user_uploads,
    export_users,
    replace_from_documents,
    state_counts,
)
from arcdb.storage.sqlite_db import SCHEMA_VERSION, connect_db, initialize_schema


class SQLiteStateTests(unittest.TestCase):
    def test_refuses_in_place_schema_version_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.sqlite3"
            conn = connect_db(db)
            try:
                conn.execute(
                    "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', '2')"
                )
                conn.commit()
                with self.assertRaises(RuntimeError):
                    initialize_schema(conn)
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='collection_users'"
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "2",
                )
            finally:
                conn.close()

    def test_import_roundtrip_and_wal(self) -> None:
        users = {
            "dev@arcdb.local": {
                "pwd_hash": "hash",
                "verified": True,
                "created_at": 123.5,
                "dev_managed": True,
            }
        }
        user_data = {
            "empty@arcdb.local": {},
            "dev@arcdb.local": {
                "422601": {
                    "status": "reading",
                    "progress": 78,
                    "collections": ["reading"],
                    "last_read": 456.5,
                    "dl": 2,
                    "hidden": False,
                    "future_field": {"keep": True},
                }
            }
        }
        collections = {
            "empty@arcdb.local": [],
            "dev@arcdb.local": [
                {"id": "reading", "name": "Читаю"},
                {"id": "later", "name": "Позже", "extra": 1},
            ]
        }
        uploads = {
            "u1": {
                "uploader_email": "dev@arcdb.local",
                "approved": True,
                "upload_date": "2026-08-21T00:00:00Z",
                "raw_title": "Fixture",
            }
        }
        custom_meta = {"fixture.epub": {"title": "Fixture", "tags": ["Test"]}}
        allowed = ["dev@arcdb.local", "reader@example.test"]

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            conn = connect_db(db)
            try:
                initialize_schema(conn)
                counts = replace_from_documents(
                    conn,
                    users=users,
                    user_data=user_data,
                    collections=collections,
                    user_uploads=uploads,
                    custom_meta=custom_meta,
                    allowed_emails=allowed,
                )
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(
                    conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],
                    str(SCHEMA_VERSION),
                )
                self.assertEqual(counts["users"], 1)
                self.assertEqual(counts["user_state_users"], 2)
                self.assertEqual(counts["user_novel_state"], 1)
                self.assertEqual(counts["collection_users"], 2)
                self.assertEqual(counts["collections"], 2)
                self.assertEqual(counts["collection_items"], 1)
                self.assertEqual(export_users(conn), users)
                self.assertEqual(export_user_data(conn), user_data)
                self.assertEqual(export_collections(conn), collections)
                self.assertEqual(export_user_uploads(conn), uploads)
                self.assertEqual(export_custom_meta(conn), custom_meta)

                conn.execute(
                    "UPDATE legacy_documents SET payload_json='{}' WHERE name='collections.json'"
                )
                conn.commit()
                self.assertEqual(
                    export_collections(conn),
                    collections,
                    "live collection export must use collection_users, not a stale import snapshot",
                )

                replace_from_documents(
                    conn,
                    users=users,
                    user_data=user_data,
                    collections=collections,
                    user_uploads=uploads,
                    custom_meta=custom_meta,
                    allowed_emails=allowed,
                )
                self.assertEqual(state_counts(conn), counts)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
