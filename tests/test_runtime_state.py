from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arcdb.storage.legacy_import import export_collections, replace_from_documents
from arcdb.storage.runtime_state import (
    ShadowStateError,
    mirror_collection_user,
    mirror_user_changes,
)
from arcdb.storage.sqlite_db import connect_db, initialize_schema
from arcdb.storage.state_parity import (
    verify_collections_parity,
    verify_user_data_parity,
)


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

    def test_mirrors_collection_create_rename_delete_and_empty_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "arcdb.sqlite3"
            self._seed(db, {"dev@arcdb.local": {}})
            env = {
                "STATE_DUAL_WRITE": "1",
                "STATE_DUAL_WRITE_VERIFY": "1",
                "SQLITE_DB_PATH": str(db),
            }
            created = [
                {"id": "a", "name": "First", "future": {"keep": True}},
                {"id": "b", "name": "Second"},
            ]
            renamed = [
                {"id": "a", "name": "Renamed", "future": {"keep": True}},
                {"id": "b", "name": "Second"},
            ]
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(
                    mirror_collection_user(
                        "dev@arcdb.local", created, reason="create"
                    ),
                    2,
                )
                self.assertEqual(
                    mirror_collection_user(
                        "dev@arcdb.local", renamed, reason="rename"
                    ),
                    2,
                )
                self.assertEqual(
                    mirror_collection_user(
                        "dev@arcdb.local", renamed, reason="idempotent"
                    ),
                    2,
                )
                self.assertEqual(
                    mirror_collection_user(
                        "dev@arcdb.local", [], reason="delete-last"
                    ),
                    0,
                )

            conn = connect_db(db)
            try:
                self.assertEqual(export_collections(conn), {"dev@arcdb.local": []})
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM collection_users WHERE user_email=?",
                        ("dev@arcdb.local",),
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM collections WHERE user_email=?",
                        ("dev@arcdb.local",),
                    ).fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_collection_mirror_rejects_unreproducible_legacy_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "arcdb.sqlite3"
            self._seed(db, {"dev@arcdb.local": {}})
            env = {
                "STATE_DUAL_WRITE": "1",
                "STATE_DUAL_WRITE_VERIFY": "1",
                "SQLITE_DB_PATH": str(db),
            }
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(ShadowStateError):
                    mirror_collection_user(
                        "dev@arcdb.local", [{"name": "missing id"}], reason="bad"
                    )
                with self.assertRaises(ShadowStateError):
                    mirror_collection_user(
                        "dev@arcdb.local",
                        [{"id": "same"}, {"id": "same"}],
                        reason="duplicate",
                    )

    def test_full_collection_and_membership_parity_detects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "arcdb.sqlite3"
            user_data = {
                "dev@arcdb.local": {
                    "42": {
                        "status": "reading",
                        "progress": 1,
                        "collections": ["reading"],
                    }
                }
            }
            collections = {
                "empty@arcdb.local": [],
                "dev@arcdb.local": [{"id": "reading", "name": "Reading"}],
            }
            conn = connect_db(db)
            try:
                initialize_schema(conn)
                replace_from_documents(
                    conn,
                    users={},
                    user_data=user_data,
                    collections=collections,
                    user_uploads={},
                    custom_meta={},
                    allowed_emails=[],
                )
            finally:
                conn.close()

            user_data_path = root / "user_data.json"
            collections_path = root / "collections.json"
            user_data_path.write_text(json.dumps(user_data), encoding="utf-8")
            collections_path.write_text(json.dumps(collections), encoding="utf-8")
            self.assertEqual(
                verify_user_data_parity(user_data_path=user_data_path, db_path=db),
                {"users": 1, "records": 1, "memberships": 1},
            )
            self.assertEqual(
                verify_collections_parity(
                    collections_path=collections_path, db_path=db
                ),
                {"users": 2, "collections": 1},
            )

            conn = connect_db(db)
            try:
                conn.execute("DELETE FROM collection_items")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(ShadowStateError):
                verify_user_data_parity(user_data_path=user_data_path, db_path=db)

            collections["dev@arcdb.local"][0]["name"] = "Diverged"
            collections_path.write_text(json.dumps(collections), encoding="utf-8")
            with self.assertRaises(ShadowStateError):
                verify_collections_parity(
                    collections_path=collections_path, db_path=db
                )


if __name__ == "__main__":
    unittest.main()
