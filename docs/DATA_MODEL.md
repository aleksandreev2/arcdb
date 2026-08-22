# ArchiveDB data model and ownership

## 1. Purpose

This document describes where important data lives today and where it should live after staged migration. Production paths must be inventoried rather than assumed from local development paths.

## 2. Legacy mutable state

The archived baseline uses files such as:

| Legacy source | Purpose | Target owner |
|---|---|---|
| `users.json` | users/auth/verification/reset metadata | SQLite `users` |
| `user_data.json` | per-user per-novel state/progress/download flags | SQLite `user_state_users` + `user_novel_state` |
| `collections.json` | collections metadata, including explicit empty user buckets | SQLite `collection_users` + `collections` |
| collection membership embedded in user state | novel membership in collections | SQLite `collection_items` |
| `user_uploads.json` | uploaded-title metadata/approval | SQLite `user_uploads` |
| `custom_meta.json` | custom novel/file metadata | SQLite `custom_metadata` |
| `allowed_gmails.txt` | allowlist | SQLite `allowed_emails` |

During migration, original payloads are preserved so unknown/unmodeled fields are not silently lost.

## 3. SQLite schema v3

### `schema_meta`

Stores schema metadata such as schema version.

### `legacy_documents`

Stores the JSON representation imported during the initial migration for audit/round-trip validation. It is a transition/audit aid, not the runtime source of mutable state.

### `users`

Primary key: email (case-insensitive).

Normalized columns currently include:

- password hash;
- verified flag;
- creation timestamp;
- verification code metadata;
- password reset metadata;
- original payload JSON.

Phase 2D runtime dual-write mirrors create/update/delete changes after `users.json` is durable. Registration, verification attempts/success, password-reset state and local dev-account maintenance retain the complete payload, including unknown fields. Password/token formats are unchanged.

### `user_state_users`

Primary key: user email (case-insensitive).

Purpose: represent the existence of a top-level `user_data.json` bucket even when the user has zero per-novel records.

Example legacy state:

```json
{
  "user@example.com": {}
}
```

Without this table, deleting a user's final `user_novel_state` row would make that empty top-level bucket impossible to distinguish from a user absent from `user_data.json`.

### `user_novel_state`

Primary key: `(user_email, novel_key)`.

Normalized fields:

- status;
- progress;
- last read time;
- download count;
- last download time;
- hidden flag;
- complete original/current payload JSON.

Phase 2A runtime dual-write mirrors changed rows in this table after the legacy JSON write succeeds.

### `collections`

Primary key: `(user_email, collection_id)`.

Fields include:

- name;
- sort order;
- original payload.

Phase 2B runtime dual-write replaces the affected user's ordered collection rows after the authoritative `collections.json` write succeeds. Complete payload JSON is preserved.

### `collection_users`

Primary key: user email (case-insensitive).

Purpose: represent a top-level `collections.json` bucket even when it contains no collections.

Example:

```json
{
  "user@example.com": []
}
```

This table was added in schema v3. Export/parity no longer depends on the stale initial `legacy_documents` snapshot to reproduce an empty live container.

### `collection_items`

Primary key: `(user_email, collection_id, novel_key)`.

Represents membership separately from collection metadata.

Phase 2A mirrors memberships embedded in a per-novel record whenever that record changes. Phase 2B additionally covers dedicated assign/unassign/delete cleanup and community import routes. Full parity independently compares the complete normalized `collection_items` relation against memberships embedded in `user_data.json`.

### `user_uploads`

Primary key: upload id.

Normalized fields include uploader email, approval flag/date/title plus original payload.

Phase 2C runtime dual-write covers create, approval/update and rejection/delete mutations. Every row retains the complete payload JSON, including unknown fields.

### `custom_metadata`

Primary key: filename.

Stores original custom metadata payload. Phase 2C runtime dual-write upserts the affected entry after `custom_meta.json` is durable.

### `allowed_emails`

Primary key: normalized email. Phase 2C replaces the normalized table after each successful allowlist file mutation. Comments, blank lines, case variants and duplicates are file-format details; the semantic allowlist is the unique lowercase email set. The original file remains preserved by migration backups.

## 4. Runtime ownership during Phase 3B

For `users.json`, `user_data.json`, `collections.json`, `user_uploads.json`, `custom_meta.json` and allowlist state represented by schema v3:

```text
legacy JSON
  - authoritative read source
  - primary write happens first

SQLite
  - shadow write of changed auth/user-state/upload/custom-metadata rows, affected collection containers and normalized allowlist
  - immediate per-row verification in local/CI when enabled
  - full-document plus normalized membership semantic parity check available
```

This is intentionally asymmetric. SQLite must not become authoritative until read-cutover tests are complete.

