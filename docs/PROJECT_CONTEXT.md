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

- Cloudflare Tunnel/Cloudflare-aware origin handling.
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

Local Windows workflow:

```bat
git pull
start.bat
```

The launcher:

- creates `.venv`;
- installs dependencies when requirements change;
- creates local `.env`;
- reconstructs the archived baseline into `.runtime/source`;
- creates a local dev account;
- if the local library is empty and EPUB fixtures are available, seeds once;
- starts the site on `127.0.0.1:5004`.

Explicit reseed:

```bat
seed-dev.bat
```

Fixtures are local-only and ignored by Git.

## Current storage problem

Mutable state is stored in files such as:

- `users.json`
- `user_data.json`
- `collections.json`
- `user_uploads.json`
- `custom_meta.json`
- `allowed_gmails.txt`

Frequent operations can read the complete JSON document, mutate a small value, rewrite the complete file and fsync. This does not scale well and makes safe multi-process serving difficult.

## Current migration direction

SQLite WAL on the OCI Block Volume is the planned mutable-state database for the single-VM architecture.

Why SQLite now:

- one primary VM;
- simple operation and backup;
- strong transactional guarantees;
- removes whole-file JSON writes;
- no separate database service required;
- straightforward future migration to PostgreSQL if the architecture ever becomes multi-node/high-write.

The repository currently contains a shadow SQLite schema/importer. Flask has NOT yet been switched to SQLite as source of truth.

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

Runtime cutover then proceeds via dual-write and parity testing. Legacy cleanup, if ever performed, is a separate later operation requiring explicit approval.

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

## Next ordered work

1. Harden migration safety and backup verification.
2. Add state repository abstraction.
3. Dual-write hot state to JSON + SQLite.
4. Add automatic parity checks.
5. Enable SQLite reads behind a feature flag.
6. Switch SQLite to source of truth after observation.
7. Optimize upload fsync and EPUB streaming.
8. Move packaging to async jobs.
9. Split Telethon into its own service.
10. Build persistent novel/chapter index.
11. Split static frontend assets.
12. Introduce R2 selectively for immutable large objects.

## Where to read next

- `AGENTS.md` — rules for AI/contributors.
- `docs/ARCHITECTURE.md` — component-level architecture.
- `docs/STORAGE_MIGRATION.md` — migration phases.
- `docs/PRODUCTION_SAFETY.md` — backup/cutover/rollback rules.
- `docs/DATA_MODEL.md` — files and SQLite ownership.
- `docs/ROADMAP.md` — implementation sequence.
- `docs/DECISIONS.md` — why these choices were made.