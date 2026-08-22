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

Status: superseded by ADR-023; retained as historical migration context.

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

## ADR-014 — Preserve explicit empty legacy containers in normalized tables

Status: accepted.

Decision:

Use explicit container tables for top-level legacy buckets whose empty presence is semantically observable:

- `user_state_users` for `{"user@example.com": {}}`;
- `collection_users` for `{"user@example.com": []}`.

Do not depend on the immutable initial `legacy_documents` snapshot to reconstruct live empty containers after runtime mutations.

Reasoning:

Deleting a user's final child row must not make an existing empty top-level bucket indistinguishable from an absent user. Runtime parity and reverse export must reproduce the current legacy document exactly, including empty containers.

## ADR-015 — Allowlist parity is semantic, while the source file is preserved

Status: accepted.

Decision:

Treat `allowed_gmails.txt` as a unique lowercase set of non-comment, non-blank email entries when importing, dual-writing, verifying and exporting normalized SQLite state.

Preserve the original byte-for-byte file, including comments and formatting, in migration snapshots. Do not model comments as `allowed_emails` rows.

Reasoning:

The Flask access check already ignores comments, blank lines, duplicate entries and email case. SQLite parity must match that runtime authorization meaning rather than accidentally treating a comment as an allowed identity. Backup scope still retains the complete original file for audit and rollback.

## ADR-016 — Mirror auth users as complete opaque payloads after legacy durability

Status: accepted for Phase 2D.

Decision:

- keep the existing `users` schema v3 table and do not change password/token formats;
- atomically write `users.json` first, then upsert/delete only changed SQLite user rows;
- retain every original user value in `payload_json`, including unknown fields;
- normalize known password, verification and reset fields only for querying/verification;
- keep login/logout reads and Flask sessions on the legacy path during Phase 2;
- make local dev-account seeding idempotent when the configured password already matches;
- suppress auth message bodies in the controlled local CI workflow and never print passwords or recovered codes.

Reasoning:

Auth is the highest-risk write domain. The existing table already models every known baseline auth field while `payload_json` prevents production-only fields from being lost. Keeping the durable legacy write first preserves the established rollback source, and a separate real HTTP workflow proves registration, verification, login and reset behavior without combining read cutover.

## ADR-017 — Introduce SQLite reads as an opt-in fail-closed comparison backend

Status: accepted for Phase 3 comparison.

Decision:

- expose `STATE_READ_BACKEND=legacy|sqlite` and default to `legacy`;
- read SQLite through read-only connections with an exact schema-version check;
- never silently fall back to legacy when SQLite mode is explicitly selected but unavailable/stale;
- route every schema-v3 application state read through the adapter;
- keep mutation helpers on direct legacy reads so write ordering remains legacy durable first, SQLite mirror second;
- compare simultaneous real Flask responses on identical seeded state before any primary-read promotion.

Reasoning:

A silent fallback would hide an unsafe cutover, while changing write inputs based on the read flag could invert or corrupt the established migration sequence. Explicit fail-closed selection makes problems observable and the dual-process parity suite isolates backend differences without changing API contracts. Production-primary SQLite reads remain a later operational decision after live reconciliation and observation.

## ADR-018 — Observe SQLite alongside authoritative legacy reads before canary cutover

Status: accepted for Phase 3B.

Decision:

- allow `STATE_READ_SHADOW_COMPARE=1` only while `STATE_READ_BACKEND=legacy`;
- return the legacy value regardless of a non-strict comparison mismatch/error;
- compare complete exported domain values in memory for all schema-v3 read domains;
- emit only domain, event, process-local counter and exception type, never payload values, identities or auth secrets;
- report the first match per domain and then a configurable interval, while always reporting mismatch/error events;
- use strict mode in CI and local verification so divergence fails visibly;
- keep the later SQLite-read canary and primary-read promotion as separate operational steps.

Reasoning:

Seeded dual-process parity proves deterministic fixtures but not live production traffic or production-only fields. Legacy-serving shadow comparison exercises the same runtime loaders without changing response ownership, providing an observable low-risk gate before a bounded SQLite canary. Restricting it to legacy mode avoids an ambiguous configuration or silent fallback once SQLite is explicitly selected.

## ADR-019 — Require an explicit-path, non-authorizing readiness report before production observation

Status: accepted for Phase 3 production preparation.

Decision:

- require operators to supply the discovered production metadata root and SQLite path explicitly;
- open SQLite with `mode=ro` and `query_only`, and compare all schema-v3 exports from one read transaction;
- require core users/user-data/collections files and exact full-domain/membership parity;
- run schema, quick, integrity and foreign-key checks;
- recursively fingerprint legacy metadata before and after the preflight and fail if any file appears, disappears or changes;
- keep reports payload-free and path-free, create them only at a new path, and set canary/primary authorization false;
- treat unknown-file counts as a reconciliation prompt rather than silently claiming those files are migrated.

Reasoning:

Local and CI parity cannot prove that production paths, live-only files or live source match the repository assumptions. A read-only explicit-path gate produces shareable evidence without mutating production or leaking auth state, while its deliberately non-authorizing decision fields prevent an automated report from being mistaken for approval to change the read source.

## ADR-020 — Audit bounded shadow evidence and rehearse read rollback before live canary

Status: accepted for Phase 3C production preparation.

Decision:

- treat one stopped process log as the observation boundary so process-local counters cannot be silently combined across restarts;
- parse only the defined payload-free shadow event format and fail closed on malformed events, unknown domains, mismatch/error events or non-increasing counters;
- require reported successful matches for every schema-v3 read domain;
- create sanitized evidence only at a new path and exclude log paths, identities and payloads;
- keep primary-read authorization false in generated evidence;
- exercise an SQLite-read process and then replace it with a legacy-only process on the same canary port in CI, repeating real authenticated endpoint parity after rollback;
- continue to require separate live reconciliation, bounded production observation, canary and rollback execution.

Reasoning:

Manual grep checks prove neither a complete observation boundary nor monotonic per-process evidence, while an unstructured CI backend comparison does not demonstrate the operational rollback sequence. A strict bounded-log audit and same-port process replacement make both gates reproducible without expanding the trust assigned to fixture data or claiming unavailable production access.

## ADR-021 — Separate private production inventory from sanitized reconciliation evidence

Status: accepted for production preparation.

Decision:

- discover process/unit/config candidates read-only without guessed application or data roots;
- require operator-confirmed explicit application, metadata, SQLite, content, systemd and Cloudflared paths for structured collection;
- keep exact paths, service identities and relative filenames only in protected private artifacts;
- emit separate path-free reports containing aggregate counts, sizes, hashes and non-authorizing decisions;
- hash source/metadata without collecting payloads or environment/config secret values;
- compare production source deterministically against the behavior-compatible materialized baseline, not against unrelated repository plumbing;
- require operator review of every missing, changed, live-only source file and unknown metadata file before readiness preflight;
- refuse report overwrite and never let inventory/reconciliation authorize canary or primary reads.

Reasoning:

The live OCI layout and source revision are unknown, while a public report must not expose production paths, user identities or payload. A private exact artifact is necessary for actionable reconciliation, but it must be separated from shareable evidence. Comparing hashes against `.runtime/source` proves code equivalence without deploying or reading user state and keeps absence of production access explicit.

## ADR-022 — Use SQLite online backup and require a verified restore

Status: accepted for repository-side migration readiness.

Decision:

- create operational SQLite backups through SQLite's online backup API so committed
  WAL content is included in one consistent snapshot;
- convert the backup artifact to a portable single-file database instead of shipping
  an unmanaged live database/WAL/SHM set;
- publish only a new backup directory after SHA-256, schema, quick/integrity/FK and
  application-query checks pass;
- require a temporary restore opened through the runtime SQLite adapter before the
  backup is considered verified;
