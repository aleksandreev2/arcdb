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

## Current migration status

Completed:

- reproducible Windows local bootstrap;
- optional/automatic local EPUB fixture seeding;
- SQLite WAL schema foundation;
- JSON -> SQLite shadow import with round-trip tests.

Not completed yet:

- runtime dual-write;
- SQLite reads in Flask;
- removal of JSON writes;
- production data migration;
- library index migration;
- job/packager split;
- Telethon service split;
- R2/static edge migration.

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
9. Switch reads to SQLite behind a feature flag and run API parity tests.
10. Make SQLite source of truth only after a stable observation period.
11. Keep legacy files read-only/archived; deletion is a separate later operation requiring explicit approval.

See `docs/STORAGE_MIGRATION.md` and `docs/PRODUCTION_SAFETY.md`.

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

`start.bat` creates/updates `.venv`, dependencies and local config. If the local library is empty and fixtures exist in `dev-fixtures/inbox`, it seeds once; populated data is not reset.

Explicit reseed:

```bat
seed-dev.bat
```

SQLite migration test:

```bat
.venv\Scripts\python.exe scripts\migrate_state_to_sqlite.py --verify
```

## Required validation for changes

At minimum:

- keep Python CI green on supported versions;
- keep local bootstrap smoke test green;
- keep seeded `/api/library` smoke test green;
- for storage changes, add parity/round-trip/integrity tests;
- for any migration, prove source files were not modified;
- update docs when architecture, storage ownership or rollout state changes.

## Documentation map

Start here, then read:

- `docs/PROJECT_CONTEXT.md` — concise handoff and current project state.
- `docs/ARCHITECTURE.md` — current and target architecture.
- `docs/DATA_MODEL.md` — state/files and intended ownership.
- `docs/STORAGE_MIGRATION.md` — SQLite migration phases.
- `docs/PRODUCTION_SAFETY.md` — deployment/migration invariants and rollback.
- `docs/ROADMAP.md` — ordered implementation plan.
- `docs/DECISIONS.md` — architectural decisions and rationale.

When making a material architectural change, update the relevant documents in the same PR.