# ArchiveDB project context

This is the fast handoff document for a new chat, AI assistant or engineer.

## What ArchiveDB is

ArchiveDB is a Flask-based novel library/reader with EPUB ingestion, local unpacked chapters/assets, per-user reading state, collections, uploads, community features and Telegram/Telethon integration.

The current production application is a large monolith. The repository contains an archived development baseline plus tooling to make it reproducible locally. The production OCI instance may contain newer changes, so production deployment must reconcile against a sanitized live snapshot before replacing code.

## Infrastructure known today

Confirmed:

- Oracle Cloud Infrastructure VM.
- Ampere A1 ARM.
- 4 OCPU / 24 GB RAM.
- OCI Block Volume for persistent storage.
- Cloudflare in front of the service.
- Python + Flask.
- Telethon Telegram integration.

Strongly indicated by archived code:

- Cloudflare Tunnel/Cloudflare-aware origin handling;
- local JSON/CSV application state;
- local EPUB files and unpacked reader trees;
- synchronous packaging/ZIP work in request handlers;
- process-local rate-limit state/caches/locks;
- Telethon started from the application process.

Unknown until production inventory is supplied:

- exact Block Volume size/VPU/mount point/filesystem;
- exact live application path and source revision;
- exact service manager/Gunicorn configuration;
- exact Cloudflare Tunnel config;
- production traffic profile and data volume;
- current backup/snapshot policy.

## Local development state

Windows workflow:

```bat
git pull
start.bat
```

The launcher:

- creates `.venv`;
- installs dependencies when requirements change;
- creates local `.env`;
- reconstructs the archived baseline into `.runtime/source`;
- applies fail-closed tracked runtime overlays;
- creates a local dev account;
- if the local library is empty and EPUB fixtures are available, seeds once;
- ensures a compatible SQLite shadow exists;
- verifies full `user_data.json`/SQLite semantic parity;
- starts the site on `127.0.0.1:5004`.

Explicit reseed:

```bat
seed-dev.bat
```

Fixtures and generated data are local-only and ignored by Git.

## Current storage problem

Mutable state is stored in files such as:

- `users.json`
- `user_data.json`
- `collections.json`
- `user_uploads.json`
- `custom_meta.json`
- `allowed_gmails.txt`

Frequent operations can read the complete JSON document, mutate a small value, rewrite the complete file and fsync. This does not scale well and makes safe multi-process serving difficult.

## Current SQLite migration status

SQLite WAL on the OCI Block Volume is the planned mutable-state database for the single-VM architecture.

Implemented in repository:

- schema v3 with explicit empty user containers for both user state and collections;
- non-destructive JSON -> SQLite initial migration;
- candidate-first promotion and rollback backups;
- SHA-256 legacy snapshots;
- round-trip/integrity/FK checks;
- SQLite -> legacy reverse export;
- runtime Phase 2A dual-write for hot per-novel user state;
- runtime Phase 2B dual-write for collection metadata and memberships;
- runtime Phase 2C dual-write for uploads, custom metadata and allowlist;
- runtime Phase 2D dual-write for registration, verification, password-reset and local dev-account user records;
- complete unknown-field-preserving `users.json` parity and idempotent dev-account bootstrap;
- full users/auth, user-state, collection metadata, normalized membership and Phase 2C metadata parity checker;
- real route-level dual-write CI;
- feature-flagged SQLite reads plus simultaneous legacy/SQLite API parity;
- legacy-serving runtime shadow comparison with payload-free events and strict all-domain CI;
- explicit-path, read-only production readiness preflight with recursive source-hash stability, full parity and a sanitized non-authorizing report;
- bounded-process shadow-event auditing and a real Flask SQLite-canary -> legacy rollback rehearsal in CI.
- read-only host discovery plus explicit-path structured production inventory and materialized-baseline reconciliation, with separate private and path-free reports.
- WAL-aware SQLite online backup with SHA-256 manifest, database integrity checks, temporary runtime restore verification and safe new-target-only restore tooling.

SQLite is **not** the default or production read source. Phase 3 now exposes `STATE_READ_BACKEND=legacy|sqlite`; local/CI can run the same authenticated API flows against both backends, while `legacy` remains default. Phase 3B adds `STATE_READ_SHADOW_COMPARE=1` for legacy-served requests: SQLite is read only for equality checking and payload-free match/mismatch/error events, so non-strict observation cannot replace or damage the authoritative response.

Phase 3C adds a fail-closed audit for one bounded process log and tests the immediate configuration rollback by replacing the SQLite-read canary process with a legacy-only process on the same port. Its sanitized report never includes the log path, identities or payloads and does not authorize primary reads. Repository-side migration, backup and restore preparation is implemented and tested; production reconciliation and observation remain separate operator work when real inputs are available.

Current write flow for covered mutations:

```text
request
  -> legacy JSON write succeeds
  -> changed record(s) mirrored to SQLite
  -> local/CI immediate verification
```

Covered Phase 2A/2B/2C/2D mutations:

- progress;
- status;
- last-read state;
- hidden state;
- download counters;
- bulk user-state removal;
- embedded collection membership on affected records;
- collection create/rename/delete;
- collection assign/unassign and repeated operations;
- delete membership cleanup;
- community collection import;
- upload submission, approval/update and rejection/delete;
- custom metadata updates;
- allowlist add/deduplicate/revoke.
- registration and replacement of an unverified registration;
- verification attempts, success and token-field cleanup;
- password-reset request, attempts, password hash update and token-field cleanup;
- local dev-account create/update while preserving unknown fields and an already valid hash.

Every mutable domain represented by the current SQLite state schema is now dual-written. `community.json`, IP-exemption state and append-only audit/download logs are separate baseline state outside schema v3 and are not claimed as migrated.

## Non-negotiable migration rule

Legacy state is not deleted during migration.

Production migration must be:

```text
legacy files
    -> immutable snapshot + hashes
    -> candidate SQLite
    -> round-trip verification
    -> SQLite integrity/FK checks
    -> source hash re-check
    -> atomic candidate promotion
    -> legacy files remain preserved
```

Production dual-write is then enabled explicitly by domain while reads remain legacy-first. Legacy cleanup, if ever performed, is a separate later operation requiring explicit approval.

## Local dual-write behavior

Local defaults enable:

```text
STATE_DUAL_WRITE=1
STATE_DUAL_WRITE_STRICT=1
STATE_DUAL_WRITE_VERIFY=1
STATE_READ_BACKEND=legacy
STATE_READ_SHADOW_COMPARE=0
STATE_READ_SHADOW_STRICT=1
STATE_READ_SHADOW_REPORT_EVERY=1000
```

If local state is reseeded and the shadow becomes stale, bootstrap can safely rebuild it because JSON is still authoritative. It refuses automatic rebuild outside local development.

Manual full parity check:

```bat
.venv\Scripts\python.exe scripts\verify_state_parity.py
```

Explicit read-only cutover preflight (use discovered production paths, never guessed ones):

```bat
.venv\Scripts\python.exe scripts\verify_read_cutover_readiness.py --meta-dir data\metadata --db data\arcdb.sqlite3
```

Bounded shadow-log audit (one process log per invocation):

```bat
.venv\Scripts\python.exe scripts\verify_read_shadow_observation.py --log private-shadow.log --report new-observation-report.json
```

## Important source problems already identified

1. JSON whole-file state rewrites.
2. Upload loop fsyncs repeatedly instead of once after sequential write.
3. EPUB finalization can load the complete archive and every entry into RAM.
4. Packaging work runs synchronously inside HTTP requests.
5. Important state/caches/rate limits are process-local.
6. Telethon starts inside the application process.
7. Library requests sort/filter a full in-memory list.
8. Directory scans/`os.walk` are used for chapter/library discovery.
9. Novel lookup uses repeated linear scans.
10. Frontend HTML files contain large inline CSS/JS.
11. Community uses frequent polling.
12. Current large-upload constraints can conflict between Flask and Cloudflare.
13. User EPUB sanitization uses regex-style HTML cleaning rather than a robust allowlist parser.
14. State-changing routes need a systematic CSRF/same-origin review.

## Target architecture

Near-term, intentionally simple:

```text
Browser
  -> Cloudflare edge
     -> static/cache/R2 where appropriate
     -> Cloudflare Tunnel
        -> OCI VM
           -> Flask/Gunicorn web
           -> SQLite WAL
           -> separate EPUB packager
           -> separate Telegram service
           -> Block Volume working/chapter data
```

Avoid a full rewrite. Keep Flask and preserve API/UI behavior while extracting responsibilities gradually.

## Current next ordered work

1. Run `docs/PRODUCTION_INVENTORY.md` on the live host and collect the exact private application/data/service inventory.
2. Reconcile every source difference and unknown metadata file against `.runtime/source`, then run the explicit-path readiness preflight.
3. Run legacy-serving shadow comparison for one bounded internal process and validate its payload-free events.
4. Run a separate bounded SQLite-read canary with tested rollback to `legacy`.
5. Make SQLite primary read source only after stable observation.
6. Stop JSON writes domain-by-domain; preserve legacy/export rollback paths.
7. Optimize upload fsync and EPUB streaming.
8. Move packaging to async jobs.
9. Split Telethon into its own service.
10. Build the persistent index and later frontend/R2 optimizations.

## Where to read next

- `AGENTS.md` — mandatory rules and exact current status.
- `docs/ARCHITECTURE.md` — component-level architecture.
- `docs/STORAGE_MIGRATION.md` — migration phases and runtime flags.
- `docs/PRODUCTION_SAFETY.md` — backup/cutover/rollback rules.
- `docs/BACKUP_RESTORE.md` — executable migration, backup, restore and retention runbook.
- `docs/DATA_MODEL.md` — files and SQLite ownership.
- `docs/ROADMAP.md` — implementation sequence.
- `docs/DECISIONS.md` — why these choices were made.
