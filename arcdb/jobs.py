"""Persistent SQLite queue for ArchiveDB background work."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any, Mapping


JOB_STATES = ("queued", "processing", "done", "failed", "cancelled")
TERMINAL_JOB_STATES = ("done", "failed", "cancelled")
JOB_SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS job_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_email TEXT NOT NULL COLLATE NOCASE,
    dedupe_key TEXT,
    state TEXT NOT NULL CHECK (state IN ('queued','processing','done','failed','cancelled')),
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    timeout_seconds INTEGER NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
    worker_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    available_at REAL NOT NULL,
    started_at REAL,
    heartbeat_at REAL,
    finished_at REAL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs(state, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_owner
    ON jobs(owner_email, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_heartbeat
    ON jobs(state, heartbeat_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_dedupe
    ON jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND state IN ('queued','processing','done');
"""


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: str
    owner_email: str
    dedupe_key: str | None
    state: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    attempts: int
    max_attempts: int
    progress: int
    timeout_seconds: int
    cancel_requested: bool
    worker_id: str | None
    created_at: float
    updated_at: float
    available_at: float
    started_at: float | None
    heartbeat_at: float | None
    finished_at: float | None
    expires_at: float


def _json_object(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Job JSON fields must contain objects.")
    return value


def _job_from_row(row: sqlite3.Row | None) -> Job | None:
    if row is None:
        return None
    return Job(
        job_id=str(row["job_id"]),
        kind=str(row["kind"]),
        owner_email=str(row["owner_email"]),
        dedupe_key=row["dedupe_key"],
        state=str(row["state"]),
        payload=_json_object(row["payload_json"]) or {},
        result=_json_object(row["result_json"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        progress=int(row["progress"]),
        timeout_seconds=int(row["timeout_seconds"]),
        cancel_requested=bool(row["cancel_requested"]),
        worker_id=row["worker_id"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        available_at=float(row["available_at"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        heartbeat_at=(
            float(row["heartbeat_at"]) if row["heartbeat_at"] is not None else None
        ),
        finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
        expires_at=float(row["expires_at"]),
    )


class JobStore:
    """Small multi-process-safe queue backed by one SQLite WAL database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            version = conn.execute(
                "SELECT value FROM job_schema_meta WHERE key='schema_version'"
            ).fetchone() if self._has_meta(conn) else None
            if version is not None and str(version[0]) != str(JOB_SCHEMA_VERSION):
                raise RuntimeError(
                    "Unsupported package job schema version "
                    f"{version[0]}; expected {JOB_SCHEMA_VERSION}."
                )
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO job_schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(JOB_SCHEMA_VERSION),),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _has_meta(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_schema_meta'"
        ).fetchone() is not None

    def enqueue(
        self,
        *,
        kind: str,
        owner_email: str,
        payload: Mapping[str, Any],
        dedupe_key: str | None = None,
        max_attempts: int = 3,
        timeout_seconds: int = 900,
        retention_seconds: int = 86_400,
        now: float | None = None,
    ) -> tuple[Job, bool]:
        if not kind or not owner_email or max_attempts <= 0 or timeout_seconds <= 0:
            raise ValueError("Invalid job configuration.")
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive.")
        created_at = time.time() if now is None else now
        job_id = uuid.uuid4().hex
        encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
        try:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO jobs("
                    "job_id,kind,owner_email,dedupe_key,state,payload_json,max_attempts,"
                    "timeout_seconds,created_at,updated_at,available_at,expires_at"
                    ") VALUES(?,?,?,?, 'queued', ?,?,?,?,?,?,?)",
                    (
                        job_id,
                        kind,
                        owner_email.strip().lower(),
                        dedupe_key,
                        encoded,
                        max_attempts,
                        timeout_seconds,
                        created_at,
                        created_at,
                        created_at,
                        created_at + retention_seconds,
                    ),
                )
                row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                return _job_from_row(row), True  # type: ignore[return-value]
        except sqlite3.IntegrityError:
            if not dedupe_key:
                raise
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedupe_key=? "
                    "AND state IN ('queued','processing','done') ORDER BY created_at DESC LIMIT 1",
                    (dedupe_key,),
                ).fetchone()
            job = _job_from_row(row)
            if job is None:
                raise
            return job, False

    def get(self, job_id: str) -> Job | None:
        with self._connection() as conn:
            return _job_from_row(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )

    def get_owned(self, job_id: str, owner_email: str) -> Job | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=? AND owner_email=? COLLATE NOCASE",
                (job_id, owner_email.strip().lower()),
            ).fetchone()
            return _job_from_row(row)

    def get_active_by_dedupe(self, dedupe_key: str) -> Job | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE dedupe_key=? "
                "AND state IN ('queued','processing','done') "
                "ORDER BY created_at DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            return _job_from_row(row)

    @staticmethod
    def _recover_stale_in_transaction(
        conn: sqlite3.Connection,
        *,
        stale_before: float,
        now: float,
    ) -> int:
        cancelled = conn.execute(
            "UPDATE jobs SET state='cancelled',finished_at=?,updated_at=?,"
            "expires_at=?+(expires_at-created_at),worker_id=NULL,"
            "heartbeat_at=NULL,error_code=NULL,error_message=NULL "
            "WHERE state='processing' AND cancel_requested=1 "
            "AND COALESCE(heartbeat_at,started_at,updated_at)<?",
            (now, now, now, stale_before),
        ).rowcount
        failed = conn.execute(
            "UPDATE jobs SET state='failed',finished_at=?,updated_at=?,"
            "expires_at=?+(expires_at-created_at),worker_id=NULL,"
            "heartbeat_at=NULL,error_code='stale_attempts_exhausted',"
            "error_message='The worker stopped before this job could finish.' "
            "WHERE state='processing' AND cancel_requested=0 AND attempts>=max_attempts "
            "AND COALESCE(heartbeat_at,started_at,updated_at)<?",
            (now, now, now, stale_before),
        ).rowcount
        requeued = conn.execute(
            "UPDATE jobs SET state='queued',available_at=?,updated_at=?,worker_id=NULL,"
            "heartbeat_at=NULL,started_at=NULL,progress=0,error_code=NULL,error_message=NULL "
            "WHERE state='processing' AND cancel_requested=0 AND attempts<max_attempts "
            "AND COALESCE(heartbeat_at,started_at,updated_at)<?",
            (now, now, stale_before),
        ).rowcount
        return cancelled + failed + requeued

    def recover_stale(self, stale_before: float, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._recover_stale_in_transaction(
                conn, stale_before=stale_before, now=current
            )

    def claim_next(
        self,
        *,
        worker_id: str,
        stale_after_seconds: int,
        kind: str | None = None,
        now: float | None = None,
    ) -> Job | None:
        current = time.time() if now is None else now
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._recover_stale_in_transaction(
                conn,
                stale_before=current - stale_after_seconds,
                now=current,
            )
            conn.execute(
                "UPDATE jobs SET state='cancelled',cancel_requested=1,finished_at=?,"
                "updated_at=?,expires_at=?+(expires_at-created_at) "
                "WHERE state='queued' AND expires_at<?",
                (current, current, current, current),
            )
            params: list[Any] = [current]
            kind_clause = ""
            if kind:
                kind_clause = " AND kind=?"
                params.append(kind)
            row = conn.execute(
                "SELECT job_id FROM jobs WHERE state='queued' AND cancel_requested=0 "
                "AND available_at<=?" + kind_clause + " ORDER BY created_at,job_id LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            job_id = str(row[0])
            updated = conn.execute(
                "UPDATE jobs SET state='processing',attempts=attempts+1,progress=1,"
                "worker_id=?,started_at=?,heartbeat_at=?,updated_at=? "
                "WHERE job_id=? AND state='queued' AND cancel_requested=0",
                (worker_id, current, current, current, job_id),
            ).rowcount
            if updated != 1:
                return None
            return _job_from_row(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )

    def heartbeat(self, job_id: str, worker_id: str, progress: int) -> bool:
        current = time.time()
        bounded_progress = max(1, min(99, int(progress)))
        with self._connection() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id=? AND state='processing' "
                "AND worker_id=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None or bool(row[0]):
                return False
            conn.execute(
                "UPDATE jobs SET heartbeat_at=?,updated_at=?,progress=? "
                "WHERE job_id=? AND state='processing' AND worker_id=?",
                (current, current, bounded_progress, job_id, worker_id),
            )
            return True

    def complete(self, job_id: str, worker_id: str, result: Mapping[str, Any]) -> bool:
        current = time.time()
        with self._connection() as conn:
            return conn.execute(
                "UPDATE jobs SET state='done',result_json=?,progress=100,finished_at=?,"
                "updated_at=?,heartbeat_at=?,expires_at=?+(expires_at-created_at),"
                "worker_id=NULL,error_code=NULL,error_message=NULL "
                "WHERE job_id=? AND state='processing' AND worker_id=? AND cancel_requested=0",
                (
                    json.dumps(dict(result), ensure_ascii=False, sort_keys=True),
                    current,
                    current,
                    current,
                    current,
                    job_id,
                    worker_id,
                ),
            ).rowcount == 1

    def fail(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: float = 1.0,
    ) -> str | None:
        current = time.time()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts,max_attempts,cancel_requested FROM jobs "
                "WHERE job_id=? AND state='processing' AND worker_id=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                return None
            if bool(row[2]):
                state = "cancelled"
                conn.execute(
                    "UPDATE jobs SET state='cancelled',finished_at=?,updated_at=?,"
                    "expires_at=?+(expires_at-created_at),"
                    "worker_id=NULL,heartbeat_at=NULL,error_code=NULL,error_message=NULL "
                    "WHERE job_id=?",
                    (current, current, current, job_id),
                )
            elif retryable and int(row[0]) < int(row[1]):
                state = "queued"
                conn.execute(
                    "UPDATE jobs SET state='queued',available_at=?,updated_at=?,progress=0,"
                    "worker_id=NULL,heartbeat_at=NULL,started_at=NULL,error_code=?,error_message=? "
                    "WHERE job_id=?",
                    (
                        current + max(0.0, retry_delay_seconds),
                        current,
                        error_code,
                        error_message,
                        job_id,
                    ),
                )
            else:
                state = "failed"
                conn.execute(
                    "UPDATE jobs SET state='failed',finished_at=?,updated_at=?,"
                    "expires_at=?+(expires_at-created_at),worker_id=NULL,"
                    "heartbeat_at=NULL,error_code=?,error_message=? WHERE job_id=?",
                    (current, current, current, error_code, error_message, job_id),
                )
            return state

    def request_cancel(self, job_id: str, owner_email: str) -> str | None:
        current = time.time()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM jobs WHERE job_id=? AND owner_email=? COLLATE NOCASE",
                (job_id, owner_email.strip().lower()),
            ).fetchone()
            if row is None:
                return None
            state = str(row[0])
            if state == "queued":
                conn.execute(
                    "UPDATE jobs SET state='cancelled',cancel_requested=1,progress=0,"
                    "finished_at=?,updated_at=?,expires_at=?+(expires_at-created_at) "
                    "WHERE job_id=?",
                    (current, current, current, job_id),
                )
                return "cancelled"
            if state == "processing":
                conn.execute(
                    "UPDATE jobs SET cancel_requested=1,updated_at=? WHERE job_id=?",
                    (current, job_id),
                )
            return state

    def mark_cancelled(self, job_id: str, worker_id: str) -> bool:
        current = time.time()
        with self._connection() as conn:
            return conn.execute(
                "UPDATE jobs SET state='cancelled',cancel_requested=1,finished_at=?,"
                "updated_at=?,expires_at=?+(expires_at-created_at),worker_id=NULL,"
                "heartbeat_at=NULL,error_code=NULL,error_message=NULL "
                "WHERE job_id=? AND state='processing' AND worker_id=?",
                (current, current, current, job_id, worker_id),
            ).rowcount == 1

    def cleanup_expired(self, now: float | None = None) -> list[Job]:
        current = time.time() if now is None else now
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('done','failed','cancelled') "
                "AND expires_at<? ORDER BY expires_at",
                (current,),
            ).fetchall()
            jobs = [_job_from_row(row) for row in rows]
            conn.executemany(
                "DELETE FROM jobs WHERE job_id=?",
                [(job.job_id,) for job in jobs if job is not None],
            )
            return [job for job in jobs if job is not None]
