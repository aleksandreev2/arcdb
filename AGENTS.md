# ArchiveDB project instructions

This file is the primary handoff document for AI assistants and new contributors. Read it before changing code.

## Project goal

ArchiveDB is a long-form web library/reader for novels and EPUB workflows. The immediate objective is to preserve the current UX/API while replacing fragile local JSON whole-file persistence and expensive request-path filesystem work with safer indexed storage and background processing.

## Known production architecture

Current confirmed production facts:

- Python + Flask monolith.
- OCI Ampere A1 ARM VM, 4 OCPU / 24 GB RAM.
- Persistent OCI Block Volume.
- Cloudflare in front of the origin; current public deployment uses `workers.dev`/Cloudflare-aware origin behavior and likely Cloudflare Tunnel.
- Local filesystem contains EPUBs, unpacked chapters/assets, metadata, caches and mutable state.
- Telegram integration uses Telethon.
- Current archived source contains important process-local caches, locks, rate buckets and a process-local Telegram client.

Do not assume unknown OCI details. See `docs/ARCHITECTURE.md` for confirmed vs unconfirmed facts.

## Safety rules

1. Never delete or overwrite production data as part of a migration.
2. Legacy JSON/CSV/TXT state stays intact until SQLite has been used successfully in production for a defined observation period and an explicit cleanup decision is made.
3. Every destructive-looking migration must be copy-first, verify-first and rollback-capable.
4. Never commit secrets, `.env`, SSH keys, Telegram session files, user databases, EPUB payloads, extracted chapters or production logs.
5. Do not blindly deploy the archived baseline over the OCI instance. Reconcile against a sanitized live snapshot first.
6. Do not increase Gunicorn process count while stateful process-local components remain in the web process.
7. Keep heavy EPUB/ZIP/image/Telegram work off Cloudflare Workers. OCI remains the heavy compute origin.
8. Prefer simple components: modular Flask app + SQLite WAL + separate background workers + Cloudflare edge/R2 when introduced.
9. Preserve API and UI behavior during storage migration. Migrate behind adapters and tests.
10. If production state differs from assumptions, stop and document the discrepancy instead of guessing.
11. JSON is still the read source during Phase 2. Do not switch reads to SQLite in the same change as a new dual-write domain.
12. Production dual-write is opt-in. Never enable it before a verified initial SQLite shadow migration on the real production data.

## Current migration status

Completed:

- reproducible Windows local bootstrap;
- automatic local EPUB fixture seeding when the library is empty;
- SQLite WAL shadow schema and safe JSON -> SQLite import;
- candidate-first migration, checksummed backups, rollback/reverse-export tooling;
- runtime dual-write Phase 2A for per-novel user state is implemented in the current codebase:
  - `/api/user_progress`;
  - `/api/user_status`;
  - `/api/user_hide`;
  - `/api/bulk_remove` user-state mutations;
  - local/Telegram download counters;
  - changed embedded collection memberships are mirrored with the affected record;
- full `user_data.json` <-> SQLite parity verifier;
- runtime dual-write Phase 2B for collection metadata and memberships:
  - create, rename and delete;
  - assign/unassign and repeated idempotent operations;
  - membership cleanup on delete;
  - community collection import;
  - explicit empty `collections.json` user containers through schema v3 `collection_users`;
- full `collections.json` and normalized `collection_items` parity verification;
- runtime dual-write Phase 2C for:
  - upload create, approval/update and rejection/delete state;
  - custom metadata upserts;
  - allowlist add/revoke with normalized set semantics;
- full `user_uploads.json`, `custom_meta.json` and semantic allowlist parity verification;
- runtime dual-write Phase 2D for users/auth:
  - registration and re-registration of unverified accounts;
  - email verification attempts/success and verification-field cleanup;
  - password-reset request, attempts, password-hash replacement and reset-field cleanup;
  - local dev-account creation/update without rehashing an already valid password;
  - complete unknown-field-preserving user payloads and delete support in the storage helper;
