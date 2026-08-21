# ArchiveDB implementation roadmap

This roadmap is ordered to reduce risk. Do not jump ahead simply because a later item is more visible.

## Phase 0 — reproducible development

Status: completed.

- Windows one-click bootstrap.
- dependency auto-install/update.
- safe local `.env`.
- local dev account.
- local EPUB fixtures/seed.
- CI smoke tests.

## Phase 1 — migration safety + SQLite shadow foundation

Status: completed for the current state scope.

Implemented:

- SQLite WAL schema;
- JSON -> SQLite importer;
- round-trip tests;
- candidate-first safe migration;
- immutable/checksummed legacy snapshots;
- integrity/foreign-key checks;
- previous-SQLite preservation and rollback;
- SQLite -> legacy reverse export;
- project documentation/handoff rules.

Production execution is still pending because real OCI paths/live source must be reconciled first.

## Phase 2 — runtime state dual-write

Status: completed for every mutable domain represented by SQLite schema v3.

### 2A — per-novel user state

Status: implemented and covered by dedicated runtime CI.

Current mirrored mutations:

- reading progress;
- reading status;
- `last_read`;
- hidden flag;
- download count/last-download fields;
- bulk user-state removals;
- embedded collection membership changes on an affected record.

Current ownership:

```text
JSON = read source + primary write
SQLite = verified shadow write
```

Local development uses strict verification. Full `user_data.json` parity is checked before startup and after end-to-end runtime API tests.

### 2B — collections

Status: implemented and covered by dedicated runtime CI.

- create and rename collection metadata;
- delete collection metadata and clean every embedded membership;
- add/remove membership through collection and bulk routes;
- community collection import;
- repeated/idempotent operations;
- preserve explicit empty `collections.json` user buckets with schema v3 `collection_users`;
- compare `collections.json`, embedded payload memberships and normalized `collection_items` against SQLite.

### 2C — uploads/custom metadata/allowlist

Status: implemented and covered by dedicated runtime CI.

- upload create, approval/update and rejection/delete transitions;
- custom metadata upserts;
- allowlist add/deduplicate/revoke with runtime-equivalent normalization;
- admin mutation tests plus real upload processing;
- full legacy/SQLite parity for all three domains.

### 2D — users/auth

Status: implemented and covered by unit plus real Flask auth workflow CI.

- registration and unverified re-registration;
- verification attempts/success and verification token cleanup;
- password-reset request, attempts, password hash replacement and reset token cleanup;
- complete payload preservation, including unknown fields;
- create/update/idempotent/delete storage-helper coverage;
- missing/stale/disabled shadow and strict mismatch coverage;
- existing dev account plus repeated idempotent bootstrap;
- real register -> verify -> login -> reset -> login workflow.

Phase 2 exit criteria:

- every supported mutable-state write produces equivalent JSON/SQLite state;
- CI covers repeated/idempotent and important route-level writes;
- no user-visible API changes;
- mismatch handling is explicit and observable.

## Phase 3 — SQLite read cutover

Status: comparison backend, seeded API parity, legacy-serving shadow observability and canary rollback rehearsal implemented; production rollout/primary-read promotion pending.

Implemented:

- `STATE_READ_BACKEND=legacy|sqlite`, defaulting to legacy;
- read-only, schema-checked SQLite reads for every schema-v3 state domain;
- fail-closed invalid/missing/stale behavior;
- mutation helpers that continue to read/write legacy first regardless of read backend;
- simultaneous Flask API parity for login, library, collections, novel, trending, community, reader and admin output;
- real mutation regressions while SQLite reads are enabled;
- opt-in runtime shadow comparison that always returns legacy in non-strict observation mode;
- process-local counters plus payload-free match/mismatch/error events for every schema-v3 read domain;
- strict CI coverage for mismatch, missing SQLite, secret-safe logs and all-domain real Flask reads;
- explicit-path, read-only readiness preflight that checks one consistent SQLite snapshot, full parity, schema/integrity/FK health and recursive legacy source-hash stability;
- overwrite-refusing sanitized readiness reports that explicitly leave canary/primary authorization false;
- fail-closed audit of one bounded process's payload-free shadow events with complete six-domain coverage;
- real Flask CI rehearsal that replaces the SQLite-read canary process with legacy on the same port and repeats authenticated API parity.

