from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from arcdb.storage.legacy_import import replace_from_documents
from arcdb.storage.sqlite_db import connect_db, initialize_schema
from arcdb.storage.state_parity import verify_users_parity
from scripts.dev_seed import main


class DevAccountSeedTests(unittest.TestCase):
    def test_repeated_seed_preserves_hash_unknown_fields_and_shadow_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = root / "metadata"
            meta.mkdir()
            users_path = meta / "users.json"
            allowed_path = meta / "allowed_gmails.txt"
            db = root / "arcdb.sqlite3"
            email = "dev@example.test"
            password = "fixture-password"
            users = {
                email: {
                    "pwd_hash": generate_password_hash(password),
                    "verified": True,
                    "created_at": 17.0,
                    "dev_managed": True,
                    "future": {"preserve": "exactly"},
                }
            }
            users_path.write_text(json.dumps(users), encoding="utf-8")
            allowed_path.write_text(email + "\n", encoding="utf-8")
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
                    allowed_emails=[email],
                )
            finally:
                conn.close()

            before = users_path.read_bytes()
            env = {
                "ARCHIVEDB_LOCAL_DEV": "1",
                "LOCAL_DEV_EMAIL": email,
                "LOCAL_DEV_PASSWORD": password,
                "META_DIR": str(meta),
                "USERS_PATH": str(users_path),
                "ALLOWED_EMAILS_PATH": str(allowed_path),
                "SQLITE_DB_PATH": str(db),
                "STATE_DUAL_WRITE": "1",
                "STATE_DUAL_WRITE_VERIFY": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(main(), 0)
                self.assertEqual(main(), 0)

            self.assertEqual(users_path.read_bytes(), before)
            self.assertEqual(
                verify_users_parity(users_path=users_path, db_path=db), {"users": 1}
            )
            self.assertEqual(json.loads(users_path.read_text(encoding="utf-8")), users)


if __name__ == "__main__":
    unittest.main()
