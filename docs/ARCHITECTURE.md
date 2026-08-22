# ArchiveDB architecture

## 1. Scope

This document separates **confirmed production facts**, **source-level observations**, **unknown infrastructure details** and the **target architecture direction**. Do not collapse those categories: the OCI instance has not been fully inventoried.

## 2. Current architecture — confirmed

- Application: Python + Flask.
- Production compute: OCI Ampere A1 ARM instance.
- Compute size reported by the owner: 4 OCPU / 24 GB RAM.
- Persistent storage: OCI Block Volume.
- Public edge: Cloudflare.
- Telegram integration: Telethon.
- Current codebase uses local files for EPUBs, chapters/assets and application metadata/state.

## 3. Current architecture — strongly indicated by tracked archived source

```text
Browser
  -> Cloudflare edge / workers.dev
  -> Cloudflare Tunnel or Cloudflare-aware origin path
  -> OCI Ubuntu VM
     -> Flask application
        -> local JSON/CSV state
        -> local EPUB files
        -> unpacked chapter/image trees
        -> caches/temp packaging sessions
        -> Telethon client/thread
```

Source clues include `CF-Connecting-IP`, Cloudflare-related comments and `/home/ubuntu/...` paths.

The behavior-compatible monolith is now normal tracked code at `arcdb/app.py`, with
tracked templates in `arcdb/templates/`. Repository/local/CI startup no longer
materializes the compressed baseline or applies runtime overlays. The historical
bundle remains available for provenance and comparison with an older live layout.

Treat Cloudflare Tunnel as strongly indicated, not finally confirmed, until live configuration is supplied.

## 4. Current monolith responsibilities

The archived Flask process currently covers too many responsibilities:

- authentication/session handling;
- library discovery/filter/sort;
- reader/chapter serving;
- per-user reading progress/status;
- collections;
- uploads and approval flows;
- custom metadata;
- community data;
- download counters/rate limiting;
- EPUB upload/finalization/packaging;
- image handling;
- Telegram/Telethon integration;
- filesystem scans and caches.

This does not mean the application should be rewritten as microservices. The goal is a modular monolith plus a few separate long-running workers where process separation has clear operational value.

## 5. Important current constraints

### Process-local state

The archived application contains process-local structures such as:

- locks;
- request/rate-limit buckets;
- caches;
- download trackers;
- Telethon client/thread state.

Therefore, blindly increasing Gunicorn worker count can produce divergent state and duplicate Telegram clients.

### Filesystem scanning

Library/chapter discovery uses filesystem walking and repeated in-memory list operations. This creates avoidable O(N) / O(N log N) work on request paths.

### JSON whole-file writes

Mutable state such as reading progress can require reading, mutating and rewriting an entire JSON document. This is one of the first storage bottlenecks being replaced.

### Heavy request work

Historical production source may still perform EPUB/ZIP/image processing inside HTTP
requests until the new worker revision is reconciled and deployed. The tracked
repository runtime no longer does final EPUB construction in Flask.

### Bounded EPUB handling

Repository/local/CI upload, extraction, reader asset recovery and package-finalization
paths now enforce archive entry/count/expanded-size/compression/path/link/duplicate
limits. Non-text entries are copied in bounded chunks and package publication is
atomic. Final packaging now runs through the persistent worker; init and bounded
client image upload remain HTTP operations.

## 6. Near-term target architecture

```text
                           Browser
                              |
                              v
                      Cloudflare edge
                 + static/cache/security
                              |
                 +------------+------------+
                 |                         |
                 v                         v
          immutable objects           API traffic
          (later: R2)                     |
                                           v
                                 Cloudflare Tunnel
                                           |
                                           v
                              OCI Ampere VM / Ubuntu
                               4 OCPU / 24 GB RAM
                                           |
              +----------------------------+-------------------------+
              |                            |                         |
              v                            v                         v
       Flask/Gunicorn web          EPUB packager worker       Telegram worker
              |                            |                         |
              +---------------+------------+-------------------------+
                              |
                              v
                        OCI Block Volume
                 +------------+-------------+
                 |            |             |
                 v            v             v
            SQLite WAL    chapters/work    temp/cache/jobs
```

This is intentionally simple. OCI remains the heavy compute origin.

## 7. Storage ownership target

Current transition status: Phase 2A–2D writes are complete for schema v3. Phase 3 provides `STATE_READ_BACKEND=legacy|sqlite` for users, user state, collections, uploads, custom metadata and allowlist. Phase 3B can additionally read SQLite alongside legacy-served requests and emit payload-free comparison events. An explicit-path read-only preflight produces a sanitized, non-authorizing health/parity/source-stability report for production reconciliation. Phase 3C validates one bounded process's event stream and CI rehearses replacement of an SQLite-read canary process with a legacy-only process on the same port. WAL-aware operational backup creates a portable verified artifact through SQLite's online backup API and proves it with a temporary runtime restore; restoration publishes only a new path. `legacy` remains the default; these local/CI controls do not substitute for live reconciliation, traffic observation or operator authorization.

### SQLite WAL

For hot mutable application state:

