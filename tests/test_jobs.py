from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile

from arcdb.epub_io import EpubLimits, validate_epub_archive
from arcdb.jobs import JobStore
from arcdb.package_worker import (
    PackageJobTimedOut,
    PackageWorkerSettings,
    cleanup_expired_job_artifacts,
    run_one_job,
)


MIMETYPE = b"application/epub+zip"
CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>
</container>"""
OPF = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="chapter"/></spine>
</package>"""
CHAPTER = b"<html xmlns='http://www.w3.org/1999/xhtml'><body>ok</body></html>"


def make_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
        archive.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr("OEBPS/chapter.xhtml", CHAPTER)


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "jobs.sqlite3"
        self.store = JobStore(self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enqueue(self, **overrides):
        values = {
            "kind": "epub_package",
            "owner_email": "owner@example.test",
            "payload": {"session_id": "a" * 32},
            "dedupe_key": "epub_package:" + "a" * 32,
            "max_attempts": 3,
            "timeout_seconds": 30,
            "retention_seconds": 60,
        }
        values.update(overrides)
        return self.store.enqueue(**values)

    def test_schema_uses_wal_and_enqueue_is_idempotent(self) -> None:
        first, created = self.enqueue()
        second, created_again = self.enqueue()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.job_id, second.job_id)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM job_schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "1",
            )
        finally:
            conn.close()

    def test_claim_complete_and_owner_isolation(self) -> None:
        queued, _ = self.enqueue()
        claimed = JobStore(self.db).claim_next(
            worker_id="worker-1", stale_after_seconds=60
        )
        self.assertEqual(claimed.job_id, queued.job_id)
        self.assertEqual(claimed.state, "processing")
        self.assertEqual(claimed.attempts, 1)
        self.assertIsNone(
            JobStore(self.db).claim_next(worker_id="worker-2", stale_after_seconds=60)
        )
        self.assertIsNone(
            self.store.get_owned(queued.job_id, "someone-else@example.test")
        )
        self.assertTrue(self.store.heartbeat(queued.job_id, "worker-1", 55))
        self.assertTrue(
            self.store.complete(queued.job_id, "worker-1", {"download_url": "/d"})
        )
        done = self.store.get(queued.job_id)
        self.assertEqual(done.state, "done")
        self.assertEqual(done.progress, 100)
        self.assertEqual(done.result, {"download_url": "/d"})

    def test_retry_then_terminal_failure_allows_new_job(self) -> None:
        first, _ = self.enqueue(max_attempts=2)
        self.store.claim_next(worker_id="w", stale_after_seconds=60)
        self.assertEqual(
            self.store.fail(
                first.job_id,
                "w",
                error_code="temporary",
                error_message="temporary",
                retryable=True,
                retry_delay_seconds=0,
            ),
            "queued",
        )
        second_attempt = self.store.claim_next(worker_id="w", stale_after_seconds=60)
        self.assertEqual(second_attempt.attempts, 2)
        self.assertEqual(
            self.store.fail(
                first.job_id,
                "w",
                error_code="temporary",
                error_message="safe public message",
                retryable=True,
            ),
            "failed",
        )
        replacement, created = self.enqueue()
        self.assertTrue(created)
        self.assertNotEqual(replacement.job_id, first.job_id)

    def test_queued_and_processing_cancellation(self) -> None:
        queued, _ = self.enqueue()
        self.assertEqual(
            self.store.request_cancel(queued.job_id, queued.owner_email), "cancelled"
        )
        self.assertEqual(self.store.get(queued.job_id).state, "cancelled")

        processing, _ = self.enqueue(dedupe_key="other")
        self.store.claim_next(worker_id="worker", stale_after_seconds=60)
        self.assertEqual(
            self.store.request_cancel(processing.job_id, processing.owner_email),
            "processing",
        )
        self.assertFalse(self.store.heartbeat(processing.job_id, "worker", 20))
        self.assertTrue(self.store.mark_cancelled(processing.job_id, "worker"))

    def test_stale_recovery_survives_store_restart(self) -> None:
        requeued, _ = self.enqueue(max_attempts=2, retention_seconds=1_000, now=100)
        self.store.claim_next(
            worker_id="dead", stale_after_seconds=10, now=100
        )
        restarted = JobStore(self.db)
        self.assertEqual(restarted.recover_stale(stale_before=150, now=200), 1)
        claimed = restarted.claim_next(
            worker_id="replacement", stale_after_seconds=10, now=200
        )
        self.assertEqual(claimed.job_id, requeued.job_id)
        self.assertEqual(claimed.attempts, 2)

        exhausted, _ = self.enqueue(
            dedupe_key="exhausted", max_attempts=1, retention_seconds=1_000, now=300
        )
        restarted.claim_next(
            worker_id="dead", stale_after_seconds=10, now=300
        )
        restarted.recover_stale(stale_before=350, now=400)
        self.assertEqual(restarted.get(exhausted.job_id).state, "failed")

    def test_expired_terminal_cleanup_keeps_active_jobs(self) -> None:
        expired, _ = self.enqueue(retention_seconds=1, now=10)
        with mock.patch("arcdb.jobs.time.time", return_value=10):
            self.store.request_cancel(expired.job_id, expired.owner_email)
        active, _ = self.enqueue(dedupe_key="active", now=10)
        removed = self.store.cleanup_expired(now=12)
        self.assertEqual([job.job_id for job in removed], [expired.job_id])
        self.assertIsNotNone(self.store.get(active.job_id))

    def test_expired_queued_job_is_cancelled_instead_of_claimed(self) -> None:
        expired, _ = self.enqueue(retention_seconds=1, now=10)
        claimed = self.store.claim_next(
            worker_id="worker", stale_after_seconds=60, now=12
        )
        self.assertIsNone(claimed)
        self.assertEqual(self.store.get(expired.job_id).state, "cancelled")


class PackageWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.store = JobStore(self.root / "jobs.sqlite3")
        self.limits = EpubLimits(
            max_entries=100,
            max_entry_bytes=4 * 1024 * 1024,
            max_total_uncompressed_bytes=16 * 1024 * 1024,
            max_text_entry_bytes=1024 * 1024,
        )
        self.settings = PackageWorkerSettings(
            sessions_dir=self.sessions,
            limits=self.limits,
            max_session_bytes=32 * 1024 * 1024,
            stale_after_seconds=10,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_job(self, *, valid: bool = True, max_attempts: int = 3):
        session_id = "b" * 32
        session = self.sessions / session_id
        (session / "images").mkdir(parents=True)
        if valid:
            make_epub(session / "base.epub")
        else:
            (session / "base.epub").write_bytes(b"not an epub")
        (session / "meta.json").write_text(
            json.dumps(
                {
                    "owner_email": "owner@example.test",
                    "struct_novel_dir": None,
                }
            ),
            encoding="utf-8",
        )
        job, _ = self.store.enqueue(
            kind="epub_package",
            owner_email="owner@example.test",
            payload={"session_id": session_id},
            dedupe_key=f"epub_package:{session_id}",
            max_attempts=max_attempts,
            timeout_seconds=30,
            retention_seconds=60,
        )
        return job, session

    def test_successful_job_publishes_valid_epub(self) -> None:
        job, session = self.create_job()
        result = run_one_job(self.store, self.settings, worker_id="worker")
        self.assertEqual(result.job_id, job.job_id)
        self.assertEqual(result.state, "done")
        self.assertEqual(result.progress, 100)
        self.assertEqual(result.result["session_id"], "b" * 32)
        validate_epub_archive(session / "final.epub", self.limits)

    def test_invalid_input_fails_without_retry(self) -> None:
        job, session = self.create_job(valid=False)
        result = run_one_job(self.store, self.settings, worker_id="worker")
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.error_code, "invalid_input")
        self.assertFalse((session / "final.epub").exists())

    def test_unexpected_failure_retries(self) -> None:
        job, _session = self.create_job(max_attempts=2)
        with mock.patch(
            "arcdb.package_worker.build_package", side_effect=OSError("private path")
        ):
            result = run_one_job(self.store, self.settings, worker_id="worker")
        self.assertEqual(result.job_id, job.job_id)
        self.assertEqual(result.state, "queued")
        self.assertEqual(result.error_code, "worker_error")
        self.assertNotIn("private path", result.error_message)

    def test_processing_cancellation_is_observed_by_worker(self) -> None:
        job, _session = self.create_job()

        def cancel_during_build(claimed, _settings, *, progress, cancellation_check):
            self.store.request_cancel(claimed.job_id, claimed.owner_email)
            cancellation_check()

        with mock.patch("arcdb.package_worker.build_package", cancel_during_build):
            result = run_one_job(self.store, self.settings, worker_id="worker")
        self.assertEqual(result.job_id, job.job_id)
        self.assertEqual(result.state, "cancelled")

    def test_timeout_is_retried_and_bounded_by_max_attempts(self) -> None:
        job, _session = self.create_job(max_attempts=1)
        with mock.patch(
            "arcdb.package_worker.build_package", side_effect=PackageJobTimedOut()
        ):
            result = run_one_job(self.store, self.settings, worker_id="worker")
        self.assertEqual(result.job_id, job.job_id)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "timeout")

    def test_expiry_cleanup_removes_only_valid_session_directory(self) -> None:
        session_id = "c" * 32
        session = self.sessions / session_id
        session.mkdir()
        (session / "disposable").write_text("fixture", encoding="utf-8")
        job, _ = self.store.enqueue(
            kind="epub_package",
            owner_email="owner@example.test",
            payload={"session_id": session_id},
            dedupe_key=f"epub_package:{session_id}",
            max_attempts=1,
            timeout_seconds=30,
            retention_seconds=1,
            now=1,
        )
        with mock.patch("arcdb.jobs.time.time", return_value=1):
            self.store.request_cancel(job.job_id, job.owner_email)
        with mock.patch("arcdb.jobs.time.time", return_value=3):
            self.assertEqual(
                cleanup_expired_job_artifacts(self.store, self.sessions), 1
            )
        self.assertFalse(session.exists())


if __name__ == "__main__":
    unittest.main()
