# ArchiveDB storage migration

## Goal

Move hot mutable state away from whole-file JSON rewrites while keeping the current Flask/API/UI behavior unchanged during migration.

## Phase 1 — SQLite foundation (this branch)

SQLite is introduced as a **shadow state database** only. The running Flask app still reads/writes the legacy JSON files.

Local path:

```text
./data/arcdb.sqlite3
```

Connection defaults:

- WAL journal mode
- `synchronous=NORMAL`
- `busy_timeout=5000`
- foreign keys enabled
- normal SQLite file on OCI Block Volume in production later

The initial schema normalizes the highest-value state:

- `users`
- `user_novel_state`
- `collections`
- `collection_items`
- `user_uploads`
- `custom_metadata`
- `allowed_emails`

Each migrated record also keeps its original JSON payload during the transition. `legacy_documents` stores the imported source snapshots so migration can be audited and rolled back safely.

Run locally after dev data is seeded:

```bat
.venv\Scripts\python.exe scripts\migrate_state_to_sqlite.py --verify
```

`--verify` reconstructs the supported JSON documents from SQLite and compares them with the source documents before reporting success.

## Phase 2 — dual-write adapter

Add a state repository interface and route these operations through it:

1. users/auth
2. user reading status/progress/download counters
3. collections and memberships
4. user uploads
5. custom metadata / allowlist

For a short transition period writes will go to SQLite and legacy JSON. Reads will still use JSON by default, with a local feature flag allowing SQLite reads for comparison.

## Phase 3 — SQLite becomes source of truth

After API parity tests pass:

- switch reads to SQLite
- stop whole-file JSON writes
- keep one-time JSON export tooling for rollback/backup
- remove process-local locks that only existed to protect JSON files

This is the point where multiple Gunicorn workers become practical for the web process.

## Not in this migration yet

The novel library index itself, EPUB/chapter files, Telegram state, packaging jobs, rate-limit buckets and download event logs are separate migrations. They should not be mixed into the first state-storage cutover.
