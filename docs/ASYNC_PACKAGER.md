# Persistent async EPUB packager

## Status

Implemented in the repository. This document is not evidence that the service is
enabled in production. Production paths and unit ownership remain inventory inputs.

The web process now validates and stores a package session, then enqueues an
`epub_package` job in a dedicated SQLite WAL database. It does not build the final
EPUB inside the HTTP request. `scripts/run_packager.py` owns job execution and uses
the bounded, atomic EPUB pipeline from `arcdb/epub_io.py`.

The job database is intentionally separate from state schema v3. Adding the queue
does not upgrade, replace, or make the candidate state database authoritative.

## API

Either existing finalize route or the generic package route enqueues work:

```http
POST /api/epub_package/finalize/<session_id>
POST /api/jobs/package
Content-Type: application/json

{"session_id":"<32 hex characters>"}
```

Both return HTTP 202 with `job_id`, `state`, `progress`, attempts and `status_url`.
Repeated enqueue for the same active/completed session returns the same job.

```http
GET /api/jobs/<job_id>
POST /api/jobs/<job_id>/cancel
```

Only the authenticated owner can see or cancel a job. Public job responses contain
no filesystem paths, payloads or worker identity. A completed response includes the
existing authenticated `download_url`.

States are `queued`, `processing`, `done`, `failed` and `cancelled`.

## Persistence and recovery

The queue uses WAL, atomic `BEGIN IMMEDIATE` claims and one owner worker per claimed
row. Attempts, heartbeat, progress, timeout, cancellation and result metadata are
persistent. On every claim, stale `processing` rows are requeued while attempts
remain, or failed after the attempt budget is exhausted. Completed/failed/cancelled
rows and their disposable session directories expire after the retention window.

The final EPUB is written beside the session as a temporary file, fsynced, validated
and atomically published. Worker failure, cancellation or timeout removes the
partial output and leaves the base EPUB/session available for retry.

## Configuration

```env
PACKAGE_JOBS_DB_PATH=/path/on/block-volume/package_jobs.sqlite3
EPUB_PACKAGE_SESSIONS_DIR=/path/on/block-volume/epub_package_sessions
PACKAGE_JOB_MAX_ATTEMPTS=3
PACKAGE_JOB_TIMEOUT_SECONDS=900
PACKAGE_JOB_STALE_SECONDS=60
PACKAGE_JOB_RETENTION_SECONDS=86400
EPUB_PACKAGE_SESSION_TTL_SECONDS=86400
```

The queue database and session directory must be on durable local block storage and
accessible to both web and packager service accounts. Do not put them in R2, an
ephemeral system temp filesystem, or a web-worker-private directory.

## Local operation

`start.bat` uses `scripts/dev_bootstrap.py`, which launches Flask and the packager as
separate child processes and stops both together. To run only the worker:

```text
.venv/Scripts/python.exe scripts/run_packager.py
```

Use `--once` for a single queued job. It returns non-zero if that attempt did not
reach `done` or `cancelled`.

## Production rollout and rollback

1. Reconcile actual application, environment, block-volume and service paths.
2. Put both queue/session paths on the verified block volume and back up the env.
3. Install `deploy/systemd/arcdb-packager.service.example` only after replacing its
   placeholders with inventory-confirmed values.
4. Start one packager and verify a fixture enqueue, status progression, download,
   restart recovery and cancellation before normal traffic.
5. Monitor service restarts, queued age, failures and available storage.

Rollback: stop the packager, revert the application revision and preserve the queue
database/session directory for diagnosis. This does not require deleting legacy JSON,
the SQLite state candidate, uploaded EPUBs or package inputs.
