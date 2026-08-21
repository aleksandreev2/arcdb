from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arcdb.storage.legacy_import import replace_from_documents
from arcdb.storage.runtime_state import ShadowStateError, mirror_user_changes
from arcdb.storage.sqlite_db import connect_db, initialize_schema


class RuntimeStateTests(unittest.TestCase):
    def _seed(self, db: Path, user_data: dict) -> None:
        conn = connect_db(db)
        try:
            initialize_schema(conn)
            replace_from_documents(
                conn,
                users={"dev@arcdb.local": {"verified": True}},
                user_data=user_data,
                collections={"dev@arcdb.local": []},
                user_uploads={},
                custom_meta={},
                allowed_emails=["dev@arcdb.local"],
            )
        finally:
            conn.close()

    def test_mirrors_changed_record_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "arcdb.sqlite3"
            before = {
                "42": {"status": "want_to_read", "progress": 0},
                "99": {"status": "reading", "progress": 3, "collections": ["x"]},
            }
            self._seed(db, {"dev@arcdb.local": before})
            after = {
                "42": {
                    "status": "reading",
                    "progress": 5,
                    "last_read": 123.5,
                    "dl": 2,
                    "last_dl": 120.0,
                    "hidden": True,
                    "collections": ["b", "a"],
                    "future": {"preserve": True},
                }
            }
            env = {
                "STATE_DUAL_WRITE": "1",
                "STATE_DUAL_WRITE_VERIFY": "1",
                "SQLITE_DB_PATH": str(db),
            }
            with patch.dict(os.environ, env, clear=False):
                changed = mirror_user_changes(
                    "dev@arcdb.local", before, after, reason="test"
                )
            self.assertEqual(changed, ["42", "99"])

            conn = connect_db(db)
            try:
                row = conn.execute(
                    "SELECT * FROM user_novel_state WHERE user_email=? AND novel_key='42'",
                    ("dev@arcdb.local",),
                ).fetchone()
                self.assertEqual(row["status"], "reading")
                self.assertEqual(row["progress"], 5)
                self.assertEqual(row["download_count"], 2)
                self.assertEqual(row["hidden"], 1)
                self.assertEqual(json.loads(row["payload_json"]), after["42"])
                memberships = [
                    r[0]
                    for r in conn.execute(
                        "SELECT collection_id FROM collection_items WHERE user_email=? AND novel_key='42' ORDER BY collection_id",
                        ("dev@arcdb.local",),
                    )
                ]
                self.assertEqual(memberships, ["a", "b"])
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM user_novel_state WHERE user_email=? AND novel_key='99'",
                        ("dev@arcdb.local",),
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_keeps_empty_user_container_after_last_record_deleted(self) -> None:
        from arcdb.storage.legacy_import import export_user_data

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "arcdb.sqlite3"
            before = {"42": {"status": "reading", "progress": 1}}
            self._seed(db, {"dev@arcdb.local": before})
            env = {
                "STATE_DUAL_WRITE": "1",
                "STATE_DUAL_WRITE_VERIFY": "1",
                "SQLITE_DB_PATH": str(db),
            }
            with patch.dict(os.environ, env, clear=False):
                mirror_user_changes("dev@arcdb.local", before, {}, reason="delete-last")
            conn = connect_db(db)
            try:
                self.assertEqual(export_user_data(conn), {"dev@arcdb.local": {}})
            finally:
                conn.close()

    def test_disabled_does_not_touch_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            with patch.dict(
                os.environ,
                {"STATE_DUAL_WRITE": "0", "SQLITE_DB_PATH": str(missing)},
                clear=False,
            ):
                self.assertEqual(
                    mirror_user_changes("x", {}, {"1": {"progress": 1}}, reason="disabled"),
                    [],
                )
            self.assertFalse(missing.exists())

    def test_enabled_refuses_missing_shadow_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            with patch.dict(
                os.environ,
                {"STATE_DUAL_WRITE": "1", "SQLITE_DB_PATH": str(missing)},
                clear=False,
            ):
                with self.assertRaises(ShadowStateError):
                    mirror_user_changes("x", {}, {"1": {"progress": 1}}, reason="missing")


if __name__ == "__main__":
    unittest.main()