- users/auth state;
- reading progress/status/hidden/download counters;
- collections/memberships;
- uploads metadata;
- custom metadata;
- allowlist;
- package jobs in a dedicated operational SQLite WAL database (implemented; not
  part of state schema v3 or evidence of production enablement);
- later: other jobs/events where appropriate.

SQLite is appropriate while ArchiveDB is a single-primary-VM application. It can later be replaced by PostgreSQL if actual multi-node/high-concurrency write requirements appear.

### OCI Block Volume

For local working/persistent data requiring POSIX filesystem semantics:

- SQLite database;
- unpacked reader chapters/assets initially;
- working directories;
- packaging temp/job state;
- caches that make sense locally;
- Telethon session state (protected, never committed).

### Cloudflare R2 — later/selective

Potential ownership:

- immutable release EPUBs;
- covers;
- notice/public images;
- large uploaded objects;
- prebuilt exports.

Do not force reader working trees or SQLite into R2.

## 8. Process/service target

Near-term services on OCI:

```text
arcdb-web.service
arcdb-packager.service
arcdb-telegram.service
cloudflared.service
```

The names are proposed, not yet live-production facts. The repository includes an
`arcdb-packager` systemd template, but actual paths/user/unit enablement are unknown.

### Web service

Responsibilities:

- HTTP/API/auth;
- lightweight metadata access;
- reader responses;
- enqueue long-running jobs.

The tracked runtime now enqueues packaging and returns HTTP 202; it does not import
or invoke the package builder in the finalize request.

### Packager worker

Responsibilities:

- EPUB finalization;
- ZIP creation;
- image/package transformations;
- long-running export work.

API pattern:

```text
POST /api/package -> 202 + job_id
GET /api/jobs/<id> -> queued/processing/done/failed + progress
```

Implemented queue states also include `cancelled`. The worker persists attempts,
heartbeat, timeout, cancellation and stale recovery in `package_jobs.sqlite3`; final
EPUB publication remains bounded and atomic. See `docs/ASYNC_PACKAGER.md`.

### Telegram worker

Owns the Telethon client/session and Telegram synchronization/events. It must not be duplicated merely because web workers scale.

## 9. Library indexing target

Current request-time filesystem scanning should become an ingest/update-time persistent index.

Candidate indexed fields:

- stable novel id/library key;
- title/original title;
- source/raw id;
- translation/raw file paths/object keys;
- chapter count;
- cover reference;
- tags/metadata;
- updated timestamp;
- content hash/version.

HTTP library queries should become indexed database queries rather than `os.walk` + full-list sort/filter.

## 10. Frontend/static target

Large inline CSS/JS in HTML should gradually move to tracked static files:

```text
static/css/base.css
static/css/gallery.css
static/css/reader.css
static/js/common.js
static/js/gallery.js
static/js/reader.js
static/js/community.js
```

Benefits:

- browser/Cloudflare caching;
- smaller HTML responses;
- clearer source ownership;
- easier CSP tightening;
- vendoring third-party runtime dependencies such as JSZip rather than relying on an external CDN at runtime.

## 11. Security direction

Preserve/strengthen:

- secure session cookies in production;
- HttpOnly;
- SameSite;
- explicit admin authorization.

Review/add:

- CSRF/same-origin controls for state-changing endpoints;
- robust allowlist HTML sanitization for user EPUB content;
- upload type/size/session ownership limits;
- separation of secrets from repository;
- Cloudflare/origin network restrictions.

## 12. Performance priorities already identified

Ordered roughly by value/risk:

1. move long packaging out of HTTP requests (upload/EPUB I/O itself is now bounded and streamed);
2. replace hot JSON whole-file state with SQLite WAL;
3. replace repeated linear novel lookup with indexed lookup;
4. build persistent library/chapter index;
5. split Telethon from web process;
6. then tune Gunicorn workers/threads using measured workload;
7. split/cache frontend static assets;
8. introduce R2 selectively;
9. reduce background community polling / use visibility/backoff and later push mechanisms.

## 13. Production details still unknown

Before deployment decisions that depend on them, collect:

- Boot Volume size/filesystem.
- Block Volume size.
- Block Volume performance tier (VPU/GB).
- attachment type and mount point.
- filesystem type/mount options.
- disk/inode utilization.
- current live ArchiveDB path.
- current systemd/process-manager configuration.
- Cloudflared configuration/routing.
- Python/package versions.
- existing backup/snapshot strategy.
- real data sizes and request/job load.

Use the complete read-only procedure in `docs/PRODUCTION_INVENTORY.md`:
`scripts/oracle_inventory.sh` discovers candidate paths without guessed roots, the
structured collector fingerprints operator-confirmed paths without payloads, and the
reconciliation tool compares live source with the explicitly materialized historical
layout. This materialization is not the runtime launch path; tracked changes under
`arcdb/` are the proposed deployment delta. These tools are implemented; no live
result is available yet.

## 14. Architecture rules

- Do not rewrite everything onto Cloudflare Workers.
- Do not introduce PostgreSQL merely because it is more familiar; justify it with measured multi-node/write needs.
- Do not introduce Kubernetes.
- Do not mix storage migration, filesystem cleanup and frontend redesign in one risky production cutover.
- Do not remove rollback paths during a migration.
- Keep documentation updated whenever ownership of a component/state changes.
