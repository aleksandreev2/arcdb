# ArchiveDB

ArchiveDB web application. The behavior-compatible runtime imported from the latest
source archive available to us is now tracked directly under `arcdb/`; the Oracle
instance may contain newer production-only changes and will be reconciled later
without overwriting server data blindly.

## Start here for AI / new contributors

Read these before architecture/storage changes:

1. `AGENTS.md` — mandatory project/safety rules.
2. `docs/PROJECT_CONTEXT.md` — concise project handoff/current state.
3. `docs/ARCHITECTURE.md` — confirmed/current/target architecture.
4. `docs/DATA_MODEL.md` — current and target data ownership.
5. `docs/STORAGE_MIGRATION.md` — staged JSON -> SQLite plan.
6. `docs/PRODUCTION_SAFETY.md` — backup/cutover/rollback invariants.
7. `docs/BACKUP_RESTORE.md` — migration, WAL-aware backup and verified restore runbook.
8. `docs/PRODUCTION_INVENTORY.md` — live discovery/inventory/reconciliation procedure.
9. `docs/ROADMAP.md` — ordered implementation plan.
10. `docs/DECISIONS.md` — architectural decisions and rationale.
11. `docs/PERFORMANCE_BASELINE.md` — reproducible before/after measurements.
12. `docs/ASYNC_PACKAGER.md` — persistent queue, worker, API and rollout procedure.
13. `docs/TELEGRAM_SERVICE.md` — isolated Telethon service, rollout and rollback.
14. `docs/OBSERVABILITY.md` — sanitized web health/readiness and request timing.
15. `docs/SECURITY.md` — state-changing request origin policy and remaining controls.
14. `docs/LIBRARY_INDEX.md` — persistent library/chapter index, rebuild and rollout.

Material architectural changes should update the relevant docs in the same PR.

## Windows: one-click local start

Requirements: **Python 3.11+** and Git for Windows.

```bat
git clone https://github.com/aleksandreev2/arcdb.git
cd arcdb
start.bat
```

`start.bat` delegates setup to `scripts/dev_bootstrap.py`. It automatically:

1. creates `.venv` when needed;
2. installs packages from `requirements.txt` when requirements change;
3. creates a local `.env` from `.env.local.example`;
4. generates a random local Flask secret;
5. creates the local `data/` tree;
6. verifies the directly tracked `arcdb/app.py` runtime and required templates;
7. creates/maintains the local development login;
8. if the local library is empty and EPUB fixtures exist in `dev-fixtures/inbox`, seeds once;
9. creates or verifies the schema v3 SQLite shadow and full auth/user/collection/metadata parity;
10. atomically rebuilds and verifies the local persistent library/chapter index;
11. starts web and the separate EPUB packager at `http://127.0.0.1:5004/login` and
    opens it in the browser. The separate Telegram service remains opt-in locally.

Normal startup does **not** reset an already populated local library.

Default local login:

```text
dev@arcdb.local
arcdb-dev-123
```

The local account exists only inside ignored `data/` files. Change `LOCAL_DEV_EMAIL` / `LOCAL_DEV_PASSWORD` in `.env` if desired.

### Explicitly rebuild development data

To intentionally reset/rebuild the local fixture library:

```text
seed-dev.bat
```

Put the supplied fixture archive at either location:

```text
dev-fixtures/inbox/Downloads.zip
```

or:

```text
Downloads.zip
```

You can also drag a ZIP/EPUB/folder onto `seed-dev.bat` or run:

```bat
seed-dev.bat "D:\path\to\Downloads.zip"
```

`seed-dev.bat` first runs `scripts/dev_bootstrap.py --setup-only`, so it automatically creates the virtual environment and installs/updates dependencies without starting Flask. It then resets **only local dev data**, scans the EPUB fixtures and creates a production-like local library.

The fixture mapping seeds RAW/translated/RAW+translated cases, local users/progress/collections/community data and writes `data/dev-seed-report.json`. PDF and `.file` entries are ignored; unused alternate EPUBs are reported rather than added as duplicate cards.

Seeder safety requirements:

- `ARCHIVEDB_LOCAL_DEV=1`;
- loopback-only host;
- writable ArchiveDB dev paths inside this checkout's `data/` directory.