- full `users.json` <-> SQLite parity verification, including an empty document;
- strict local dual-write verification and end-to-end CI;
- Phase 3 feature-flagged read adapters plus simultaneous legacy/SQLite API parity;
- Phase 3B legacy-serving SQLite shadow comparison with process-local counters, payload-free events and strict all-domain CI;
- read-only cutover readiness preflight with full parity, SQLite health checks, recursive source-hash stability and a payload-free report that cannot authorize cutover by itself;
- Phase 3C shadow-observation evidence validation plus a real Flask SQLite-canary -> legacy rollback rehearsal in CI.
- read-only production discovery, structured private inventory, path-free reporting and deterministic source reconciliation tooling; live execution is still pending.
- WAL-aware SQLite online backup, sanitized checksum manifest, independent runtime restore verification and new-target-only restore tooling.
- behavior-compatible Flask runtime and templates tracked directly under `arcdb/`; local startup and runtime CI no longer materialize `.b64` baseline parts or apply overlays.
- bounded upload/EPUB I/O: one upload fsync before atomic publish, structure/CRC/path/bomb validation, atomic extraction, owner/size/count-limited package sessions and entry-streamed finalization.
- persistent async EPUB jobs: dedicated SQLite WAL queue, HTTP 202/status/cancel,
  separate packager process, retries/attempts/heartbeat/timeout/stale recovery,
  expiry cleanup and atomic result publication. Repository/local/CI implementation
  is complete; production service enablement is not claimed.

Still pending:

- production enablement of SQLite reads after live reconciliation/observation;
- removal of JSON writes;
- production data/runtime cutover;
- persistent library index;
- Telethon service split;
- R2/static edge migration.

## Current state ownership during Phase 3C

```text
request
  -> legacy JSON mutation + atomic write     # source of truth
  -> SQLite shadow mirror of changed rows
  -> optional immediate row verification

reads
  -> `STATE_READ_BACKEND=legacy` by default
  -> optional read-only SQLite adapter for Phase 3 comparison
  -> optional legacy-serving SQLite shadow comparison with payload-free events
```

The SQLite mirror must never be treated as authoritative yet.

Phase 3 now provides `STATE_READ_BACKEND=legacy|sqlite` across schema-v3 state domains. `legacy` is mandatory as the default. SQLite mode is for verified local/internal API comparison and must fail closed on a missing, stale or invalid database. Phase 3B adds opt-in `STATE_READ_SHADOW_COMPARE=1` while legacy remains selected: each covered read compares SQLite, returns the legacy result and emits payload-free match/mismatch/error events. Mutation helpers still read legacy directly before the durable legacy-first write.

Phase 3C adds `scripts/verify_read_shadow_observation.py`. It audits the payload-free events from one explicitly bounded process log, fails on mismatch/error/malformed/unknown/reset counters, requires all six schema-v3 domains and emits a new path-free report that still leaves primary-read authorization false. CI also stops the SQLite-read process, starts a legacy-only process on the same canary port and repeats authenticated endpoint parity. This is a rehearsal, not evidence that production traffic or production state was observed.

Local defaults:

```text
STATE_DUAL_WRITE=1
STATE_DUAL_WRITE_STRICT=1
STATE_DUAL_WRITE_VERIFY=1
STATE_READ_BACKEND=legacy
STATE_READ_SHADOW_COMPARE=0
STATE_READ_SHADOW_STRICT=1
STATE_READ_SHADOW_REPORT_EVERY=1000
```

Production should leave `STATE_DUAL_WRITE` disabled until the explicit production shadow migration/cutover procedure is followed.

`start.bat` verifies complete local parity for `users.json`, `user_data.json`, `collection_items`, `collections.json`, `user_uploads.json`, `custom_meta.json` and the semantic allowlist before starting. If the local shadow is missing/stale, it can rebuild it from legacy files. This automatic rebuild behavior is deliberately local-development only.

All mutable domains represented by the current SQLite state schema are now runtime dual-written. Other baseline files such as community state, IP exemptions and append-only audit/download logs are not part of this schema and must not be described as migrated.

## Storage migration sequence

Do not skip phases:

1. Snapshot legacy state and calculate hashes.
2. Build a separate candidate SQLite database.
3. Import and verify round-trip parity.
4. Run SQLite `quick_check`, `integrity_check` and `foreign_key_check`.
5. Verify source hashes are unchanged.
6. Promote candidate atomically; preserve any prior SQLite file as a timestamped backup.
7. Introduce dual-write while JSON remains the read source.
8. Compare JSON and SQLite after writes.
9. Complete all mutable-state domains under dual-write.
10. Switch reads to SQLite behind a feature flag and run API parity tests.
11. Make SQLite source of truth only after a stable observation period.
12. Keep legacy files read-only/archived; deletion is a separate later operation requiring explicit approval.

See `docs/STORAGE_MIGRATION.md` and `docs/PRODUCTION_SAFETY.md`.

