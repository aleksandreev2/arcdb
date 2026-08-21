# Production migration and rollback safety

This document defines invariants that must hold before any production storage cutover.

## Core invariant

A migration must never require deleting the only copy of user state.

Legacy JSON/CSV/TXT files are retained throughout SQLite migration and remain available for rollback. Removing them is not part of migration and must never be bundled into an unrelated deployment.

## Required preconditions

Before touching production state:

1. identify the exact live state paths;
2. stop guessing about `/home/ubuntu/...` or local development paths;
3. record application/version/repository commit being deployed;
4. record filesystem free space;
5. ensure enough free space for at least:
   - one full source-state snapshot;
   - one candidate SQLite database;
   - one previous SQLite backup;
6. ensure the migration user can create files in the target directories;
7. do not run the migration during another state-format migration.

For the first production pass, prefer a brief maintenance/read-only window so source state cannot change between snapshot and cutover. If no maintenance window is possible, runtime dual-write and a more careful consistent-snapshot protocol are required.

## Recommended two-step production use

After production paths in `.env` have been verified, first prepare without promotion:

```bash
python scripts/migrate_state_to_sqlite.py \
  --verify \
  --require-core \
  --no-promote
```

This creates:

- timestamped byte-for-byte legacy snapshot;
- `manifest.json` with SHA-256/size/path information;
- a fully verified candidate SQLite database;
- no change to the current SQLite target.

Inspect the printed paths and verify the backup independently:

```bash
python scripts/verify_migration_backup.py /path/to/migration-backups/TIMESTAMP \
  --check-current-sources
```

For the actual promotion, rerun the migration without `--no-promote` while writers are quiesced:

```bash
python scripts/migrate_state_to_sqlite.py \
  --verify \
  --require-core
```

The tool creates a fresh verified snapshot/candidate again before promotion. Do not promote an old candidate after production state has changed.

## Safe migration protocol

### Step 1 — discover source files

Record the exact paths of:

- users state;
- user novel/progress state;
- collections;
- user uploads metadata;
- custom metadata;
- allowlist;
- any other state discovered on the live host.

Do not silently treat a missing expected production file as an empty document. A production migration should fail closed when a required live source unexpectedly disappears.

### Step 2 — byte-for-byte backup

Create a new timestamped backup directory outside the source directories.

Copy the source files without modifying them. For every file record:

- absolute source path;
- backup filename/path;
- size;
- SHA-256;
- modification time;
- whether it existed.

Write this information to `manifest.json` in the backup directory.

The implemented migration snapshots every existing file recursively under `META_DIR` plus explicitly configured core state/CSV paths. This deliberately preserves files that schema v1 does not yet understand.

### Step 3 — verify backup

Hash the backup copies and compare them against the source hashes. If any mismatch occurs, abort.

A backup that has not been verified is not considered a backup.

Independent verification command:

```bash
python scripts/verify_migration_backup.py /path/to/TIMESTAMP --check-current-sources
```

### Step 4 — create a candidate database

Never import directly into the active database file.

Create a separate file such as:

```text
arcdb.sqlite3.candidate-YYYYMMDD-HHMMSS-PID
```

Import legacy documents into this candidate.

Do not call schema initialization as an in-place v2 -> v3 upgrade. The initializer refuses version changes; rebuild the candidate from authoritative legacy files so empty collection containers can be reconstructed and verified.

### Step 5 — application-data verification

Reconstruct supported legacy documents from SQLite and compare them to the in-memory snapshot used for import.

The comparison must include:

- users;
- user state/progress;
- empty user state containers;
- collections, including empty collection lists through `collection_users`;
- normalized `collection_items` against every membership embedded in `user_data.json`;
- uploads;
- custom metadata;
- allowlist as the unique lowercase set of non-comment email entries; preserve the original file formatting in the verified snapshot.

Unknown legacy fields must survive migration via preserved payloads until they are explicitly modeled.

### Step 6 — database verification

Before promotion, require all of the following:

```sql
PRAGMA quick_check;
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

Expected results:

- `quick_check` -> `ok`;
- `integrity_check` -> `ok`;
- `foreign_key_check` -> zero rows.

Also verify expected schema version and `journal_mode=wal` when opened as the runtime database.

### Step 7 — prove sources were untouched

Recalculate hashes of every legacy source file and compare them with the Step 2 hashes.

The tool also detects added/removed files inside the metadata snapshot scope. If the running application changes state while the candidate is being built, promotion fails closed.

If any source changed unexpectedly, do not promote the candidate. Determine whether the application wrote state during the migration and repeat using a consistent window/protocol.

### Step 8 — candidate promotion

Only after every verification succeeds:

1. close/checkpoint the candidate SQLite connection;
2. refuse promotion if an existing target has WAL/SHM sidecars, because it may be active/uncheckpointed;
3. create a checksum-verified copy of an existing SQLite target;
4. move the previous target to a `pre-migration-original` backup path;
5. atomically rename the verified candidate to the target path where the filesystem permits atomic rename;
6. verify the promoted database again;
7. automatically restore the prior target if post-promotion verification fails and a prior target existed;
8. keep the legacy snapshot and legacy live files.

Promotion is the first point at which the target SQLite path changes. Legacy sources still do not change.

## Runtime cutover protocol

Database file migration and application cutover are separate events.

Repository Phase 2D completes runtime dual-write for all mutable domains currently represented by SQLite schema v3, including full `users.json` payloads. This does not authorize production enablement: the live baseline/paths must still be reconciled, a verified production shadow must exist and `STATE_DUAL_WRITE` remains opt-in. Community/IP-exemption/audit files outside schema v3 are not silently included in this claim.

Phase 3 adds `STATE_READ_BACKEND=legacy|sqlite`, but keeps `legacy` as the default. SQLite mode uses read-only connections and fails closed for missing/stale/invalid state. Phase 3B adds a safer observation step: with `STATE_READ_BACKEND=legacy` and `STATE_READ_SHADOW_COMPARE=1`, the application serves the legacy result, reads SQLite only for comparison and emits payload-free match/mismatch/error events. Non-strict mode is fail-safe for the authoritative read; strict mode is for CI/local validation. Do not set SQLite mode for production-wide traffic merely because seeded CI parity is green: first verify the live shadow, use bounded internal traffic, observe comparison events and prove rollback to `legacy`.

Recommended order:

1. SQLite shadow DB exists and verifies successfully.
2. JSON remains the application read source.
3. Enable dual-write for one bounded set of operations.
4. Compare JSON and SQLite after each write in development/CI.
5. Deploy dual-write with telemetry/logged mismatches.
6. Observe a stable period.
7. Keep `STATE_READ_BACKEND=legacy`, enable shadow comparison for bounded internal traffic and observe every domain.
8. Investigate every mismatch/error; do not continue while any divergence is unexplained.
9. Disable shadow comparison and prove that legacy-only serving is an immediate rollback.
10. Enable `STATE_READ_BACKEND=sqlite` only for a separate bounded internal canary.
11. Compare API responses/state and switch the canary back to `legacy` as a rollback drill.
12. Make SQLite primary read source only after a stable observation period.
13. Keep legacy writes temporarily if rollback confidence requires it.
14. Stop legacy writes after another stable observation period.
15. Preserve legacy files read-only for an agreed retention period.

## Rollback

### Before SQLite becomes read source

Rollback is trivial: set `STATE_READ_BACKEND=legacy`, disable `STATE_READ_SHADOW_COMPARE`, and continue using legacy state. Dual-write may remain enabled if its verified shadow is healthy; disabling it is a separate operational choice.

### After SQLite becomes read source

Preferred rollback options, in order:

1. switch read feature flag back to legacy files if they remained current via dual-write;
2. restore the previous SQLite database from the preserved pre-migration copy/original;
3. export verified SQLite state back to legacy format only through explicit tested export tooling.

Create a reverse export into a **new directory**:

```bash
python scripts/export_sqlite_to_legacy.py \
  --db /path/to/arcdb.sqlite3 \
  --output-dir /path/to/new-rollback-export
```

The exporter refuses to overwrite an existing output directory and reads the generated JSON/TXT files back before reporting success. It does not overwrite live legacy files.

Never manually copy partial table contents in an emergency unless there is no safer option.

## Prohibited migration behaviors

Do not:

- `rm` legacy state during migration;
- truncate legacy JSON after import;
- overwrite the only existing SQLite database before candidate verification;
- assume a zero-row import is valid when source files were expected to contain data;
- run a migration against paths outside the explicitly discovered production data root;
- use unverified shell globbing for state deletion/moves;
- mix schema migration with EPUB/chapter directory cleanup;
- deploy a new baseline over production merely because local CI is green.

## Backup retention

At minimum preserve:

- the pre-cutover legacy snapshot;
- the previous SQLite DB if replacing one;
- the migration manifest/checksums;
- deployment commit/version information.

Longer-term retention should be defined after real production storage size and OCI snapshot policy are known.

## OCI-specific note

OCI Block Volume snapshots/backups can provide an additional infrastructure-level recovery layer, but they do not replace application-level verified backups. Before the first production migration, determine whether Block Volume backup/snapshot is available and create one when practical.

## Definition of migration success

A migration is successful only when:

- source hashes stayed unchanged during migration;
- backup hashes match source hashes;
- candidate SQLite passes application round-trip verification;
- SQLite integrity checks pass;
- candidate promotion succeeds;
- application smoke tests pass;
- rollback artifacts still exist after deployment;
- a reverse-export path has been tested for migrated domains.