- keep manifests free of absolute paths, payloads and identities;
- restore only to a new target path and refuse existing targets or sidecars;
- keep legacy migration snapshots and SQLite-to-legacy export tooling independently.

Reasoning:

Copying only a live SQLite main file can omit committed WAL pages, while merely
hashing a copied file does not prove the application can restore it. The online
backup API provides SQLite-consistent snapshot semantics. A required runtime restore
test detects unusable artifacts before retention or cutover, and new-target-only
publication prevents recovery tooling from becoming an accidental destructive
database replacement mechanism.

## ADR-023 — Run ArchiveDB from directly tracked source

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- track the behavior-compatible Flask entrypoint at `arcdb/app.py` and its templates
  under `arcdb/templates/`;
- launch this tracked entrypoint directly from local bootstrap and runtime CI;
- retain the compressed baseline, materializer and overlay only for historical
  provenance and archived-source reconciliation;
- prohibit new runtime behavior from being introduced through text overlays;
- defer internal monolith splitting to separate behavior-preserving stages.

Reasoning:

The baseline/overlay mechanism made every runtime change depend on a generated,
ignored file and fragile exact string replacements. A behavior-identical tracked
import (with trailing whitespace normalized only) keeps current API/UI behavior while
making subsequent I/O, security, jobs and process separation changes normal
reviewable Git edits. Keeping the original verified bundle preserves provenance
without making it a production or development build dependency.

## ADR-024 — Bound and atomically publish untrusted EPUB I/O

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- write uploads sequentially to sibling temporary files, flush/fsync once, then
  atomically replace the destination;
- validate EPUB structure, XML declarations, CRC/content reads, canonical paths,
  file types, duplicates/collisions, entry counts/sizes, expanded total and
  compression ratio before publication;
- extract into a fresh sibling directory and rename it only after every bounded
  entry succeeds;
- stream package entries and reader asset recovery instead of using `ZipFile.read`
  for large binary content;
- restrict package images by signature and configurable per-file/session limits;
- bind package sessions to their authenticated creator and expire them after a
  bounded lifetime;
- keep the bounded implementation reusable by the separate persistent-jobs phase.

Reasoning:

Filename extensions and ZIP central-directory metadata alone do not protect against
traversal, link abuse, duplicate cross-platform paths, decompression bombs, damaged
content or unbounded memory. Atomic temporary publication keeps partial uploads,
extractions and final EPUBs out of live paths. The reusable bounded implementation
is safe to call from a worker.

## ADR-025 — Use a separate SQLite WAL queue for persistent EPUB jobs

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- keep package jobs in `package_jobs.sqlite3`, separate from candidate state schema
  v3, so the operational queue does not force an in-place state migration;
- make Flask enqueue only and return HTTP 202; execute packaging in a dedicated
  process that never imports the Flask runtime;
- atomically claim queued rows and persist attempts, heartbeat, progress, timeout,
  cancellation, sanitized errors and bounded retention;
- recover stale processing rows after restart while attempts remain;
- use the existing bounded/atomic EPUB implementation for result publication;
- keep queue/session paths on shared local Block Volume storage, not R2 or
  process-private temp storage;
- provide a systemd template but do not claim production enablement before inventory
  confirms paths, user ownership and units.

Reasoning:

EPUB finalization can outlive an HTTP request and must survive web/worker restart.
SQLite provides sufficient single-VM claim and recovery semantics without adding
Redis/Celery. Separating operational jobs from user-state schema v3 avoids weakening
the candidate-first migration invariant or requiring a risky in-place schema bump.

## ADR-026 — Isolate Telethon behind authenticated loopback streaming

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- remove Telethon imports, client creation, event-loop ownership and Telegram API
  credentials from `arcdb-web`;
- run exactly one `arcdb-telegram` worker that owns the protected session;
- preserve the authenticated public download route and stream its Telegram fallback
  through token-authenticated loopback HTTP;
- reject non-loopback web configuration and redirects so the shared token cannot be
  forwarded to an external host;