## Tracked runtime source and historical baseline

The repository/local/CI runtime entrypoint is `arcdb/app.py`, with templates in
`arcdb/templates/`. Edit these tracked files directly. `start.bat` and runtime CI
must not reconstruct source from `.b64` parts or apply text overlays before launch.

The verified compressed bundle, `scripts/materialize_baseline.py` and
`scripts/runtime_overlays.py` are retained only as historical provenance and as the
behavior-compatible archived reference used by source reconciliation. They are not
runtime build steps. Do not add new runtime behavior through overlays.

## Target architecture direction

Browser -> Cloudflare edge -> Cloudflare Tunnel -> OCI Flask/Gunicorn.

On OCI:

- web API service;
- SQLite WAL on Block Volume for mutable application state;
- separate EPUB packager worker;
- separate Telegram/Telethon service;
- persistent reader chapter data and working files on Block Volume.

Cloudflare later:

- static assets/cache at edge;
- R2 for immutable covers, release EPUBs and other large objects where useful.

Do not introduce Kubernetes, distributed databases or microservices without a measured need.

## Local workflow

Windows:

```bat
git pull
start.bat
```

`start.bat` creates/updates `.venv`, dependencies and local config. If the local library is empty and fixtures exist in `dev-fixtures/inbox`, it seeds once; populated data is not reset. It also ensures the local SQLite shadow is compatible and semantically equal to the current legacy user state before starting Flask.

Explicit reseed:

```bat
seed-dev.bat
```

Safe SQLite migration test:

```bat
.venv\Scripts\python.exe scripts\migrate_state_to_sqlite.py --verify
```

Full state parity check:

```bat
.venv\Scripts\python.exe scripts\verify_state_parity.py
```

## Required validation for changes

At minimum:

- keep Python CI green on supported versions;
- keep local bootstrap smoke test green;
- keep seeded `/api/library` smoke test green;
- keep Runtime Dual Write CI green while Phase 2 is active;
- for storage changes, add parity/round-trip/integrity tests;
- for any migration, prove source files were not modified;
- for SQLite backup changes, prove WAL content is captured, corruption is rejected and an independent runtime restore succeeds;
- for upload/EPUB changes, cover normal and malformed EPUBs, traversal, links, duplicate/colliding paths, entry/count/expanded-size/compression limits, Unicode paths, partial cleanup and real authenticated packaging;
- for persistent jobs, cover enqueue/claim/success/failure/retry, queued and
  processing cancellation, timeout, stale restart recovery, expiry cleanup and the
  real web -> worker -> download flow;
- keep the explicit read-cutover preflight green and its report free of paths, identities and payloads;
- keep the bounded shadow-observation audit and SQLite-canary -> legacy rollback rehearsal green;
- update docs when architecture, storage ownership or rollout state changes.

## Next ordered work

1. Run the two-stage procedure in `docs/PRODUCTION_INVENTORY.md` on the live host and reconcile its private inventory against the materialized repository baseline.
2. Review every exact private source difference and unknown metadata file, then run the explicit read-only readiness preflight against discovered production paths.
3. Enable legacy-serving shadow comparison for one bounded process, retain its private raw log and validate the payload-free events with `scripts/verify_read_shadow_observation.py`.
4. Enable SQLite reads only for a bounded internal canary with immediate rollback to `legacy`.
5. Make SQLite primary reads only after stable observation.
6. Stop legacy writes domain-by-domain later; keep rollback exports and legacy archives.

Do not jump directly to read cutover while write domains are incomplete.

## Documentation map

Start here, then read:

- `docs/PROJECT_CONTEXT.md` — concise handoff and current project state.
- `docs/ARCHITECTURE.md` — current and target architecture.
- `docs/DATA_MODEL.md` — state/files and intended ownership.
- `docs/STORAGE_MIGRATION.md` — SQLite migration phases.
- `docs/PRODUCTION_SAFETY.md` — deployment/migration invariants and rollback.
- `docs/BACKUP_RESTORE.md` — exact migration, operational backup, restore and retention procedure.
- `docs/ASYNC_PACKAGER.md` — persistent package queue, worker and rollout/rollback.
- `docs/PRODUCTION_INVENTORY.md` — exact read-only live discovery/inventory/reconciliation procedure.
- `docs/ROADMAP.md` — ordered implementation plan.
- `docs/DECISIONS.md` — architectural decisions and rationale.

When making a material architectural change, update the relevant documents in the same PR.
