from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from arcdb.storage.legacy_import import export_users, replace_from_documents
from arcdb.storage.sqlite_backup import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    SQLiteBackupError,
    create_sqlite_backup,
    restore_backup_to_new_target,
    verify_backup_directory,
)
from arcdb.storage.sqlite_db import connect_db, initialize_schema
from arcdb.storage.safe_migration import sha256_file


class SQLiteBackupTests(unittest.TestCase):
    def _source(self, root: Path) -> tuple[Path, sqlite3.Connection]:
        database = root / "live.sqlite3"
        conn = connect_db(database)
        initialize_schema(conn)
        replace_from_documents(
            conn,
            users={
                "reader@example.test": {
                    "pwd_hash": "fixture-hash",
                    "verified": True,
                    "future": {"preserved": True},
                }
            },
            user_data={
                "reader@example.test": {
                    "42": {"progress": 12, "collections": ["reading"]}
                }
            },
            collections={
                "reader@example.test": [{"id": "reading", "name": "Reading"}]
            },
            user_uploads={"upload-1": {"approved": False}},
            custom_meta={"fixture.epub": {"title": "Fixture"}},
            allowed_emails=["reader@example.test"],
        )
        self.assertTrue(Path(str(database) + "-wal").is_file())
        return database, conn

    def test_online_backup_captures_wal_and_passes_independent_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, writer = self._source(root)
            try:
                source_hash_before = sha256_file(source)
                wal_path = Path(str(source) + "-wal")
                wal_hash_before = sha256_file(wal_path)
                backup_dir = root / "backups" / "backup-1"
                manifest = create_sqlite_backup(
                    source_db=source,
                    backup_dir=backup_dir,
                    timeout_seconds=30,
                )
                self.assertTrue(
                    manifest["source"]["sidecars_observed"]["wal"]["observed"]
                )
                self.assertTrue(manifest["decision"]["backup_verified"])
                self.assertTrue(manifest["decision"]["restore_verified"])
                self.assertFalse(
                    manifest["decision"]["safe_to_delete_legacy_data"]
                )
                serialized_manifest = json.dumps(manifest, ensure_ascii=False)
                self.assertNotIn(str(root), serialized_manifest)
                self.assertNotIn("reader@example.test", serialized_manifest)
                self.assertNotIn("fixture-hash", serialized_manifest)
                self.assertEqual(manifest["database"]["journal_mode"], "delete")
                self.assertTrue((backup_dir / BACKUP_FILENAME).is_file())
                self.assertTrue((backup_dir / MANIFEST_FILENAME).is_file())
                self.assertFalse(
                    Path(str(backup_dir / BACKUP_FILENAME) + "-wal").exists()
                )
                self.assertEqual(sha256_file(source), source_hash_before)
                self.assertEqual(sha256_file(wal_path), wal_hash_before)
                self.assertEqual(
                    writer.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1
                )

                writer.execute("DELETE FROM users")
                writer.commit()
                verification = verify_backup_directory(backup_dir)
                self.assertEqual(
                    verification["database"]["row_counts"]["users"], 1
                )
                self.assertEqual(
                    verification["restore_test"]["application_probe"], "ok"
                )

                restored = root / "restored" / "arcdb.sqlite3"
                result = restore_backup_to_new_target(
                    backup_dir=backup_dir,
                    target_db=restored,
                )
                self.assertEqual(result["status"], "restored_to_new_target")
                self.assertFalse(result["decision"]["active_database_replaced"])
                restored_conn = sqlite3.connect(restored)
                restored_conn.row_factory = sqlite3.Row
                try:
                    users = export_users(restored_conn)
                finally:
                    restored_conn.close()
                self.assertIn("reader@example.test", users)
                self.assertEqual(
                    users["reader@example.test"]["future"], {"preserved": True}
                )
            finally:
                writer.close()

    def test_backup_and_restore_refuse_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, writer = self._source(root)
            try:
                existing_backup = root / "existing-backup"
                existing_backup.mkdir()
                with self.assertRaises(SQLiteBackupError) as backup_error:
                    create_sqlite_backup(
                        source_db=source,
                        backup_dir=existing_backup,
                    )
                self.assertEqual(backup_error.exception.code, "backup_exists")

                backup_dir = root / "backup"
                create_sqlite_backup(source_db=source, backup_dir=backup_dir)
                existing_target = root / "existing.sqlite3"
                existing_target.write_bytes(b"do-not-overwrite")
                before = existing_target.read_bytes()
                with self.assertRaises(SQLiteBackupError) as restore_error:
                    restore_backup_to_new_target(
                        backup_dir=backup_dir,
                        target_db=existing_target,
                    )
                self.assertEqual(
                    restore_error.exception.code, "restore_target_exists"
                )
                self.assertEqual(existing_target.read_bytes(), before)

                sidecar_only_target = root / "sidecar-only.sqlite3"
                Path(str(sidecar_only_target) + "-wal").write_bytes(b"preserve")
                with self.assertRaises(SQLiteBackupError) as sidecar_error:
                    restore_backup_to_new_target(
                        backup_dir=backup_dir,
                        target_db=sidecar_only_target,
                    )
                self.assertEqual(
                    sidecar_error.exception.code, "restore_target_exists"
                )

                with self.assertRaises(SQLiteBackupError) as nested_error:
                    restore_backup_to_new_target(
                        backup_dir=backup_dir,
                        target_db=backup_dir / "nested" / "arcdb.sqlite3",
                    )
                self.assertEqual(
                    nested_error.exception.code, "restore_target_inside_backup"
                )
            finally:
                writer.close()

    def test_corruption_and_manifest_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, writer = self._source(root)
            try:
                backup_dir = root / "backup"
                create_sqlite_backup(source_db=source, backup_dir=backup_dir)

                corrupt = root / "corrupt"
                shutil.copytree(backup_dir, corrupt)
                artifact = corrupt / BACKUP_FILENAME
                artifact.write_bytes(artifact.read_bytes() + b"corruption")
                with self.assertRaises(SQLiteBackupError) as checksum:
                    verify_backup_directory(corrupt)
                self.assertEqual(
                    checksum.exception.code, "backup_size_mismatch"
                )

                with_sidecar = root / "with-sidecar"
                shutil.copytree(backup_dir, with_sidecar)
                Path(str(with_sidecar / BACKUP_FILENAME) + "-wal").write_bytes(
                    b"unexpected"
                )
                with self.assertRaises(SQLiteBackupError) as sidecar:
                    verify_backup_directory(with_sidecar)
                self.assertEqual(
                    sidecar.exception.code, "backup_sidecars_present"
                )

                tampered = root / "tampered"
                shutil.copytree(backup_dir, tampered)
                manifest_path = tampered / MANIFEST_FILENAME
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["artifact"]["filename"] = "../outside.sqlite3"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(SQLiteBackupError) as invalid:
                    verify_backup_directory(tampered)
                self.assertEqual(
                    invalid.exception.code, "backup_manifest_invalid"
                )
            finally:
                writer.close()

    def test_missing_database_and_invalid_timeout_fail_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SQLiteBackupError) as missing:
                create_sqlite_backup(
                    source_db=root / "missing.sqlite3",
                    backup_dir=root / "backup",
                )
            self.assertEqual(missing.exception.code, "database_missing")
            self.assertFalse((root / "backup").exists())

            source, writer = self._source(root)
            try:
                with self.assertRaises(SQLiteBackupError) as timeout:
                    create_sqlite_backup(
                        source_db=source,
                        backup_dir=root / "backup",
                        timeout_seconds=0,
                    )
                self.assertEqual(timeout.exception.code, "invalid_timeout")
                self.assertFalse((root / "backup").exists())
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
