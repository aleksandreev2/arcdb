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

Status: in progress.

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

Do last within write migration because auth has higher correctness/security risk.

- registration/verification;
- password hashes;
- reset-code state;
- account/access changes;
- dedicated login/reset/admin tests.

Phase 2 exit criteria:

- every supported mutable-state write produces equivalent JSON/SQLite state;
- CI covers repeated/idempotent and important route-level writes;
- no user-visible API changes;
- mismatch handling is explicit and observable.

## Phase 3 — SQLite read cutover

Do not start until Phase 2 write coverage is complete enough for the target domain.

1. add feature flag for state read source;
2. run API parity tests using identical seeded state;
3. test login, library, reader, progress, collections, uploads/admin flows;
4. enable SQLite reads in local dev;
5. compare responses against legacy reads;
6. deploy only after production shadow data is verified;
7. observe mismatch/error metrics;
8. promote SQLite as primary read source;
9. keep legacy files and rollback controls.

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
1. users/auth dual-write
2. SQLite read feature flag + API parity
3. SQLite primary reads
4. stop legacy writes domain-by-domain
5. immediate upload/EPUB I/O fixes
6. async packager
7. Telethon split
8. persistent library index
9. frontend/static split
10. R2/Cloudflare optimization
11. production rollout after live reconciliation
```

## Explicit non-goals for now

- Kubernetes.
- microservice rewrite.
- immediate PostgreSQL migration.
- full Cloudflare Workers rewrite.
- deleting legacy user data during SQLite migration.
- reorganizing all EPUB/chapter storage during user-state cutover.