Phase 3 can export the live normalized/payload tables through a read-only SQLite connection when `STATE_READ_BACKEND=sqlite`. The default is `legacy`. Phase 3B can also compare those exports with real legacy-served reads when `STATE_READ_SHADOW_COMPARE=1`; equality is checked in memory and logs contain only domain/event/counter/error-type metadata, never state payloads. Write helpers bypass the read adapter and load legacy state directly so the required durable legacy-first sequence cannot be inverted by the read flag.

The production readiness preflight loads each legacy document once, compares it with all schema-v3 exports from one read-only SQLite transaction and verifies the recursively discovered legacy file set/hashes did not change during the check. Its report contains aggregate source/file counts, database checks and row counts only; it contains no paths, keys, identities or payload values and does not authorize a canary.

Relevant feature flags:

```text
STATE_DUAL_WRITE
STATE_DUAL_WRITE_STRICT
STATE_DUAL_WRITE_VERIFY
STATE_DUAL_WRITE_LOG_SUCCESS
STATE_READ_BACKEND
STATE_READ_SHADOW_COMPARE
STATE_READ_SHADOW_STRICT
STATE_READ_SHADOW_REPORT_EVERY
```

Local development enables strict/verified mirroring by default. Production remains opt-in.

## 5. Data intentionally NOT in SQLite state v2

These are separate concerns and must not be migrated accidentally as part of user-state cutover:

- original EPUB binaries;
- translated/release EPUB binaries;
- unpacked chapter HTML;
- chapter images/assets;
- covers;
- temp packaging sessions;
- Telegram session files;
- process-local caches;
- rate-limit buckets;
- download event logs if represented separately;
- library discovery/index data;
- Cloudflare configuration.

## 6. Local development tree

Typical ignored tree:

```text
data/
├── arcdb.sqlite3
├── migration-backups/
├── sqlite-backups/
├── shadow-sidecar-backups/
├── metadata/
│   ├── users.json
│   ├── user_data.json
│   ├── collections.json
│   ├── user_uploads.json
│   ├── custom_meta.json
│   └── allowed_gmails.txt
├── structured_output/
├── batched_epubs/
├── output/
├── telegram/
└── tmp/
```

`shadow-sidecar-backups/` is local-development recovery plumbing used only when a stale shadow DB must be checkpointed/rebuilt after reseeding. It is not the production migration backup format.

## 7. Novel/library identity

The current code can refer to a title using several values, including numeric/raw/source ids, filenames and library keys. Repeated lookup by scanning lists is a known problem.

Future library indexing should define one stable internal `novel_id` and explicit alternate keys:

- source id / NovelPia id where available;
- raw filename;
- translated filename;
- external/source reference;
- legacy library key.

Do not silently change identifier semantics during SQLite user-state migration because existing `user_data.json` keys must keep mapping to the same titles.

## 8. Future library index

Expected later tables/entities may include:

### `novels`

- stable internal id;
- title/original title;
- source;
- source id;
- language;
- metadata/tags;
- chapter count;
- cover key/path;
- current version/hash;
- timestamps.

### `novel_files`

- novel id;
- file/object type (`raw_epub`, `translation_epub`, `cover`, etc.);
- local path or object key;
- size/hash;
- version/current flag.

### `chapters`

- novel id;
- chapter id/number/title;
- relative path/object key;
- content hash;
- optional word count/update time.

These are future plans, not part of state schema v3.

## 9. Storage semantics

### Mutable relational state

Use SQLite WAL while there is one primary OCI VM.

### POSIX working data

Use OCI Block Volume for SQLite, unpacked reader content and processing work that needs filesystem semantics.

### Immutable large objects

Consider R2 later for release EPUBs/covers/images when it reduces origin I/O/egress and operational complexity.

## 10. Backup expectations by class

### User state

Must have application-level backup + checksum manifest before production migration/cutover.

### SQLite

Use candidate-first migration promotion and preserve the previous SQLite copy. For
recurring operational backups, use `scripts/create_sqlite_backup.py`: the SQLite
online backup API captures a consistent committed snapshot including WAL content,
then produces a portable single-file artifact with SHA-256, integrity/FK checks and
a temporary runtime restore test. `scripts/restore_sqlite_backup.py` restores only to
a new path and never overwrites an active database. See `docs/BACKUP_RESTORE.md`.

Before a migration promotion or an application cutover to a restored path,
stop/quiesce users and preserve the current database plus its sidecars. Active
WAL/SHM sidecars remain a fail-closed condition for replacing a migration target.

### EPUB/chapter data

Do not delete or reorganize during user-state migration. Treat content-storage migration as its own project with file inventories/hashes.

### Telegram session

Secret operational state. Never commit. Back up/restore only through secure operational procedures.

## 11. Data-model rule

When adding a normalized column for a legacy field, do not remove the raw/payload fallback until production evidence shows all meaningful legacy fields are explicitly modeled and migration/export tests cover them.