### Subsequent updates

Double-click:

```text
update-and-start.bat
```

It performs `git pull --ff-only` and then launches `start.bat`.

## SQLite shadow migration

ArchiveDB is migrating hot mutable state away from whole-file JSON rewrites. Phase 2A user-state, Phase 2B collections, Phase 2C uploads/custom-metadata/allowlist and Phase 2D users/auth now dual-write to the verified SQLite WAL shadow after the durable legacy write. The Flask baseline still reads legacy files and has **not** switched SQLite to the production source of truth.

Phase 3 adds an opt-in comparison backend:

```text
STATE_READ_BACKEND=legacy   # default
STATE_READ_BACKEND=sqlite   # verified shadow comparison only
```

Phase 3B can compare SQLite on real legacy-served reads without changing the response source:

```text
STATE_READ_BACKEND=legacy
STATE_READ_SHADOW_COMPARE=1
STATE_READ_SHADOW_STRICT=0        # fail-safe observation
STATE_READ_SHADOW_REPORT_EVERY=1000
```

The first successful comparison per domain and then every configured interval are logged as payload-free events; mismatches and SQLite errors are always logged. Strict mode is enabled in CI so any divergence fails visibly. Both explicit backends and runtime shadow comparison are exercised against identical seeded API flows in CI. SQLite is not the production default or source of truth; production enablement still requires live reconciliation, bounded observation and a separate canary.

Phase 3C makes the observation and rollback gates reproducible. Audit the private log from exactly one bounded shadow-comparison process into a new sanitized report:

```bash
python scripts/verify_read_shadow_observation.py \
  --log /explicit/private/shadow-process.log \
  --minimum-reported-matches 1 \
  --report /new/private/path/shadow-observation.json
```

The audit requires increasing successful counters for all six schema-v3 domains and fails on mismatch, SQLite error, malformed/unknown events or ambiguous process-counter resets. The report contains only domain counters and decisions; the raw application log must still be treated as private. CI rehearses the next step by running the authenticated API suite through SQLite reads, replacing that canary process with a legacy-only process on the same port and repeating parity. This proves the repository rollback mechanism, not a production rollout; primary-read authorization remains false.

Before any production observation or canary, run the read-only preflight with explicit discovered paths:

```bash
python scripts/verify_read_cutover_readiness.py \
  --meta-dir /explicit/live/metadata \
  --db /explicit/live/arcdb.sqlite3 \
  --report /new/private/path/readiness-report.json
```

It requires the core legacy files, verifies every schema-v3 domain in one read-only SQLite snapshot, runs schema/quick/integrity/foreign-key checks and proves the recursive legacy source hashes stayed stable. The optional report contains only counts/check results and refuses overwrite. A passing report explicitly does **not** authorize SQLite canary or primary reads.

Local safe migration test:

```bat
.venv\Scripts\python.exe scripts\migrate_state_to_sqlite.py --verify
```

The migration is candidate-first and non-destructive:

```text
legacy metadata/CSV state
  -> verified timestamped snapshot + SHA-256 manifest
  -> separate candidate SQLite
  -> round-trip + quick_check + integrity_check + foreign_key_check
  -> source hash re-check
  -> candidate promotion
```

Legacy files are not modified/deleted by the migration. If an SQLite target already exists it is preserved before candidate promotion. See `docs/PRODUCTION_SAFETY.md` before any production use.

## Verified SQLite backup and restore

After SQLite exists, create a consistent operational backup with the SQLite online
backup API. This includes committed WAL pages without copying a live database and its
sidecars by hand:

```bash
PYTHONPATH=. python scripts/create_sqlite_backup.py \
  --db /explicit/data/arcdb.sqlite3 \
  --backup-dir /new/backup/directory

PYTHONPATH=. python scripts/verify_sqlite_backup.py /new/backup/directory

PYTHONPATH=. python scripts/restore_sqlite_backup.py \
  --backup-dir /new/backup/directory \
  --target-db /new/restore/arcdb.sqlite3
```