- keep the service token distinct from the Flask secret and keep Telegram API/session
  values in a separate private environment;
- expose payload-free process health/readiness and fail Telegram-backed downloads
  closed without taking down local library/reader flows;
- keep production installation inventory-gated and retain the previous revision and
  protected session backup for rollout rollback.

Reasoning:

Starting Telethon at Flask import creates one client per Gunicorn worker and couples
web restarts to a long-lived Telegram session. A loopback streaming boundary preserves
constant-memory downloads and the existing browser API while giving the client a
single lifecycle. A persistent queue is unnecessary for interactive media streams;
the service owns no user-state migration and can fail independently with a bounded
503 response.

## ADR-027 — Keep library discovery in a separate rebuildable SQLite index

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- keep `library_index.sqlite3` separate from candidate state schema v3;
- assign deterministic internal ids and preserve all existing external identifiers
  as explicit aliases;
- perform metadata/content scans only during an explicit sibling-candidate rebuild,
  validate SQLite integrity/counts, fsync and atomically publish;
- query the index read-only for library, novel and reader discovery and fail closed
  when it is missing or incompatible;
- update custom metadata and approved/rejected uploads incrementally;
- use optional trigram FTS5 with a SQL substring fallback;
- treat the database as private derived data, never as user-state backup or proof of
  production migration.

Reasoning:

Coupling the operational index to schema-v3 state would weaken the candidate-first
user-data migration and make a derived cache part of rollback authority. Explicit
rebuild and fail-closed reads remove full storage scans and linear lookup from hot
paths without changing API identifiers or risking user state. SQLite is sufficient
for the single-VM architecture and avoids an unnecessary search service.

The index also removes incidental custom-metadata reads from `/api/library`.
Therefore bounded shadow observation uses an explicit admin-only, payload-free
state-read probe for all schema-v3 domains instead of depending on side effects of a
filesystem scan. This keeps coverage stable as request paths become more efficient.

## ADR-028 — Reconstruct EPUB chapter HTML from an explicit parser allowlist

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- replace regex removal as the reader's security boundary with a standard-library
  HTML parser and explicit tag, attribute and URL-scheme allowlists;
- drop complete executable, embedded and foreign-namespace subtrees, including
  script, style, iframe, object, SVG and MathML;
- strip inline style, event handlers, comments, processing instructions and unknown
  attributes while preserving common semantic EPUB markup and rewritten relative
  reader asset URLs;
- accept only relative/fragment URLs or `http`, `https` and `mailto`, after
  browser-relevant control/whitespace normalization;
- make malformed active markup fail closed and reconstruct balanced escaped output;
- keep this boundary dependency-free and cover both the pure sanitizer and real
  legacy/SQLite reader endpoints with hostile fixtures.

Reasoning:

Regex substitutions cannot model HTML parsing, entity decoding, duplicate
attributes, malformed nesting or foreign namespaces reliably. Reconstructing only
known-safe output is easier to audit and fails closed. Python's parser is sufficient
for this deliberately narrow fragment policy and avoids adding a new production
dependency. Dropping inline EPUB style and SVG/MathML is an explicit security versus
fidelity tradeoff until a separately reviewed richer policy is justified.

## ADR-029 — Fail closed on upload ownership and mutable stored paths

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- expose pending upload covers only to the authenticated uploader or an admin and
  return 404 to unrelated users; retain authenticated-reader access after approval;
- retain admin-only approval/rejection and owner-only package session/job controls;
- treat every EPUB, cover, extracted-folder and download path read from mutable
  metadata as untrusted;
- resolve candidate/root paths through symlinks and require component-aware
  containment with `commonpath`, rejecting root, traversal, sibling-prefix,
  cross-drive and symlink escapes;
- apply the same helper before reads, sends, extraction targets and deletion while
  leaving invalid metadata and user files untouched.

Reasoning:

Authentication alone did not prevent an unrelated account from viewing an
unapproved cover, and string-prefix path checks can accept sibling directories such
as `structured_output_evil`. Metadata is mutable operational state and cannot be a
filesystem authorization boundary. A single realpath-aware confinement primitive
keeps normal absolute/relative deployments compatible while making corrupted or
hostile stored values fail closed. No state migration is needed.

## ADR-030 — Use response nonces before the large static-template split

Status: accepted and implemented for repository/local/CI runtime; the temporary
style exception was subsequently removed by ADR-032.

Decision:

- generate a fresh cryptographic nonce for every request and use it in the response
  CSP plus every tracked executable inline block;
- remove `unsafe-inline` and third-party CDN sources from `script-src` and set
  `script-src-attr 'none'`;
- replace the remaining HTML event-handler attributes with explicit listeners;
- remove the unused JSZip load/CDN fallback rather than vendor an unused dependency;
- extract the identical auth-page CSS to a content-versioned local asset and return
  immutable caching only for versioned static requests;
- retain `style-src 'unsafe-inline'` temporarily for large gallery/reader/community
  templates and remove it in the later behavior-preserving static CSS phase.

Reasoning:

Moving 250+ KB of coupled template CSS/JS and eliminating all dynamic style
attributes in one security patch would create unnecessary UI regression risk. A
request nonce immediately closes arbitrary inline-script and handler execution while
preserving behavior and Jinja-provided reader data. Externalizing the already
identical auth CSS proves the fingerprint/cache contract. The remaining style-only
exception does not authorize script execution and is documented rather than hidden.

## ADR-031 — Measure bounded request components and benchmark only controlled local HTTP

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- retain the payload-free total request event and add only the fixed `sqlite`,
  `filesystem`, `epub` and `job` duration components when an instrumented scope ran;
- expose those same totals through `Server-Timing` so a local HTTP runner does not
  need access to private process logs;
- aggregate only route/scenario labels, method, status, sample counts and
  p50/p95/p99 timings; never retain concrete URLs, IDs, credentials, request IDs or
  response payloads;
- restrict the authenticated workload runner to loopback HTTP and the reproducible
  seed library; use explicit bounded profile labels for controlled comparisons;
- keep upload, EPUB packaging, SQLite queue and large-library synthetic benchmarks
  beside the HTTP workload and distinguish all local results from production facts.

Reasoning:

Total latency alone cannot tell whether a regression belongs to SQLite, storage,
EPUB processing or queue control, while arbitrary dynamic labels can leak user data
and create unbounded metrics. Fixed additive scopes provide enough attribution for
the current monolith without introducing a metrics service. A loopback-only runner
prevents this developer tool from becoming an accidental production load generator
or credential transport. Local/CI measurements establish repeatability and wiring;
Gunicorn, traffic concurrency and Block Volume tuning still require reconciled live
inputs.

## ADR-032 — Externalize page CSS and validate immutable asset versions

Status: accepted and implemented for repository/local/CI runtime.

Decision:

- move the existing gallery, reader, community and generated admin styles into
  tracked local CSS files without a visual redesign or API change;
- replace fixed inline presentation and dynamic visibility/width writes with named
  classes, `hidden`, semantic `progress` elements and bounded Web Animations while
  retaining reader preference custom properties;
- content-version every runtime stylesheet with the first 16 hexadecimal SHA-256
  characters and grant immutable caching only when that prefix matches the actual
  confined static file;
- set `style-src 'self'` and `style-src-attr 'none'`, retaining the existing
  per-response nonce contract for executable inline bootstrap code;
- require structural, real HTTP and local browser regression checks for the large
  page templates.

Reasoning:

The large style blocks were stable presentation assets that inflated every HTML
response and forced a broad CSP exception. A mechanical extraction plus explicit
state primitives preserves current behavior while closing that exception. Checking
the requested version against the served bytes prevents an arbitrary hash-shaped
query from making stale unversioned content immutable. Production enablement still
depends on reconciled rollout and observed headers.
