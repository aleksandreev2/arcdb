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

## Phase 0B — tracked runtime source

Status: completed.

- behavior-compatible materialized Flask source is tracked as `arcdb/app.py`;
- all eight runtime templates are tracked under `arcdb/templates/`;
- local bootstrap and runtime CI launch tracked source directly;
- runtime behavior no longer depends on `.b64` extraction or text overlays;
- the compressed baseline/materializer/overlay remain historical provenance and
  reconciliation tools only.

The monolith has intentionally not been split or redesigned in this phase. Direct
source ownership is the prerequisite for the later I/O, security, jobs and process
separation changes.

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
- WAL-aware online SQLite backup, independent verification and new-target-only restore;
- project documentation/handoff rules.

The repository-side migration and backup/restore toolchain is complete for the current
state scope. Actual production execution remains separate and requires explicit real
paths and operator-controlled timing.

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
- read-only host discovery, explicit-path structured production inventory, private exact source diff and separate path-free reporting; local/CI fixtures do not count as live inventory.

Pending:

1. execute `docs/PRODUCTION_INVENTORY.md` with production access and collect the private live inventory;
2. reconcile every private source difference and unknown metadata file, then run the readiness preflight on explicit production paths;
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

Status: completed in repository/local/CI runtime.

- upload sequential chunks with one flush/fsync before atomic publication;
- stream ZIP/EPUB entries instead of loading the complete archive into RAM;
- enforce request, entry, expanded archive, compression-ratio, package image,
  package-session byte/file/count and expiry limits;
- reject traversal, absolute/reserved paths, symlinks/special files, encryption,
  duplicate/case/Unicode collisions, malformed structure/XML and CRC corruption;
- validate uploaded package images by signature and bind package sessions to the
  authenticated creator;
- atomically publish extracted directories and final EPUBs, cleaning partial files;
- retain init/upload/download routes; change finalize deliberately to HTTP 202 plus
  a persistent job id in Phase 6.

The bounded implementation is reused by Phase 6's separate worker; the Flask
finalize route no longer invokes it directly.

## Phase 6 — async packaging jobs

Repository/local/CI implementation completed:

- dedicated SQLite WAL queue, separate from state schema v3;
- finalize and `POST /api/jobs/package` return `202 + job_id`;
- authenticated status/cancel endpoints and frontend polling;
- separate `scripts/run_packager.py` process with atomic claim and publication;
- persistent attempts/progress/heartbeat/timeout/cancellation;
- bounded retries, stale-worker restart recovery and expiry cleanup;
- local bootstrap process separation and a production systemd template;
- real web -> queue -> worker -> status -> download CI coverage.

Production enablement remains inventory/operator-gated; repository tests do not
prove the unit or its Block Volume paths are live.

## Phase 7 — split Telethon

Repository/local/CI implementation completed:

- removed Telethon imports, startup, asyncio loop and session credentials from web;
- added one dedicated Telegram service with an authenticated loopback streaming
  boundary, health/readiness and sanitized failures;
- restricted the web client to loopback, disabled redirects and preserved streaming;
- added local opt-in lifecycle management, a one-worker systemd template and
  separate environment ownership;
- covered service auth/readiness/streaming, web failure mapping and duplicate-start
  isolation with tests.

Production enablement remains inventory/operator-gated. Other process-local web
state still prevents increasing Gunicorn workers without measurement and review.

## Phase 8 — persistent library/chapter index

Repository/local/CI implementation is complete; production enablement is not
claimed:

- stable internal ids plus filename/library/source aliases;
- explicit candidate build outside request paths with integrity/FK/count checks and
  atomic publication;
- persistent metadata/tags/hashes and ordered chapter/title/image discovery;
- indexed `find_novel`, reader discovery and server-side query/filter/sort/page;
- optional SQLite FTS5 trigram search with compatible fallback;
- incremental custom-metadata and upload approval/rejection maintenance;
- fail-closed runtime, tests, benchmark and rollout/rollback runbook.

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

Status: systematic state-changing request origin protection, POST-only logout and
parser-based EPUB HTML sanitization are implemented in repository/local/CI. The
remaining production/inventory-gated controls are pending.

Some items can occur earlier when touching related code:

- systematic CSRF/same-origin protection (implemented; exact production origin pending inventory);
- parser + explicit tag/attribute/URL allowlist for EPUB content (implemented);
- systematic ownership review for non-package upload/session endpoints (EPUB package ownership/limits are complete in Phase 5);
- tighter CSP;
- origin network restrictions;
- admin auditability;
- secret rotation/documented secret ownership.

## Phase 12 — performance tuning based on measurements

Status: baseline request timing instrumentation is implemented in repository/local
CI; production measurements and tuning remain pending.

After process-local state is removed/split:

- benchmark Gunicorn process/thread combinations;
- inspect Block Volume I/O;
- adjust VPU tier only if measured I/O remains a bottleneck;
- extend request timing with bounded subsystem/job timings where measurements justify it;
- tune caching based on hit rates, not guesses.

## Current immediate order

```text
1. live inventory + explicit-path readiness preflight + reconciliation
2. bounded legacy-serving shadow observation
3. bounded SQLite read canary
4. SQLite primary reads
5. stop legacy writes domain-by-domain
6. production-enable the implemented Telethon split after inventory
7. production-enable the implemented persistent library index after inventory
8. security/observability and measured performance work (next repository-side stage)
9. R2/Cloudflare optimization
```

Without live-production inputs, Phase 11 security/observability is the next
independent repository-side stage; state read-cutover, library index, packager and
Telegram production enablement remain operator-gated rather than guessed.

## Explicit non-goals for now

- Kubernetes.
- microservice rewrite.
- immediate PostgreSQL migration.
- full Cloudflare Workers rewrite.
- deleting legacy user data during SQLite migration.
- reorganizing all EPUB/chapter storage during user-state cutover.