Pending:

1. obtain a live inventory/sanitized baseline and run the readiness preflight on explicit production paths;
2. reconcile unknown files and live code/config differences;
3. enable legacy-serving shadow comparison for one bounded internal process and validate its events;
4. enable SQLite reads only for a separate bounded internal canary;
5. promote SQLite as primary read source only after stable observation;
6. keep legacy files and rollback controls.

Exit criteria:

- stable observation period;
- no unexplained state divergence;
- rollback tested.

## Phase 4 — stop legacy whole-file writes

After SQLite reads are stable:

- stop JSON writes per domain, one domain at a time;
- retain explicit SQLite -> legacy export tooling;
- archive legacy files read-only;
- do not delete them as part of this phase.

Benefits:

- remove read-modify-write whole JSON cycles;
- reduce fsync amplification;
- simplify locking;
- prepare for multiple web workers.

## Phase 5 — immediate I/O fixes

Can partly run in parallel once storage work is stable.

- upload sequential chunks, one flush/fsync at end rather than each 1 MB chunk;
- stream ZIP/EPUB entries instead of loading entire archive into RAM;
- enforce total upload/session limits consistently;
- add robust MIME/file validation where appropriate.

## Phase 6 — async packaging jobs

- create persistent jobs representation;
- POST returns `202 + job_id`;
- packager worker processes queued jobs;
- progress/status endpoint;
- output stored safely;
- retries/cancellation/expiry cleanup;
- move heavyweight finalization out of HTTP request lifecycle.

## Phase 7 — split Telethon

- remove Telethon startup from Flask module import;
- create dedicated Telegram service;
- define shared persistence/event boundary;
- ensure web restarts do not restart Telegram client;
- only then reconsider web worker count.

## Phase 8 — persistent library/chapter index

- define stable novel id and alternate keys;
- scan/import outside request path;
- persist chapter counts/paths/metadata/hashes;
- replace linear `find_novel` and repeated `os.walk` calls;
- use indexed query/search;
- consider SQLite FTS5 for title/search.

## Phase 9 — frontend/static split

- move inline CSS/JS to static assets;
- vendor runtime dependencies such as JSZip;
- add cache headers/fingerprints;
- reduce HTML size;
- tighten CSP after inline code is removed.

## Phase 10 — Cloudflare/R2 optimization

Only after object ownership is clear.

Potential R2 candidates:

- immutable release EPUBs;
- covers;
- public/notice images;
- large uploads/prebuilt exports.

Worker responsibilities should remain light:

- routing;
- auth/security helpers where appropriate;
- cache/static;
- object access control;
- proxying API to Tunnel.

Do not move EPUB processing/Telegram/SQLite into Workers.

## Phase 11 — security hardening

Some items can occur earlier when touching related code:

- systematic CSRF/same-origin protection;
- allowlist HTML sanitizer for EPUB content;
- upload ownership/session limits;
- tighter CSP;
- origin network restrictions;
- admin auditability;
- secret rotation/documented secret ownership.

## Phase 12 — performance tuning based on measurements

After process-local state is removed/split:

- benchmark Gunicorn process/thread combinations;
- inspect Block Volume I/O;
- adjust VPU tier only if measured I/O remains a bottleneck;
- add request/job timing metrics;
- tune caching based on hit rates, not guesses.

## Current immediate order

```text
1. live inventory + explicit-path readiness preflight + reconciliation
2. bounded legacy-serving shadow observation
3. bounded SQLite read canary
4. SQLite primary reads
5. stop legacy writes domain-by-domain
6. immediate upload/EPUB I/O fixes
7. async packager
8. Telethon split
9. persistent library index/frontend split
10. R2/Cloudflare optimization
```

## Explicit non-goals for now

- Kubernetes.
- microservice rewrite.
- immediate PostgreSQL migration.
- full Cloudflare Workers rewrite.
- deleting legacy user data during SQLite migration.
- reorganizing all EPUB/chapter storage during user-state cutover.