Creation and independent verification both perform integrity, foreign-key,
application-query and temporary runtime restore checks. Restore refuses to overwrite
an existing database or sidecar and publishes only a new verified target. Legacy
files remain preserved. See `docs/BACKUP_RESTORE.md` for the complete migration,
backup, retention and rollback sequence.

## Production inventory and source reconciliation

No live production inventory or sanitized snapshot is included in this repository, so current production paths/configuration are still unknown. Repository-side tooling now provides the complete read-only procedure without inventing those facts:

```bash
bash scripts/oracle_inventory.sh > /new/private/path/oracle-inventory.txt

PYTHONPATH=. python3 scripts/collect_production_inventory.py \
  --app-root /explicit/live/source \
  --meta-dir /explicit/live/metadata \
  --sqlite-db /explicit/live/arcdb.sqlite3 \
  --content-root chapters=/explicit/live/chapters \
  --private-report /new/private/path/production-inventory-private.json \
  --report /new/private/path/production-inventory-report.json

python3 scripts/materialize_baseline.py --force
PYTHONPATH=. python3 scripts/reconcile_production_inventory.py \
  --inventory /private/path/production-inventory-private.json \
  --reference-root .runtime/source \
  --private-report /new/private/path/production-reconciliation-private.json \
  --report /new/private/path/production-reconciliation-report.json
```

Private artifacts retain the exact paths and relative filenames needed by the operator. Separate sanitized reports contain aggregate counts/hashes and no paths, user identities, payloads or secret values. All reports leave readiness, canary and primary-read authorization false. See `docs/PRODUCTION_INVENTORY.md` for systemd, Gunicorn, Cloudflared, mount and unknown-file handling.

## Upload and EPUB safety

User uploads are streamed to a sibling temporary file, flushed/fsynced once and
atomically published. EPUB validation reads the complete archive with bounded memory
and rejects malformed structure/CRC, unsafe or colliding paths, links/special files,
encryption, oversized entries, excessive expanded size/count and unsafe compression
ratios. Extraction and final EPUB publication are atomic.

Client-assisted package sessions are bound to the authenticated creator, expire,
limit active sessions/files/bytes and accept only signature-validated raster images
for URLs discovered in that session's base EPUB. Finalization retains the existing
route but now returns HTTP 202 with a persistent job id. The frontend polls the
owner-only status endpoint while `scripts/run_packager.py` streams and atomically
publishes the result in a separate process. Retry, heartbeat, timeout, cancellation,
stale restart recovery and expiry cleanup are stored in a dedicated SQLite WAL queue.

Pending novel cover assets are visible only to their authenticated uploader and
admins; after approval they are available to authenticated library readers. Every
EPUB, cover, extracted-folder and local-download path loaded from mutable metadata
is resolved through symlinks and confined to its configured storage root, including
component-aware rejection of traversal and sibling-prefix paths.

Local `start.bat` launches web and packager as separate child processes. Production
service setup remains inventory-gated; see `docs/ASYNC_PACKAGER.md` and the systemd
template under `deploy/systemd/`.

All limits are explicit in `.env.example` and `.env.local.example`. Reproduce the
whole-archive versus streaming memory/time comparison with:

```text
PYTHONPATH=. python scripts/benchmark_epub_io.py --payload-mib 16 --repetitions 5
```

See `docs/PERFORMANCE_BASELINE.md` for the recorded p50/p95/p99 result and its local,
non-production scope.

## Persistent library index

Library, novel lookup, tags/authors and reader chapter/image discovery use a
separate rebuildable SQLite index. Normal requests do not rebuild metadata or walk
chapter storage. Local bootstrap publishes an atomic candidate automatically;
non-local startup verifies only and fails closed if the index is missing.

Explicit build/verification:

```bash
PYTHONPATH=. python scripts/reindex_library.py
PYTHONPATH=. python scripts/reindex_library.py --verify-only
```

The index is not schema-v3 user state and is not a replacement for state backup.
See `docs/LIBRARY_INDEX.md` for production rollout and rollback.

## Local data

Development data stays outside Git:

```text
data/
├── arcdb.sqlite3
├── library_index.sqlite3
├── migration-backups/
├── sqlite-backups/
├── metadata/
├── output/
├── structured_output/
├── batched_epubs/
├── telegram/
├── tmp/
└── dev-seed-report.json
```

