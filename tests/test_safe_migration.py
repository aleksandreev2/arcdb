from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from arcdb.storage.safe_migration import (
    assert_sources_unchanged,
    build_verified_candidate,
    create_verified_snapshot,
    file_fingerprint,
    promote_candidate,
    sha256_file,
)


class SafeMigrationTests(unittest.TestCase):
    def _docs(self):
        return {
            "users": {"dev@example.test": {"pwd_hash": "x", "verified": True}},
            "user_data": {"dev@example.test": {}},
            "collections": {"dev@example.test": []},
            "user_uploads": {},
            "custom_meta": {},
            "allowed_emails": ["dev@example.test"],
        }

    def test_snapshot_candidate_and_promotion_do_not_modify_legacy_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = root / "metadata"
            meta.mkdir()
            sources = {
                "users.json": self._docs()["users"],
                "user_data.json": self._docs()["user_data"],
                "collections.json": self._docs()["collections"],
                "user_uploads.json": {},
                "custom_meta.json": {},
            }
            for name, value in sources.items():
                (meta / name).write_text(json.dumps(value), encoding="utf-8")
            (meta / "allowed_gmails.txt").write_text("dev@example.test\n", encoding="utf-8")
            # Unknown state is not imported yet, but it still must be snapshotted.
            unknown = meta / "ip_exemptions.json"
            unknown.write_text('{"127.0.0.1": true}\n', encoding="utf-8")
            raw_csv = root / "master_library_index.csv"
            raw_csv.write_text("id,title\n1,Fixture\n", encoding="utf-8")

            explicit = [
                meta / "users.json",
                meta / "user_data.json",
                meta / "collections.json",
                meta / "user_uploads.json",
                meta / "custom_meta.json",
                meta / "allowed_gmails.txt",
                raw_csv,
            ]
            before = {str(path.resolve()): file_fingerprint(path.resolve()) for path in [*meta.iterdir(), raw_csv]}

            backup = root / "backups" / "run1"
            snapshot = create_verified_snapshot(
                backup_dir=backup,
                meta_dir=meta,
                explicit_files=explicit,
            )
            self.assertTrue((backup / "legacy-files" / "metadata" / "ip_exemptions.json").exists())
            self.assertTrue(any(entry["source"] == str(raw_csv.resolve()) for entry in snapshot["manifest"]["files"]))

            candidate = root / "arcdb.sqlite3.candidate"
            counts, checks = build_verified_candidate(candidate_path=candidate, docs=self._docs())
            self.assertEqual(counts["users"], 1)
            self.assertEqual(checks["integrity_check"], ["ok"])

            assert_sources_unchanged(
                snapshot["fingerprints"], meta_dir=meta, explicit_files=explicit
            )
            target = root / "arcdb.sqlite3"
            promote_candidate(candidate, target, backup)
            self.assertTrue(target.exists())

            for raw_path, expected in before.items():
                current = file_fingerprint(Path(raw_path))
                self.assertEqual(current.get("sha256"), expected.get("sha256"), raw_path)
                self.assertEqual(current.get("size"), expected.get("size"), raw_path)

    def test_source_change_aborts_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = root / "metadata"
            meta.mkdir()
            users = meta / "users.json"
            users.write_text("{}\n", encoding="utf-8")
            snapshot = create_verified_snapshot(
                backup_dir=root / "backup",
                meta_dir=meta,
                explicit_files=[users],
            )
            users.write_text('{"changed": true}\n', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                assert_sources_unchanged(
                    snapshot["fingerprints"], meta_dir=meta, explicit_files=[users]
                )

    def test_new_unknown_metadata_file_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = root / "metadata"
            meta.mkdir()
            users = meta / "users.json"
            users.write_text("{}\n", encoding="utf-8")
            snapshot = create_verified_snapshot(
                backup_dir=root / "backup",
                meta_dir=meta,
                explicit_files=[users],
            )
            (meta / "new_state.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                assert_sources_unchanged(
                    snapshot["fingerprints"], meta_dir=meta, explicit_files=[users]
                )

    def test_explicit_derived_databases_are_excluded_but_unknown_files_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = root / "metadata"
            meta.mkdir()
            users = meta / "users.json"
            users.write_text("{}\n", encoding="utf-8")
            derived = meta / "library_index.sqlite3"
            derived.write_bytes(b"derived")
            Path(str(derived) + "-wal").write_bytes(b"wal")
            unknown = meta / "unknown.sqlite3"
            unknown.write_bytes(b"preserve")

            snapshot = create_verified_snapshot(
                backup_dir=root / "backup",
                meta_dir=meta,
                explicit_files=[users],
                excluded_files=[derived],
            )
            sources = {entry["source"] for entry in snapshot["manifest"]["files"]}
            self.assertNotIn(str(derived.resolve()), sources)
            self.assertNotIn(str(Path(str(derived) + "-wal").resolve()), sources)
            self.assertIn(str(unknown.resolve()), sources)

            derived.write_bytes(b"changed while migration runs")
            assert_sources_unchanged(
                snapshot["fingerprints"],
                meta_dir=meta,
                explicit_files=[users],
                excluded_files=[derived],
            )

    def test_existing_sqlite_is_preserved_before_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "arcdb.sqlite3"
            conn = sqlite3.connect(target)
            conn.execute("CREATE TABLE old_data(value TEXT)")
            conn.execute("INSERT INTO old_data(value) VALUES('keep-me')")
            conn.commit()
            conn.close()
            old_hash = sha256_file(target)

            candidate = root / "candidate.sqlite3"
            build_verified_candidate(candidate_path=candidate, docs=self._docs())
            backup_dir = root / "backup"
            backup_dir.mkdir()
            previous_copy = promote_candidate(candidate, target, backup_dir)

            self.assertIsNotNone(previous_copy)
            assert previous_copy is not None
            self.assertTrue(previous_copy.exists())
            self.assertEqual(sha256_file(previous_copy), old_hash)
            moved_original = backup_dir / "previous-sqlite" / "arcdb.sqlite3.pre-migration-original"
            self.assertTrue(moved_original.exists())
            self.assertEqual(sha256_file(moved_original), old_hash)


if __name__ == "__main__":
    unittest.main()
