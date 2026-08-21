from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arcdb.storage.legacy_import import (
    export_allowed_emails,
    export_custom_meta,
    export_user_uploads,
    replace_from_documents,
)
from arcdb.storage.runtime_state import (
    ShadowStateError,
    mirror_allowed_emails,
    mirror_custom_metadata_entry,
    mirror_upload_changes,
)
from arcdb.storage.sqlite_db import connect_db, initialize_schema
from arcdb.storage.state_parity import verify_metadata_domains_parity
from scripts.migrate_state_to_sqlite import read_allowed


class RuntimeMetadataTests(unittest.TestCase):
    def test_migration_allowlist_reader_uses_runtime_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowed_gmails.txt"
            path.write_text(
                "# comment\nUSER@example.test\nuser@example.test\nreader@example.test\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_allowed(path),
                ["reader@example.test", "user@example.test"],
            )

    def _seed(
        self,
        db: Path,
        *,
        uploads: dict | None = None,
        custom_meta: dict | None = None,
        allowed: list[str] | None = None,
    ) -> None:
        conn = connect_db(db)
        try:
            initialize_schema(conn)
            replace_from_documents(
                conn,
                users={},
                user_data={},
                collections={},
                user_uploads=uploads or {},
                custom_meta=custom_meta or {},
                allowed_emails=allowed or [],
            )
        finally:
            conn.close()

    def _env(self, db: Path) -> dict[str, str]:
        return {
            "STATE_DUAL_WRITE": "1",
            "STATE_DUAL_WRITE_VERIFY": "1",
            "SQLITE_DB_PATH": str(db),
        }

    def test_mirrors_upload_create_update_delete_and_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "arcdb.sqlite3"
            self._seed(db)
            created = {
                "upload_1": {
                    "id": "upload_1",
                    "title_en": "Fixture",
                    "uploader_email": "dev@arcdb.local",
                    "upload_date": "2026-08-21",
                    "approved": False,
                    "future": {"keep": True},
                }
            }
            approved = json.loads(json.dumps(created))
            approved["upload_1"].update(
                approved=True,
                status="approved",
                approved_at="2026-08-21T18:00:00Z",
            )
            with patch.dict(os.environ, self._env(db), clear=False):
                self.assertEqual(
                    mirror_upload_changes({}, created, reason="create"), ["upload_1"]
                )
                self.assertEqual(
                    mirror_upload_changes(created, approved, reason="approve"),
                    ["upload_1"],
                )
                self.assertEqual(
                    mirror_upload_changes(approved, approved, reason="idempotent"), []
                )

            conn = connect_db(db)
            try:
                self.assertEqual(export_user_uploads(conn), approved)
                row = conn.execute(
                    "SELECT approved, title FROM user_uploads WHERE upload_id='upload_1'"
                ).fetchone()
                self.assertEqual((row["approved"], row["title"]), (1, "Fixture"))
            finally:
                conn.close()

            with patch.dict(os.environ, self._env(db), clear=False):
                self.assertEqual(
                    mirror_upload_changes(approved, {}, reason="reject"), ["upload_1"]
                )
            conn = connect_db(db)
            try:
                self.assertEqual(export_user_uploads(conn), {})
            finally:
                conn.close()

    def test_mirrors_custom_metadata_and_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "arcdb.sqlite3"
            self._seed(db, allowed=["existing@example.test"])
            metadata = {
                "title_en": "Custom title",
                "tags": ["A", "B"],
                "future": {"keep": True},
            }
            with patch.dict(os.environ, self._env(db), clear=False):
                mirror_custom_metadata_entry(
                    "fixture.epub", metadata, reason="custom_metadata"
                )
                self.assertEqual(
                    mirror_allowed_emails(
                        [
                            " Existing@Example.Test ",
                            "new@example.test",
                            "NEW@example.test",
                        ],
                        reason="allowlist_add",
                    ),
                    2,
                )
                self.assertEqual(
                    mirror_allowed_emails(
                        ["existing@example.test"], reason="allowlist_remove"
                    ),
                    1,
                )

            conn = connect_db(db)
            try:
                self.assertEqual(export_custom_meta(conn), {"fixture.epub": metadata})
                self.assertEqual(export_allowed_emails(conn), ["existing@example.test"])
            finally:
                conn.close()

    def test_metadata_full_parity_normalizes_allowlist_comments_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "arcdb.sqlite3"
            uploads = {"u1": {"title_en": "Fixture", "approved": False}}
            custom = {"fixture.epub": {"title_en": "Custom"}}
            allowed = ["dev@example.test", "reader@example.test"]
            self._seed(db, uploads=uploads, custom_meta=custom, allowed=allowed)

            uploads_path = root / "user_uploads.json"
            custom_path = root / "custom_meta.json"
            allowed_path = root / "allowed_gmails.txt"
            uploads_path.write_text(json.dumps(uploads), encoding="utf-8")
            custom_path.write_text(json.dumps(custom), encoding="utf-8")
            allowed_path.write_text(
                "# access list\nDEV@example.test\nreader@example.test\ndev@example.test\n",
                encoding="utf-8",
            )
            self.assertEqual(
                verify_metadata_domains_parity(
                    user_uploads_path=uploads_path,
                    custom_meta_path=custom_path,
                    allowed_emails_path=allowed_path,
                    db_path=db,
                ),
                {"uploads": 1, "custom_metadata": 1, "allowed_emails": 2},
            )

            custom_path.write_text(
                json.dumps({"fixture.epub": {"title_en": "Diverged"}}),
                encoding="utf-8",
            )
            with self.assertRaises(ShadowStateError):
                verify_metadata_domains_parity(
                    user_uploads_path=uploads_path,
                    custom_meta_path=custom_path,
                    allowed_emails_path=allowed_path,
                    db_path=db,
                )

    def test_disabled_metadata_mirrors_do_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            with patch.dict(
                os.environ,
                {"STATE_DUAL_WRITE": "0", "SQLITE_DB_PATH": str(missing)},
                clear=False,
            ):
                self.assertEqual(
                    mirror_upload_changes({}, {"u": {}}, reason="disabled"), []
                )
                self.assertIsNone(
                    mirror_custom_metadata_entry("x", {}, reason="disabled")
                )
                self.assertEqual(
                    mirror_allowed_emails(["x@example.test"], reason="disabled"), 0
                )
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