When the historical baseline is explicitly materialized for provenance or source
reconciliation, its reconstructed tree and temporary ZIP live under ignored
`.runtime/`. Normal startup does not create or execute this tree. Large EPUB/ZIP
fixtures in `dev-fixtures/inbox/` are also ignored.

Telegram is a separate process and is disabled locally by default
(`ARCHIVEDB_START_TELEGRAM=0`). Web imports never create a Telethon client. See
`docs/TELEGRAM_SERVICE.md` for the private environment split and opt-in procedure.
SMTP is optional. The auth CI uses only fixture accounts plus a local-only suppressed
email sink and derives fixture codes from stored hashes without printing passwords
or codes.

## Current production architecture (known so far)

```text
Browser
  -> Cloudflare edge / likely Tunnel
  -> Oracle Cloud Infrastructure Ampere instance
     - ARM
     - 4 OCPU
     - 24 GB RAM
     - OCI Block Volume
  -> Python / Flask
  -> local files + JSON/CSV metadata + Telethon
```

Production credentials, Telegram sessions, user databases, EPUBs, extracted chapters and other runtime data must never be committed.

The web process exposes payload-free `GET /healthz` and `GET /readyz` endpoints.
Normal request logs use bounded Flask route templates and include request ID, method,
status and duration without email/IP/payload data. See `docs/OBSERVABILITY.md` for
the exact contract and the p50/p95/p99 summarizer.

All state-changing HTTP methods require an allowed browser origin before route
dispatch, and logout is POST-only. Production must set the exact public
`ARCHIVEDB_ALLOWED_ORIGINS`; see `docs/SECURITY.md`.

Reader chapter responses are reconstructed by a parser-based explicit
tag/attribute/URL allowlist. Executable and embedded markup, event handlers, inline
styles and dangerous URL schemes are removed; malformed active markup fails closed.
See `docs/SECURITY.md` for the compatibility and verification contract.

Executable browser code is protected by a per-response nonce: CSP no longer allows
`unsafe-inline` scripts or the previous CDN fallback and blocks script attributes.
The shared auth CSS is a local fingerprinted immutable asset; the later static split
will remove the remaining inline-style exception without changing the design.

## Repository layout

```text
AGENTS.md                       AI/contributor rules and handoff entrypoint
arcdb/storage/                  SQLite/storage migration foundation
arcdb/app.py                    tracked Flask runtime entrypoint
arcdb/templates/                tracked runtime templates
baseline/                       retained verified historical source archive
dev-fixtures/seed-manifest.json reproducible local library fixture mapping
dev-fixtures/inbox/             ignored location for Downloads.zip / EPUBs
seed-dev.bat                    explicit Windows local-data rebuild launcher
scripts/materialize_baseline.py verifies + extracts the retained historical baseline
scripts/dev_bootstrap.py        local environment/dependency/bootstrap launcher
scripts/dev_seed.py             local-only login seed
scripts/dev_seed_library.py     EPUB scanner + local library/state seeder
scripts/migrate_state_to_sqlite.py safe legacy-state -> SQLite migration
scripts/create_sqlite_backup.py WAL-aware verified operational backup
scripts/verify_sqlite_backup.py independent backup + restore verification
scripts/restore_sqlite_backup.py verified restore to a new target only
scripts/oracle_inventory.sh     read-only production inventory helper
scripts/collect_production_inventory.py structured private/path-free inventory reports
scripts/reconcile_production_inventory.py live source vs historical-layout diff
tests/                          bootstrap/seed/storage safety coverage
docs/                           architecture, data, safety, roadmap and decisions
```

The repository runtime is now tracked directly in `arcdb/app.py` and
`arcdb/templates/`. The baseline bundle and overlay script remain only as historical
provenance/reconciliation tooling and are not used by `start.bat` or runtime CI to
launch the application.

## Production caution

Do not deploy this repository over Oracle simply because local CI is green. Before production deployment, reconcile this baseline with the live instance (or a sanitized source snapshot), preserve production paths/data, create verified backups and follow the documented rollback-capable migration protocol.
