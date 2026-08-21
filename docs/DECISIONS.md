# ArchiveDB architecture decisions

This file records important decisions so future contributors and AI assistants do not repeatedly re-open settled questions without new evidence.

## ADR-001 — Keep OCI as the primary compute origin

Status: accepted.

Decision:

- keep the existing OCI Ampere VM as the heavy compute origin;
- do not rewrite the application onto Cloudflare Workers.

Reasoning:

ArchiveDB has Python/Flask, Telethon, local filesystem workflows and heavy EPUB/ZIP/image processing. These are natural Linux VM workloads. Cloudflare should be used as edge/cache/security/object delivery rather than as a forced replacement for the origin.

Revisit if:

- the application is fundamentally rewritten;
- measured economics/operations justify another compute platform.

## ADR-002 — Use SQLite WAL for mutable state on the single VM

Status: accepted for current architecture.

Decision:

Replace hot JSON whole-file state with SQLite WAL on the OCI Block Volume.

Reasoning:

- one primary VM;
- low operational complexity;
- transactions and indexes;
- avoids whole-file rewrites;
- works well with local persistent block storage;
- easy to back up and inspect.

Not a forever constraint. PostgreSQL becomes reasonable if real multi-node/high-write requirements emerge.

## ADR-003 — Migration is non-destructive and candidate-first

Status: mandatory.

Decision:

- legacy data is never deleted as part of migration;
- migration builds a separate candidate DB;
- source files are backed up and hashed;
- candidate must pass round-trip and integrity checks;
- existing SQLite target must be preserved before promotion.

Reasoning:

The highest-risk failure is silent user-state loss. Operational simplicity is less important than recoverability during cutover.

## ADR-004 — Preserve API/UI during storage migration

Status: accepted.

Decision:

Introduce storage adapters and dual-write/read feature flags rather than changing frontend/API contracts at the same time as persistence.

Reasoning:

It isolates failures and makes parity tests meaningful.

## ADR-005 — Separate only services with clear process-lifecycle reasons

Status: accepted.

Decision:

Near-term split:

- web;
- EPUB packager worker;
- Telegram/Telethon worker.

Do not create many microservices.

Reasoning:

Packaging is long-running and unsuitable for HTTP request lifetime. Telethon must not duplicate with web workers. The rest benefits from staying in a modular Flask application until proven otherwise.

## ADR-006 — Do not scale Gunicorn processes before shared-state cleanup

Status: accepted.

Decision:

Avoid blindly increasing WSGI process count while process-local rate limits/caches/Telethon/locks affect correctness.

Reasoning:

Multiple processes would have divergent in-memory state and may duplicate external clients.

## ADR-007 — Move expensive filesystem discovery out of request paths

Status: planned/accepted direction.

Decision:

Create a persistent novel/chapter index updated at ingest/change time rather than repeatedly scanning directories per request.

Reasoning:

Repeated `os.walk`, full-list filtering/sorting and linear lookups become increasingly expensive as the library grows.

## ADR-008 — Use R2 selectively for immutable objects, not as a universal filesystem

Status: planned.

Decision:

Potential R2 objects:

- release EPUBs;
- covers;
- public images;
- uploads/exports where direct object delivery helps.

Keep SQLite and POSIX working data on Block Volume.

Reasoning:

Object storage is useful for immutable/large delivery objects, but not for workloads needing random local filesystem semantics.

## ADR-009 — Keep repository documentation as project memory

Status: accepted.

Decision:

Material changes must update relevant docs and `AGENTS.md` when they change rules/status.

Reasoning:

The project is being developed across multiple chats/tools and may be handed to other AI systems. Architecture knowledge must not live only in conversation history.

## ADR-010 — Production baseline must be reconciled before deployment

Status: mandatory.

Decision:

Do not assume the archived source in Git is byte-identical to the live OCI application. Obtain a sanitized snapshot/inventory or owner-provided comparison before replacing production code.

Reasoning:

The owner has not provided direct SSH access and production may contain newer local changes.

## ADR-011 — JSON remains authoritative during dual-write introduction

Status: accepted for Phase 2.

Decision:

For each newly migrated write domain:

1. perform the existing legacy JSON mutation/write first;
2. only after that succeeds, mirror the changed semantic state into SQLite;
3. keep reads on JSON;
4. compare/verify the shadow;
5. do not switch reads in the same change that introduces a new write domain.

Reasoning:

The application already depends on legacy JSON behavior. Keeping JSON authoritative isolates SQLite-mirroring failures and provides a deterministic recovery source while write coverage is incomplete.

If a strict local mirror fails after JSON succeeded, the request/test may fail loudly, but the authoritative state is still present in JSON and the shadow can be rebuilt from it.

## ADR-012 — Runtime overlay is temporary fail-closed migration plumbing

Status: accepted temporarily.

Decision:

While the Flask monolith is still stored as a compressed verified baseline, apply small migration hooks through `scripts/runtime_overlays.py` during materialization instead of repeatedly repacking the large baseline bundle.

Requirements:

- exact code matches only;
- fail if expected baseline markers are absent or duplicated;
- overlay hash participates in runtime materialization identity;
- keep overlay changes small and migration-focused;
- remove this mechanism once Flask source becomes normally tracked/refactored.

Reasoning:

It keeps current source provenance/checksum intact while allowing reviewable transitional hooks. Silent fuzzy patching would be too risky.

## ADR-013 — Local shadow auto-rebuild is not a production mechanism

Status: accepted.

Decision:

Local `start.bat` may rebuild a missing/stale SQLite shadow from authoritative JSON after verifying that another local server is not running and safely handling stale WAL sidecars.

Production must not use this convenience path. Production shadow creation/replacement follows `docs/PRODUCTION_SAFETY.md` and explicit migration commands.

Reasoning:

Fast reproducible local development is valuable, but automatic production persistence replacement would violate the project's migration safety model.
