# ArchiveDB storage migration

## Goal

Move hot mutable state away from whole-file JSON rewrites while keeping the current Flask/API/UI behavior unchanged during migration and preserving a complete rollback path.

## Non-destructive invariant

Legacy state is **not deleted or modified** by the migration tool.

Migration and cleanup are separate operations. Even after SQLite becomes source of truth, legacy files must remain preserved for an agreed retention period. Any future deletion requires an explicit, separate decision.

## Phase 1 — SQLite shadow foundation

Status: implemented.

SQLite is introduced as a shadow state database. The running Flask baseline still reads/writes the legacy JSON files.

Local target path:

```text
./data/arcdb.sqlite3
```

Connection defaults:

- WAL journal mode;
- `synchronous=NORMAL`;
- `busy_timeout=5000`;
- foreign keys enabled;
- SQLite file intended for OCI Block Volume in production.

Schema v1 normalizes:

- `users`;
- `user_novel_state`;
- `collections`;
- `collection_items`;
- `user_uploads`;
- `custom_metadata`;
- `allowed_emails`.

Each migrated record also keeps its original JSON payload during the transition. `legacy_documents` stores imported source-document representations for audit and round-trip verification.

## Safe migration algorithm

The migration tool now uses this sequence:

```text
1. discover all files under META_DIR + explicit CSV/state files
2. create timestamped backup directory
3. copy every discovered file byte-for-byte
4. verify backup SHA-256 == source SHA-256
5. build a separate candidate SQLite database
6. verify JSON <-> SQLite round-trip
7. PRAGMA quick_check
8. PRAGMA integrity_check
9. PRAGMA foreign_key_check
10. verify schema version
11. re-hash all tracked source files and compare with pre-migration hashes
12. detect added/removed files in the metadata snapshot scope
13. only then promote the candidate
14. if a prior SQLite target exists, preserve it before promotion
```

If source state changes while the candidate is being built, promotion fails closed. This is important for production: prefer a short maintenance/read-only window for the first import, or use a later consistent dual-write protocol.

## Backup scope

The snapshot is intentionally broader than schema v1.

It includes:

- every existing file recursively under `META_DIR`;
- users/user-data/collections/upload/custom-meta/allowlist paths even when explicitly configured;
- translated CSV index;
- RAW master CSV index.

Therefore legacy files that SQLite v1 does not yet understand are still preserved in the migration backup.

Examples may include IP/exemption state, community/download-related state or other future/production-only metadata files discovered under `META_DIR`.

## Running locally

After dev data is seeded:

```bat
.venv\Scripts\python.exe scripts\migrate_state_to_sqlite.py --verify
```

Verification is mandatory now; `--verify` remains accepted for compatibility/clarity.

Production-oriented strict check:

```bash
python scripts/migrate_state_to_sqlite.py --verify --require-core
```

`--require-core` refuses to continue if core users/user_data/collections source files are unexpectedly missing.

Prepare/verify without changing the current SQLite target:

```bash
python scripts/migrate_state_to_sqlite.py --verify --require-core --no-promote
```

This leaves the verified candidate beside the target and records its path/hash in the timestamped manifest.

## Migration artifacts

Default local layout:

```text
data/
├── arcdb.sqlite3
└── migration-backups/
    └── YYYYMMDD-HHMMSS-PID/
        ├── manifest.json
        ├── legacy-files/
        │   ├── metadata/
        │   └── external/
        └── previous-sqlite/
            ├── arcdb.sqlite3.verified-copy
            └── arcdb.sqlite3.pre-migration-original
```

When no previous SQLite target exists, `previous-sqlite/` may not exist.

The manifest records source paths, backup paths, file sizes, SHA-256 values, candidate/target information, row counts and SQLite verification results.

## Existing SQLite target behavior

A previous SQLite database is never silently discarded.

Before promotion:

1. migration refuses to replace a target if `-wal`/`-shm` sidecars indicate an active/uncheckpointed database;
2. previous DB gets a checksum-verified copy in the migration backup;
3. original DB is moved to a `pre-migration-original` backup name;
4. candidate is renamed into the target path;
5. target is verified after promotion;
6. if post-promotion verification fails, the previous original is restored automatically when available.

This intentionally uses extra disk space in exchange for rollback safety.

## Source mutation guarantee

The migration tool never writes to legacy metadata/CSV source files.

It fingerprints source files before candidate creation and re-checks them before promotion. It also compares the set of files under the metadata snapshot scope so newly created or removed unknown metadata files cause promotion to abort.

## Phase 2 — runtime dual-write adapter

Next planned stage.

Add a state repository interface and route writes through it, domain by domain:

1. reading progress/status/hidden/download counters;
2. collections/memberships;
3. uploads/custom metadata/allowlist;
4. users/auth with dedicated tests.

For a transition period writes go to both SQLite and legacy JSON. Reads remain JSON-first.

After each supported write in development/CI, compare the resulting semantic state. Production should log mismatches rather than silently choosing one copy.

## Phase 3 — SQLite read comparison/cutover

After dual-write parity is stable:

- add read-source feature flag;
- run seeded API parity tests;
- enable SQLite reads in local/internal scope;
- observe mismatches/errors;
- switch primary reads to SQLite;
- retain legacy files and rollback controls.

## Phase 4 — stop whole-file JSON writes

Only after SQLite reads are stable:

- stop legacy writes one domain at a time;
- keep explicit SQLite -> legacy export tooling;
- archive legacy files read-only;
- do not delete them as part of the cutover.

## Not part of state migration v1

The following must remain separate migrations/projects:

- novel library index;
- EPUB/chapter files;
- Telegram state;
- packaging jobs;
- rate-limit buckets;
- download event logs not represented in current state tables;
- Cloudflare/R2 object migration;
- filesystem cleanup/reorganization.

Do not mix these with the first user-state production cutover.

## Production checklist

Before first production migration:

- reconcile repository baseline with live code/config;
- inventory real source paths;
- identify all metadata/state files;
- check free disk space;
- create OCI Block Volume backup/snapshot when practical;
- stop or quiesce writers for consistent initial snapshot;
- run migration with `--require-core`;
- retain backup manifest and all legacy files;
- smoke test application;
- do not enable SQLite reads until dual-write/parity stages are ready.

See `docs/PRODUCTION_SAFETY.md` for the full operational protocol.