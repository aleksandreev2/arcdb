from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arcdb.storage import readiness
from arcdb.storage.legacy_import import replace_from_documents
from arcdb.storage.readiness import (
    ReadinessError,
    resolve_readiness_paths,
    verify_read_cutover_readiness,
)
from arcdb.storage.safe_migration import sha256_file
from arcdb.storage.sqlite_db import connect_db, initialize_schema
from scripts.verify_read_cutover_readiness import write_report


class ReadCutoverReadinessTests(unittest.TestCase):
    def _fixture(self, root: Path, *, empty: bool = False):
        meta = root / "metadata"
        meta.mkdir()
        db = root / "arcdb.sqlite3"
        if empty:
            docs = {
                "users": {},
                "user_data": {},
                "collections": {},
                "user_uploads": {},
                "custom_meta": {},
                "allowed_emails": [],
            }
        else:
            docs = {
                "users": {
                    "reader@example.test": {
                        "pwd_hash": "fixture-password-hash",
                        "verified": True,
                        "reset_code_hash": "fixture-reset-token-hash",
                        "future": {"opaque": True},
                    }
                },
                "user_data": {
                    "reader@example.test": {
                        "42": {
                            "progress": 7,
                            "collections": ["reading"],
                            "future": "preserved",
                        }
                    }
                },
                "collections": {
                    "reader@example.test": [{"id": "reading", "name": "Reading"}]
                },
                "user_uploads": {"upload-1": {"approved": False}},
                "custom_meta": {"book.epub": {"title": "Fixture"}},
                "allowed_emails": ["reader@example.test"],
            }

        for name, key in (
            ("users.json", "users"),
            ("user_data.json", "user_data"),
            ("collections.json", "collections"),
            ("user_uploads.json", "user_uploads"),
            ("custom_meta.json", "custom_meta"),
        ):
            (meta / name).write_text(
                json.dumps(docs[key], ensure_ascii=False), encoding="utf-8"
            )
        (meta / "allowed_gmails.txt").write_text(
            "# preserved comment\n"
            + "\n".join(docs["allowed_emails"])
            + ("\n" if docs["allowed_emails"] else ""),
            encoding="utf-8",
        )
        nested = meta / "nested"
        nested.mkdir()
        (nested / "community-state.json").write_text(
            '{"not_in_schema_v3":true}', encoding="utf-8"
        )

        conn = connect_db(db)
        try:
            initialize_schema(conn)
            replace_from_documents(conn, **docs)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        finally:
            conn.close()
        return meta, db, docs

    def test_success_is_read_only_complete_and_payload_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, db, docs = self._fixture(root)
            paths = resolve_readiness_paths(meta_dir=meta, db_path=db)
            source_before = {
                path.relative_to(meta): path.read_bytes()
                for path in meta.rglob("*")
                if path.is_file()
            }
            db_hash = sha256_file(db)

            report = verify_read_cutover_readiness(paths)

            source_after = {
                path.relative_to(meta): path.read_bytes()
                for path in meta.rglob("*")
                if path.is_file()
            }
            self.assertEqual(source_after, source_before)
            self.assertEqual(sha256_file(db), db_hash)
            self.assertEqual(report["status"], "preflight_passed")
            self.assertEqual(report["database"]["schema_version"], 3)
            self.assertEqual(report["database"]["quick_check"], "ok")
            self.assertEqual(report["database"]["integrity_check"], "ok")
            self.assertEqual(report["legacy_sources"]["unknown_files"], 1)
            self.assertFalse(report["decision"]["bounded_canary_authorized"])
            self.assertFalse(report["decision"]["primary_read_authorized"])

            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("reader@example.test", serialized)
            self.assertNotIn("fixture-password-hash", serialized)
            self.assertNotIn("fixture-reset-token-hash", serialized)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("community-state.json", serialized)
            self.assertEqual(report["database"]["row_counts"]["users"], len(docs["users"]))

    def test_explicit_derived_database_is_not_a_legacy_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, db, _docs = self._fixture(root)
            derived = meta / "library_index.sqlite3"
            derived.write_bytes(b"derived")
            Path(str(derived) + "-wal").write_bytes(b"derived-wal")
            paths = resolve_readiness_paths(
                meta_dir=meta,
                db_path=db,
                derived_database_paths=(derived,),
            )
            report = verify_read_cutover_readiness(paths)
            self.assertEqual(report["status"], "preflight_passed")
            self.assertEqual(report["legacy_sources"]["unknown_files"], 1)

    def test_empty_documents_are_valid_when_core_files_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, db, _ = self._fixture(root, empty=True)
            report = verify_read_cutover_readiness(
                resolve_readiness_paths(meta_dir=meta, db_path=db)
            )
            self.assertEqual(report["database"]["row_counts"]["users"], 0)
            self.assertEqual(
                report["database"]["row_counts"]["user_novel_state"], 0
            )

    def test_missing_core_and_stale_schema_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, db, _ = self._fixture(root)
            (meta / "users.json").unlink()
            with self.assertRaises(ReadinessError) as missing:
                verify_read_cutover_readiness(
                    resolve_readiness_paths(meta_dir=meta, db_path=db)
                )
            self.assertEqual(missing.exception.code, "required_source_missing")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, db, _ = self._fixture(root)
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "UPDATE schema_meta SET value='2' WHERE key='schema_version'"
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(ReadinessError) as stale:
                verify_read_cutover_readiness(
                    resolve_readiness_paths(meta_dir=meta, db_path=db)
                )
            self.assertEqual(stale.exception.code, "schema_mismatch")

    def test_missing_database_and_normalized_membership_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, _, _ = self._fixture(root)
            with self.assertRaises(ReadinessError) as missing:
                verify_read_cutover_readiness(
                    resolve_readiness_paths(
                        meta_dir=meta, db_path=root / "missing.sqlite3"
                    )
                )
            self.assertEqual(missing.exception.code, "database_missing")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, db, _ = self._fixture(root)
            conn = sqlite3.connect(db)
            try:
                conn.execute("DELETE FROM collection_items")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(ReadinessError) as memberships:
                verify_read_cutover_readiness(
                    resolve_readiness_paths(meta_dir=meta, db_path=db)
                )
            self.assertEqual(
                memberships.exception.code, "collection_items_parity"
            )

    def test_payload_mismatch_reports_only_safe_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, db, _ = self._fixture(root)
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "UPDATE users SET payload_json=? WHERE email=?",
                    ('{"future":"different","secret":"do-not-log"}', "reader@example.test"),
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(ReadinessError) as mismatch:
                verify_read_cutover_readiness(
                    resolve_readiness_paths(meta_dir=meta, db_path=db)
                )
            self.assertEqual(mismatch.exception.code, "users_parity")
            message = str(mismatch.exception)
            self.assertIn("1 differing key(s)", message)
            self.assertNotIn("reader@example.test", message)
            self.assertNotIn("do-not-log", message)

    def test_source_change_during_check_is_detected_before_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta, db, _ = self._fixture(root)
            original_export = readiness.export_users

            def export_and_add_source(conn):
                result = original_export(conn)
                (meta / "arrived-during-check.json").write_text(
                    "{}", encoding="utf-8"
                )
                return result

            with patch.object(readiness, "export_users", export_and_add_source):
                with self.assertRaises(ReadinessError) as changed:
                    verify_read_cutover_readiness(
                        resolve_readiness_paths(meta_dir=meta, db_path=db)
                    )
            self.assertEqual(changed.exception.code, "source_changed")
            self.assertNotIn("arrived-during-check.json", str(changed.exception))

    def test_report_writer_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "readiness.json"
            report = {"status": "preflight_passed"}
            write_report(report_path, report)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)
            with self.assertRaises(ReadinessError) as existing:
                write_report(report_path, report)
            self.assertEqual(existing.exception.code, "report_exists")


if __name__ == "__main__":
    unittest.main()
