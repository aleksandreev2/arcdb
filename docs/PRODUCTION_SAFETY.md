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

### Step 3 — verify backup

Hash the backup copies and compare them against the source hashes. If any mismatch occurs, abort.

A backup that has not been verified is not considered a backup.

### Step 4 — create a candidate database

Never import directly into the active database file.

Create a separate file such as:

```text
arcdb.sqlite3.candidate-YYYYMMDD-HHMMSS
```

Import legacy documents into this candidate.

### Step 5 — application-data verification

Reconstruct supported legacy documents from SQLite and compare them to the in-memory snapshot used for import.

The comparison must include:

- users;
- user state/progress;
- empty user state containers;
- collections, including empty collection lists;
- uploads;
- custom metadata;
- allowlist.

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

If any source changed unexpectedly, do not promote the candidate. Determine whether the application wrote state during the migration and repeat using a consistent window/protocol.

### Step 8 — candidate promotion

Only after every verification succeeds:

1. close/checkpoint the candidate SQLite connection;
2. if an old SQLite target exists, rename it to a timestamped `pre-migration` backup;
3. atomically rename the verified candidate to the target path where the filesystem permits atomic rename;
4. never delete the old SQLite backup during this operation;
5. keep the legacy snapshot and legacy live files.

Promotion is the first point at which the target SQLite path changes. Legacy sources still do not change.

## Runtime cutover protocol

Database file migration and application cutover are separate events.

Recommended order:

1. SQLite shadow DB exists and verifies successfully.
2. JSON remains the application read source.
3. Enable dual-write for one bounded set of operations.
4. Compare JSON and SQLite after each write in development/CI.
5. Deploy dual-write with telemetry/logged mismatches.
6. Observe a stable period.
7. Enable SQLite reads behind a feature flag for internal/testing traffic.
8. Compare API responses/state.
9. Make SQLite primary read source.
10. Keep legacy writes temporarily if rollback confidence requires it.
11. Stop legacy writes after another stable observation period.
12. Preserve legacy files read-only for an agreed retention period.

## Rollback

### Before SQLite becomes read source

Rollback is trivial: disable dual-write/SQLite feature flags and continue using legacy state.

### After SQLite becomes read source

Preferred rollback options, in order:

1. switch read feature flag back to legacy files if they remained current via dual-write;
2. restore the previous SQLite database from the preserved pre-migration copy;
3. export verified SQLite state back to legacy format only through explicit tested export tooling.

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
- rollback artifacts still exist after deployment.