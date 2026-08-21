# ArchiveDB

ArchiveDB web application. This repository currently uses the latest source archive available to us as the development baseline; the Oracle instance may contain newer production-only changes and will be reconciled later without overwriting server data blindly.

## Windows: one-click local start

Requirements: **Python 3.11+** and Git for Windows.

```bat
git clone https://github.com/aleksandreev2/arcdb.git
cd arcdb
start.bat
```

`start.bat` delegates setup to `scripts/dev_bootstrap.py`. On the first run it automatically:

1. creates `.venv`;
2. installs packages from `requirements.txt`;
3. creates a local `.env` from `.env.local.example`;
4. generates a random local Flask secret;
5. creates the local `data/` tree;
6. verifies and reconstructs the checked-in development baseline into `.runtime/source/`;
7. creates a local development login;
8. starts ArchiveDB at `http://127.0.0.1:5004/login` and opens it in the browser.

Default local login:

```text
dev@arcdb.local
arcdb-dev-123
```

The local account exists only inside ignored `data/` files. Change `LOCAL_DEV_EMAIL` / `LOCAL_DEV_PASSWORD` in `.env` if desired.

### Seed a populated development library

Library/data seeding is **never run by `start.bat`**. It only happens when you explicitly run:

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

The supplied fixture set currently seeds:

- RAW EPUBs into `data/batched_epubs/`;
- a RAW + translated pair for `S급 헌터들의 가이드가 되었다` / `Я стал куратором охотников S-ранга`;
- `Регрессор Академии яндере` as a large translated-reader fixture;
- `Мои сексуальные университетские подружки` as another translated-reader fixture;
- RAW-only Korean novels for filtering/download testing;
- local metadata, users, reading progress, collections and community chat data;
- `data/dev-seed-report.json` with EPUB metadata, hashes, counts, validation issues and unused alternate EPUBs.

PDF and `.file` entries in a fixture ZIP are ignored. Alternative EPUB versions that are not selected by `dev-fixtures/seed-manifest.json` remain visible in the seed report as test cases but are not added as duplicate library cards.

For safety the seeder refuses to run unless all of these are true:

- `ARCHIVEDB_LOCAL_DEV=1`;
- `HOST` is loopback-only;
- every writable ArchiveDB data path resolves inside this checkout's `data/` directory.

Before replacing an existing seed it backs up the small JSON/CSV state into `.dev-backups/` and keeps the three most recent backups. Large derived EPUB/chapter folders are regenerated instead of duplicated in backups.

### Subsequent updates

Double-click:

```text
update-and-start.bat
```

It performs `git pull --ff-only` and then launches the same bootstrap. Python packages are reinstalled **only when `requirements.txt` changes**.

## Local data

Development data stays outside Git:

```text
data/
├── metadata/
├── output/
├── structured_output/
├── batched_epubs/
├── telegram/
├── tmp/
└── dev-seed-report.json
```

The reconstructed source and temporary baseline ZIP live under ignored `.runtime/`. Large EPUB/ZIP fixtures in `dev-fixtures/inbox/` are also ignored.

Telegram is disabled locally by default (`ARCHIVEDB_NO_TELEGRAM=1`). SMTP is optional; when SMTP credentials are absent, verification codes are printed to the terminal.

## Current production architecture (known so far)

```text
Browser
  -> Cloudflare Worker / Cloudflare Tunnel
  -> Oracle Cloud Infrastructure Ampere instance
     - ARM
     - 4 OCPU
     - 24 GB RAM
     - OCI Block Volume
  -> Python / Flask
  -> local files + JSON/CSV metadata + Telethon
```

Production credentials, Telegram sessions, user databases, EPUBs, extracted chapters and other runtime data must never be committed.

## Repository layout

```text
baseline/                       temporary checked-in compressed source baseline
dev-fixtures/seed-manifest.json reproducible local library fixture mapping
dev-fixtures/inbox/             ignored location for Downloads.zip / EPUBs
seed-dev.bat                    explicit Windows local-data seed launcher
scripts/materialize_baseline.py verifies + extracts baseline to `.runtime/source/`
scripts/dev_bootstrap.py        local environment/dependency/bootstrap launcher
scripts/dev_seed.py             local-only login seed
scripts/dev_seed_library.py     EPUB scanner + local library/state seeder
scripts/oracle_inventory.sh     read-only production inventory helper
tests/make_seed_fixtures.py     tiny generated EPUB fixtures used by CI
docs/ARCHITECTURE.md            architecture notes
```

The baseline bundle is temporary plumbing for the initial archive import. As the application is refactored, source files will move into a normal directly tracked package layout.

## Production caution

Do not deploy this branch to Oracle simply by replacing the live directory. Before production deployment we will compare this baseline with the live instance (or a sanitized source snapshot supplied by the owner), preserve production paths/data, create a backup and only then build the deployment/rollback flow.
