from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arcdb.storage.legacy_import import export_users, replace_from_documents
from arcdb.storage.runtime_state import ShadowStateError, mirror_auth_users_changes
from arcdb.storage.sqlite_db import connect_db, initialize_schema
from arcdb.storage.state_parity import verify_users_parity


class RuntimeAuthUsersTests(unittest.TestCase):
    def _seed(self, db: Path, users: dict) -> None:
        conn = connect_db(db)
        try:
            initialize_schema(conn)
            replace_from_documents(
                conn,
                users=users,
                user_data={},
                collections={},
                user_uploads={},
                custom_meta={},
                allowed_emails=[],
            )
        finally:
            conn.close()

    def _env(self, db: Path) -> dict[str, str]:
        return {
            "STATE_DUAL_WRITE": "1",
            "STATE_DUAL_WRITE_VERIFY": "1",
            "SQLITE_DB_PATH": str(db),
        }

    def test_create_update_tokens_unknown_fields_idempotency_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "arcdb.sqlite3"
            self._seed(db, {})
            email = "reader@example.test"
            password_hash = "scrypt:fixture-password-hash"
            created = {
                email: {
                    "pwd_hash": password_hash,
                    "verified": False,
                    "created_at": 123.5,
                    "code_hash": "verification-hash",
                    "code_expires": 999.25,
                    "code_attempts": 2,
                    "reset_code_hash": "reset-hash",
                    "reset_code_expires": 1001.5,
                    "reset_code_attempts": 1,
                    "future": {"roles": ["reader"], "preserve": True},
                }
            }
            with patch.dict(os.environ, self._env(db), clear=False):
                self.assertEqual(
                    mirror_auth_users_changes({}, created, reason="create"),
                    [email],
                )

                verified = json.loads(json.dumps(created))
                record = verified[email]
                record["verified"] = True
                record.pop("code_hash")
                record.pop("code_expires")
                record.pop("code_attempts")
                self.assertEqual(
                    mirror_auth_users_changes(created, verified, reason="verify"),
                    [email],
                )
                self.assertEqual(record["pwd_hash"], password_hash)
                self.assertEqual(
                    mirror_auth_users_changes(verified, verified, reason="idempotent"),
                    [],
                )

            conn = connect_db(db)
            try:
                row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["pwd_hash"], password_hash)
                self.assertEqual(row["verified"], 1)
                self.assertIsNone(row["code_hash"])
                self.assertEqual(row["reset_code_hash"], "reset-hash")
                self.assertEqual(json.loads(row["payload_json"]), verified[email])
            finally:
                conn.close()

            with patch.dict(os.environ, self._env(db), clear=False):
                self.assertEqual(
                    mirror_auth_users_changes(verified, {}, reason="delete"),
                    [email],
                )
            conn = connect_db(db)
            try:
                self.assertEqual(export_users(conn), {})
            finally:
                conn.close()

    def test_disabled_missing_and_stale_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.sqlite3"
            payload = {"reader@example.test": {"verified": False}}
            with patch.dict(
                os.environ,
                {"STATE_DUAL_WRITE": "0", "SQLITE_DB_PATH": str(missing)},
                clear=False,
            ):
                self.assertEqual(
                    mirror_auth_users_changes({}, payload, reason="disabled"), []
                )
            self.assertFalse(missing.exists())

            with patch.dict(
                os.environ,
                {"STATE_DUAL_WRITE": "1", "SQLITE_DB_PATH": str(missing)},
                clear=False,
            ):
                with self.assertRaises(ShadowStateError):
                    mirror_auth_users_changes({}, payload, reason="missing")

            stale = root / "stale.sqlite3"
            conn = sqlite3.connect(stale)
            try:
                conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute("INSERT INTO schema_meta VALUES('schema_version', '2')")
                conn.commit()
            finally:
                conn.close()
            with patch.dict(os.environ, self._env(stale), clear=False):
                with self.assertRaises(ShadowStateError):
                    mirror_auth_users_changes({}, payload, reason="stale")

    def test_immediate_verification_detects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "arcdb.sqlite3"
            self._seed(db, {})
            payload = {"reader@example.test": {"verified": False}}
            with (
                patch.dict(os.environ, self._env(db), clear=False),
                patch("arcdb.storage.runtime_state._sync_auth_user"),
            ):
                with self.assertRaises(ShadowStateError):
                    mirror_auth_users_changes({}, payload, reason="forced-mismatch")

    def test_full_users_parity_including_empty_document_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "arcdb.sqlite3"
            users_path = root / "users.json"
            self._seed(db, {})
            users_path.write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                verify_users_parity(users_path=users_path, db_path=db), {"users": 0}
            )

            users = {
                "reader@example.test": {
                    "pwd_hash": "opaque-hash",
                    "verified": True,
                    "future": [1, {"nested": "value"}],
                }
            }
            with patch.dict(os.environ, self._env(db), clear=False):
                mirror_auth_users_changes({}, users, reason="parity")
            users_path.write_text(json.dumps(users), encoding="utf-8")
            self.assertEqual(
                verify_users_parity(users_path=users_path, db_path=db), {"users": 1}
            )

            conn = connect_db(db)
            try:
                conn.execute(
                    "UPDATE users SET payload_json='{}' WHERE email=?",
                    ("reader@example.test",),
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(ShadowStateError):
                verify_users_parity(users_path=users_path, db_path=db)


if __name__ == "__main__":
    unittest.main()
