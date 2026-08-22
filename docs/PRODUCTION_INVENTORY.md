# ArchiveDB production inventory and reconciliation

## Status and purpose

No production SSH session or sanitized live snapshot is present in this repository. Therefore this document does not claim any discovered production paths, source revision, service configuration or cutover result.

The repository provides a read-only procedure that an operator can run once production access is available:

1. discover candidate paths and service files on the host;
2. collect a structured private inventory from operator-confirmed explicit paths;
3. create a separate path-free report;
4. reconcile the private source inventory against the materialized repository baseline;
5. review every live-only, changed or missing source file and every unknown metadata file before readiness preflight.

Inventory and reconciliation never authorize shadow observation, an SQLite canary or primary reads.

## Report ownership

Keep raw discovery output, the private structured inventory and the private reconciliation diff on protected operator storage. Do not attach them to a public PR or commit them. They contain exact production paths, systemd unit paths/users and relative source or metadata filenames. They do not contain file payloads, environment values, process command lines, Cloudflare credential values or secrets.

The separate sanitized reports contain aggregate counts, sizes, hashes, storage/runtime coverage and decision flags. They omit:

- filesystem paths;
- service user/group identities;
- source and metadata filenames;
- user identities and state payloads;
- environment values;
- Cloudflare tunnel IDs, hostnames, credentials and tokens;
- Unix socket paths.

Both private and sanitized writers refuse to overwrite an existing output file.

## Step 1 — host discovery

Run from a trusted checkout or copy only the reviewed script to the host. Start without a filesystem root if the application path is unknown:

```bash
bash scripts/oracle_inventory.sh > /new/private/path/oracle-inventory.txt
```

This inspects process executable/working directories, relevant systemd unit metadata, mounts, disks, listening ports and Cloudflared config filenames. It does not print process command lines, environment values or config contents.

After the operator identifies approved storage roots, repeat with those exact roots:

```bash
bash scripts/oracle_inventory.sh \
  --search-root /operator/confirmed/root-a \
  --search-root /operator/confirmed/root-b \
  > /new/private/path/oracle-inventory-with-roots.txt
```

The script has no guessed `/home/ubuntu` default. Every filesystem search root must be supplied explicitly and must be absolute.

From the private output and relevant protected unit/environment files, record:

- live application source root and revision, if available;
- metadata root and SQLite path, including WAL/SHM sidecar presence;
- EPUB, unpacked chapter, upload, output and packaging roots;
- ArchiveDB web/packager/Telegram unit files actually present;
- Gunicorn unit file and Cloudflared config path;
- mount owning each persistent path.

Do not copy secret values into a report or command line.

## Step 2 — structured private inventory

Run with only operator-confirmed explicit paths. The labels below are examples; omit nonexistent roots and use labels that describe the real deployment:

```bash
PYTHONPATH=. python3 scripts/collect_production_inventory.py \
  --app-root /explicit/live/application/source \
  --meta-dir /explicit/live/metadata \
  --sqlite-db /explicit/live/arcdb.sqlite3 \
  --metadata-file translated_csv=/explicit/live/uploaded_novels_tracker.csv \
  --metadata-file raw_master_csv=/explicit/live/master_library_index.csv \
  --content-root epubs=/explicit/live/epubs \
  --content-root chapters=/explicit/live/structured-output \
  --content-root packaging=/explicit/live/packaging-work \
  --systemd-unit /explicit/systemd/arcdb-web.service \
  --systemd-unit /explicit/systemd/cloudflared.service \
  --cloudflared-config /explicit/cloudflared/config.yml \
  --private-report /new/private/path/production-inventory-private.json \
  --report /new/private/path/production-inventory-report.json
```

If production source is not a Git worktree, add the deployment revision recorded by the operator with `--source-revision DEPLOYMENT_REVISION`.

Approved content labels are `epubs`, `chapters`, `uploads`, `covers`, `output`, `packaging`, `temp`, `cache`, `library`, `structured_output` and `batched_epubs`. Restricting labels prevents an operator-supplied path or identity from becoming a key in the sanitized report.

Use `--metadata-file` for configured state/index files outside `--meta-dir`. Approved labels are `users`, `user_data`, `collections`, `user_uploads`, `custom_meta`, `allowlist`, `community`, `translated_csv` and `raw_master_csv`. Explicit files are deduplicated when they already reside under the metadata root.

The source inventory hashes files but excludes common runtime/data/secret artifacts such as `.env`, logs, SQLite files, sessions, EPUB/ZIP payloads, `data/`, `.runtime/`, `.venv/` and `.git/`. Metadata files are hashed and classified without reading their payload into either report. Content roots are summarized by count, total size and file type; content filenames are not collected.

The SQLite record is only an inventory fingerprint. If WAL/SHM sidecars are present, do not interpret the main-file hash as a consistent database backup. The later readiness preflight opens SQLite read-only and performs the actual database checks.

## Step 3 — source reconciliation

On a trusted engineering machine at the exact repository commit being evaluated:

```bash
python3 scripts/materialize_baseline.py --force
```

Transfer the private inventory through protected operator storage, then compare the
production source with the behavior-compatible historical layout:

```bash
PYTHONPATH=. python3 scripts/reconcile_production_inventory.py \
  --inventory /private/path/production-inventory-private.json \
  --reference-root .runtime/source \
  --private-report /new/private/path/production-reconciliation-private.json \
  --report /new/private/path/production-reconciliation-report.json
```

This explicit materialization is a reconciliation/provenance operation only. The
repository application now runs directly from `arcdb/app.py` and
`arcdb/templates/`; it does not launch `.runtime/source`. The tracked source was
imported directly from the behavior-compatible materialized runtime, with trailing
whitespace normalized only. For later commits, separately review the Git changes from
that import point as the proposed deployment delta. Do not mistake the historical
reference tree for the current runtime build path.

The private reconciliation lists exact relative source filenames that are missing from production, changed in production or unknown in production. It also lists metadata filenames outside the repository's known legacy inventory. The sanitized report exposes counts only.

Review every difference. A live-only file may be required production behavior; a missing file may reflect a different deployment layout; an unknown metadata file may contain state outside SQLite schema v3. Never deploy the archived baseline over any unexplained difference.

## Step 4 — readiness preflight

Only after private reconciliation is complete and every difference is understood, use the exact discovered metadata and SQLite paths:

```bash
PYTHONPATH=. python3 scripts/verify_read_cutover_readiness.py \
  --meta-dir /explicit/live/metadata \
  --db /explicit/live/arcdb.sqlite3 \
  --report /new/private/path/readiness-report.json
```

Follow `docs/PRODUCTION_SAFETY.md` for the required quiet window, unknown-file review, bounded shadow observation and rollback gates. Local/CI inventory fixtures are repository-tooling evidence only and are not production evidence.

## Required review checklist

- exact application root recorded privately;
- production revision known or explicitly recorded as unavailable;
- source diff reviewed with no unexplained changed, missing or live-only files;
- exact metadata and SQLite paths recorded;
- every unknown metadata file classified and retained;
- EPUB/chapter/upload/package roots recorded without moving or deleting content;
- actual systemd units and Gunicorn settings recorded;
- actual Cloudflared config inspected without exporting secrets;
- owning mount/filesystem for SQLite and content roots recorded;
- reports stored outside Git with restrictive access;
- readiness/canary/primary-read authorization remains false until the later operational gates pass.
