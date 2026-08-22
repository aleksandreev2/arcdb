# ArchiveDB SQLite backup and restore

This runbook covers repository-side state migration, recurring SQLite backup and
verified restoration. It does not assume remote access or a production path. The
operator must always pass explicit, already-confirmed paths.

## What is protected

Two different backup types are intentionally retained:

1. A migration snapshot preserves authoritative legacy JSON/CSV files and any
   previous SQLite database while a candidate is built and promoted.
2. An operational SQLite backup preserves a consistent committed database state,
   including committed pages still present only in WAL, after SQLite exists.

Neither backup type deletes or replaces legacy data. An SQLite backup does not
replace the migration snapshot or the tested SQLite-to-legacy export path.

## 1. Prepare a legacy-to-SQLite migration

Configure explicit paths in the private `.env`, then run with core legacy files
required:

```bash
PYTHONPATH=. python scripts/migrate_state_to_sqlite.py \
  --db /explicit/data/arcdb.sqlite3 \
  --backup-root /explicit/backups/migrations \
  --require-core \
  --verify
```

The command snapshots and hashes the legacy source, builds a separate candidate,
checks semantic round-trip/schema/integrity/foreign keys, rechecks source hashes,
preserves a prior SQLite target and only then promotes the candidate. It refuses an
active target with WAL/SHM sidecars.

Independently verify the new timestamped migration directory printed by the command:

```bash
PYTHONPATH=. python scripts/verify_migration_backup.py \
  /explicit/backups/migrations/TIMESTAMP \
  --check-current-sources
```

For a rehearsal that must not change the configured SQLite target, add
`--no-promote` to the migration command.

The library index and package-jobs database are operational/derived databases, not
schema-v3 state backups. Migration excludes their explicitly configured paths and
WAL/SHM sidecars from the immutable legacy snapshot while still preserving every
unknown metadata file. Prefer storing them outside the legacy metadata root; rebuild
the library index separately with `scripts/reindex_library.py`.

## 2. Create a WAL-aware operational backup

Choose a new directory for every backup. Existing directories are never overwritten:

```bash
PYTHONPATH=. python scripts/create_sqlite_backup.py \
  --db /explicit/data/arcdb.sqlite3 \
  --backup-dir /explicit/backups/sqlite/2026-08-22T120000Z
```

The command uses SQLite's online backup API rather than filesystem-copying the live
database. This provides one consistent committed snapshot while correctly including
committed WAL pages. It then:

- converts the artifact to a portable single-file database;
- runs `quick_check`, `integrity_check` and `foreign_key_check`;
- checks the expected ArchiveDB schema and application-level row query;
- records SHA-256 and size in `manifest.json`;
- restores to a temporary location and opens it through the runtime SQLite adapter;
- publishes the backup directory only after all checks pass.

The manifest records only the source filename, WAL/SHM presence and aggregate row
counts/check results. It does not record absolute paths, state payloads or identities.

## 3. Independently verify a retained backup

Run this after creation and periodically for retained copies:

```bash
PYTHONPATH=. python scripts/verify_sqlite_backup.py \
  /explicit/backups/sqlite/2026-08-22T120000Z \
  --restore-temp-parent /explicit/restore-test-space
```

Verification fails closed on a missing/changed manifest, size or checksum mismatch,
unexpected WAL/SHM sidecars, schema/integrity/FK failure, row-count mismatch or a
failed temporary runtime restore.

## 4. Restore to a new target

Never restore over the active path. Restore to a new path, verify it, and make
cutover a separate controlled operation:

```bash
PYTHONPATH=. python scripts/restore_sqlite_backup.py \
  --backup-dir /explicit/backups/sqlite/2026-08-22T120000Z \
  --target-db /explicit/restore/arcdb.sqlite3
```

The command re-verifies the backup before copying, refuses an existing target or
target sidecars, verifies the incomplete copy, and atomically publishes the new file.
It never replaces the active database or modifies legacy files.

Before an actual application cutover, stop or quiesce writers, retain the current
database and its sidecars together, verify the restored database again, point the
application at the new explicit path, and run application smoke/parity checks. If
checks fail, point the application back to the retained previous path. Do not delete
legacy JSON after a successful restore.

## Windows commands

The same tools work from the repository virtual environment:

```bat
.venv\Scripts\python.exe scripts\migrate_state_to_sqlite.py --db D:\arcdb\data\arcdb.sqlite3 --backup-root D:\arcdb\backups\migrations --require-core --verify
.venv\Scripts\python.exe scripts\create_sqlite_backup.py --db D:\arcdb\data\arcdb.sqlite3 --backup-dir D:\arcdb\backups\sqlite\2026-08-22T120000Z
.venv\Scripts\python.exe scripts\verify_sqlite_backup.py D:\arcdb\backups\sqlite\2026-08-22T120000Z
.venv\Scripts\python.exe scripts\restore_sqlite_backup.py --backup-dir D:\arcdb\backups\sqlite\2026-08-22T120000Z --target-db D:\arcdb\restore\arcdb.sqlite3
```

## Retention and failure rules

- Keep the immutable pre-migration legacy snapshot and migration manifest.
- Keep at least one independently verified operational SQLite backup outside the
  active database directory.
- Use a new timestamped directory for each run; do not edit a published backup.
- Copy retained backups to separate failure-domain storage when available, then run
  the independent verifier against that copy.
- Never treat a backup as usable until its restore test passes.
- Never delete legacy data based on the SQLite backup manifest; its explicit decision
  remains `safe_to_delete_legacy_data: false`.
- A storage-level volume snapshot is an additional recovery layer, not a substitute
  for this application-level backup and restore verification.
