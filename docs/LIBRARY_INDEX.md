# Persistent library index

## Status and scope

The repository/local/CI runtime uses a separate SQLite library index for library,
novel lookup, tags/authors and reader chapter/image discovery. This does not change
schema-v3 user-state ownership and is not evidence that any production host has
been migrated.

`library_index.sqlite3` is derived, rebuildable data. It may contain private local
paths inside item payloads, so the database and command output remain private even
though verification output contains only counts and an opaque generation id.

## Runtime invariant

Normal request paths never build the library by scanning storage:

```text
explicit reindex
  -> scan metadata/EPUB/chapter sources
  -> build sibling SQLite candidate
  -> quick_check + integrity_check + foreign_key_check + count check
  -> fsync candidate
  -> atomic replace

request
  -> read-only SQLite query/lookup
  -> response
```

A missing or incompatible index fails closed with HTTP 503. The runtime does not
silently fall back to `os.walk`, because that would reintroduce unpredictable
request latency and conceal an incomplete rollout.

## Indexed model

Schema version 1 stores:

- one stable internal id plus explicit aliases for source id, filename, library key
  and legacy ids;
- normalized title, original title, author, language, status, source, chapter count,
  views/likes and update ordering fields;
- tags and alternate search text;
- ordered reader chapter paths/titles and image paths;
- a content fingerprint and the compatibility payload required by existing APIs;
- optional FTS5 trigram search, with a bounded SQL substring fallback when the
  available SQLite build lacks that tokenizer.

Duplicate stable identities or aliases reject the candidate; the active index is
left untouched rather than silently assigning an identifier to the wrong title.

The index supports server-side search, tag AND/OR/exclusion, source, translated/raw,
audience, status, language, author, chapter range, collection/reading-state,
updated-time, sorting and page/limit queries. Page size is capped at 100.

## Build and verify

Set an explicit private path on persistent local storage:

```text
LIBRARY_INDEX_DB_PATH=/path/on/block-volume/library_index.sqlite3
```

Prefer a sibling of the legacy metadata directory so derived writes are naturally
outside immutable legacy-source discovery.

Build and atomically publish:

```bash
PYTHONPATH=. python scripts/reindex_library.py
```

Verify without scanning or changing storage:

```bash
PYTHONPATH=. python scripts/reindex_library.py --verify-only
```

Local bootstrap rebuilds after optional fixture seeding. Non-local bootstrap only
verifies the active index; it never performs an implicit production filesystem scan.

Metadata edits and upload approval/rejection update affected index rows
transactionally. A failed incremental update reports an operator-visible error and
requires the controlled reindex; source/user files are not rolled back or deleted.

## Production rollout and rollback

Production enablement remains inventory-gated:

1. discover the exact metadata/content paths and choose an explicit index path;
2. stop or otherwise serialize ingest/admin mutations during the first build;
3. run the explicit rebuild and verify command;
4. start the candidate web revision and exercise library, novel and reader flows;
5. keep the prior application revision available for rollback.

Rollback is application-only: stop the new revision and start the previous revision
against the unchanged metadata/content and schema-v3 state. The index is not a user
state backup and must not be substituted for the WAL-aware state backup procedure.
If the index is lost, rebuild it from preserved source storage.

When a derived index or package-jobs database lives below the legacy metadata root,
pass each exact path to readiness as `--exclude-derived-db`. Migration resolves the
configured `LIBRARY_INDEX_DB_PATH` and `PACKAGE_JOBS_DB_PATH` and excludes those
databases plus WAL/SHM sidecars from the immutable legacy snapshot. Unknown files,
including unknown SQLite files, are still preserved and reported.

## Tests and benchmark

Unit and runtime workflows cover candidate failure, integrity, aliases, chapter
lookup, search/filter/sort/pagination, incremental update/delete and missing-index
failure. `scripts/benchmark_library_index.py` emits only synthetic counts and
p50/p95/p99 latency; see `docs/PERFORMANCE_BASELINE.md`.
