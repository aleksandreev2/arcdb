# ArchiveDB storage migration

## Goal

Move hot mutable state away from whole-file JSON rewrites while keeping the current Flask/API/UI behavior unchanged during migration and preserving a complete rollback path.

## Non-destructive invariant

Legacy state is **not deleted or modified by migration tooling**.

Migration and cleanup are separate operations. Even after SQLite becomes source of truth, legacy files must remain preserved for an agreed retention period. Any future deletion requires an explicit, separate decision.

## Phase 1 — SQLite shadow foundation

Status: implemented.

SQLite is a shadow state database. During Phase 2 the running Flask app still reads legacy JSON and writes JSON first.

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

## Schema v2

Current normalized state:

- `users`;
- `user_state_users`;
- `user_novel_state`;
- `collections`;
- `collection_items`;
- `user_uploads`;
- `custom_metadata`;
- `allowed_emails`.

`user_state_users` exists so a top-level legacy user bucket such as:

```json
{"user@example.com": {}}
```

can survive both initial migration and runtime deletion of the user's last per-novel row. This avoids relying on an old imported document snapshot to preserve an empty live container.

Each migrated record still keeps its original JSON payload during the transition. `legacy_documents` stores imported source-document representations for audit and migration round-trip verification.

## Safe initial migration algorithm

The migration tool uses this sequence:

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

If source state changes while the candidate is being built, promotion fails closed. On production prefer a short maintenance/read-only window for the first import, or use a deliberately designed consistent snapshot protocol.

## Backup scope

The snapshot is intentionally broader than the normalized schema.

It includes:

- every existing file recursively under `META_DIR`;
- users/user-data/collections/upload/custom-meta/allowlist paths even when explicitly configured;
- translated CSV index;
- RAW master CSV index.

Therefore unknown or production-only legacy files are still preserved even before they have normalized tables.

## Running the initial migration locally

After dev data is seeded:

```bat
.venv\Scripts\python.exe scripts\migrate_state_to_sqlite.py --verify
```

Production-oriented strict check:

```bash
python scripts/migrate_state_to_sqlite.py --verify --require-core
```

Prepare/verify without changing the current SQLite target:

```bash
python scripts/migrate_state_to_sqlite.py --verify --require-core --no-promote
```

## Migration artifacts

Typical local layout:

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

Local bootstrap has one additional convenience path: when a **local-development-only shadow** is stale after reseeding, it first checks that no local ArchiveDB server is listening, checkpoints the shadow WAL, preserves any remaining sidecars in `data/shadow-sidecar-backups/`, then invokes the same safe migration. This automatic handling is not a production procedure.

## Phase 2 — runtime dual-write

Status: in progress. Phase 2A is implemented.

### Write order

For covered state mutations:

```text
Flask route
   -> mutate legacy in memory
   -> atomic legacy JSON write succeeds
   -> compute changed per-novel records
   -> SQLite transaction mirrors those records
   -> optional immediate SQLite verification
```

JSON is deliberately written first and remains the read/source-of-truth during this phase.

If SQLite fails after JSON succeeds:

- local strict mode raises so tests/development notice immediately;
- JSON remains the authoritative successful write;
- the next local startup detects full parity failure and can rebuild the shadow safely from JSON;
- production should log/alert on the mismatch and keep serving the legacy source until investigated.

### Phase 2A covered mutations

Current runtime wiring mirrors changes triggered by:

- `/api/user_progress`;
- `/api/user_status`;
- `/api/user_hide`;
- `/api/bulk_remove`;
- local EPUB download counter updates;
- Telegram download counter updates.

For each changed per-novel record it mirrors:

- `status`;
- `progress`;
- `last_read`;
- `dl` -> normalized download count;
- `last_dl`;
- `hidden`;
- complete raw JSON payload;
- embedded collection memberships for that record.

This does **not** mean collection routes are fully migrated. Collection metadata and every membership mutation path are Phase 2B.

### Feature flags

Local defaults:

```text
STATE_DUAL_WRITE=1
STATE_DUAL_WRITE_STRICT=1
STATE_DUAL_WRITE_VERIFY=1
STATE_DUAL_WRITE_LOG_SUCCESS=0
```

Semantics:

- `STATE_DUAL_WRITE` — enable shadow writes;
- `STATE_DUAL_WRITE_STRICT` — re-raise mirror failures after the JSON write; intended for local/CI, not a substitute for production monitoring;
- `STATE_DUAL_WRITE_VERIFY` — re-read and compare affected SQLite rows immediately;
- `STATE_DUAL_WRITE_LOG_SUCCESS` — optional noisy success logging.

Production dual-write remains opt-in and must not be enabled until a verified production SQLite shadow exists.

### Full parity check

Run:

```bat
.venv\Scripts\python.exe scripts\verify_state_parity.py
```

It opens SQLite read-only and verifies the entire semantic `user_data.json` document against the SQLite shadow, including empty user buckets and raw per-record payloads.

Local `start.bat` performs this parity check before starting. A compatible equal shadow is reused. A missing/stale local shadow is rebuilt safely from JSON. Automatic rebuild is refused outside `ARCHIVEDB_LOCAL_DEV=1`.

### Runtime overlay mechanism

The Flask source is still reconstructed from a verified compressed baseline. Until it becomes a normally tracked package, `scripts/runtime_overlays.py` applies the dual-write hook during materialization.

The overlay is intentionally fail-closed:

- exact code markers must match once;
- unexpected baseline changes fail materialization;
- overlay file hash participates in `.runtime.sha256`;
- this is transitional plumbing, not the target architecture.

### Phase 2B — collections

Next scope:

- collection create/rename/delete;
- every add/remove membership route;
- bulk operations;
- full `collections.json` + membership parity.

### Phase 2C — uploads/custom metadata/allowlist

Then mirror:

- upload metadata/approval state;
- custom metadata;
- allowlist changes;
- corresponding admin mutations.

### Phase 2D — users/auth

Migrate auth last within the write phase with dedicated registration/login/verification/reset tests.

## Phase 3 — SQLite read comparison/cutover

Only after sufficient write-domain coverage:

- add read-source feature flag;
- run seeded API parity tests;
- enable SQLite reads in local/internal scope;
- observe mismatches/errors;
- switch primary reads to SQLite;
- retain legacy files and rollback controls.

Do not combine a new dual-write domain and read-source cutover in one change.

## Phase 4 — stop whole-file JSON writes

Only after SQLite reads are stable:

- stop legacy writes one domain at a time;
- keep explicit SQLite -> legacy export tooling;
- archive legacy files read-only;
- do not delete them as part of the cutover.

## Not part of this state migration

The following remain separate migrations/projects:

- novel library index;
- EPUB/chapter files;
- Telegram session state;
- packaging jobs;
- process-local rate-limit buckets;
- download event logs not represented in current state tables;
- Cloudflare/R2 object migration;
- filesystem cleanup/reorganization.

## Production checklist before enabling shadow writes

- reconcile repository baseline with live code/config;
- inventory real source paths;
- identify all metadata/state files;
- check free disk space;
- create OCI Block Volume backup/snapshot when practical;
- stop/quiesce writers for the initial consistent snapshot;
- run initial migration with `--require-core`;
- verify backup manifest and SQLite checks;
- keep `STATE_DUAL_WRITE=0` until the shadow is verified and runtime code is reconciled with live production;
- enable dual-write for a bounded domain first;
- monitor mismatch/error logs;
- do not enable SQLite reads yet.

See `docs/PRODUCTION_SAFETY.md` for the operational protocol.
