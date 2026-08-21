# ArchiveDB data model and ownership

## 1. Purpose

This document describes where important data lives today and where it should live after staged migration. It is not a promise that production paths exactly match local paths; production paths must be inventoried first.

## 2. Legacy mutable state

The archived baseline uses files such as:

| Legacy source | Purpose | Target owner |
|---|---|---|
| `users.json` | users/auth/verification/reset metadata | SQLite `users` |
| `user_data.json` | per-user per-novel state/progress/download flags | SQLite `user_novel_state` |
| `collections.json` | collections metadata | SQLite `collections` |
| collection membership embedded in user state | novel membership in collections | SQLite `collection_items` |
| `user_uploads.json` | uploaded-title metadata/approval | SQLite `user_uploads` |
| `custom_meta.json` | custom novel/file metadata | SQLite `custom_metadata` |
| `allowed_gmails.txt` | allowlist | SQLite `allowed_emails` |

During migration, original payloads are preserved so unknown/unmodeled fields are not silently lost.

## 3. SQLite schema v1

### `schema_meta`

Stores schema metadata such as schema version.

### `legacy_documents`

Stores the JSON representation imported during migration for audit/round-trip validation. This is a transition aid, not the long-term application API.

### `users`

Primary key: email (case-insensitive).

Important normalized columns currently include:

- password hash;
- verified flag;
- creation timestamp;
- verification code metadata;
- password reset metadata;
- original payload JSON.

### `user_novel_state`

Primary key: `(user_email, novel_key)`.

Normalized fields:

- status;
- progress;
- last read time;
- download count;
- last download time;
- hidden flag;
- original payload JSON.

### `collections`

Primary key: `(user_email, collection_id)`.

Fields include:

- name;
- sort order;
- original payload.

### `collection_items`

Primary key: `(user_email, collection_id, novel_key)`.

Represents membership separately from the collection metadata.

### `user_uploads`

Primary key: upload id.

Normalized fields include uploader email, approval flag/date/title plus original payload.

### `custom_metadata`

Primary key: filename.

Stores original custom metadata payload.

### `allowed_emails`

Primary key: normalized email.

## 4. Data that is intentionally NOT in SQLite state v1

These are separate concerns and should not be migrated accidentally as part of user-state cutover:

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

## 5. Local development tree

Typical local ignored tree:

```text
data/
├── arcdb.sqlite3
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

This layout exists for reproducible development. Production paths may differ.

## 6. Novel/library identity

The current code can refer to a title using several values, including numeric/raw/source ids, filenames and library keys. Repeated lookup by scanning lists is a known problem.

Future library indexing should define one stable internal `novel_id` and explicit alternate keys:

- source id / NovelPia id where available;
- raw filename;
- translated filename;
- external/source reference;
- legacy library key.

Do not silently change identifier semantics during the first SQLite user-state migration because `user_data.json` keys must continue to map to the same titles.

## 7. Future library index

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

These are future plans, not part of schema v1.

## 8. Storage semantics

### Mutable relational state

Use SQLite WAL while there is one primary OCI VM.

### POSIX working data

Use OCI Block Volume for SQLite, unpacked reader content and processing work that needs filesystem semantics.

### Immutable large objects

Consider R2 later for files such as release EPUBs/covers/images when it reduces origin I/O/egress and operational complexity.

## 9. Backup expectations by class

### User state

Must have application-level backup + checksum manifest before migration/cutover.

### SQLite

Use candidate-first promotion and preserve previous SQLite copy. Later add regular consistent SQLite backups/checkpoints.

### EPUB/chapter data

Do not delete or reorganize during user-state migration. Treat content-storage migration as its own project with file inventories/hashes.

### Telegram session

Secret operational state. Never commit. Back up/restore only through secure operational procedures.

## 10. Data-model rule

When adding a normalized column for a legacy field, do not remove the raw/payload fallback until production evidence shows all meaningful legacy fields are explicitly modeled and migration/export tests cover them.