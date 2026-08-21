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
└── tmp/
```

The reconstructed source and temporary baseline ZIP live under ignored `.runtime/`.

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
scripts/materialize_baseline.py verifies + extracts it to `.runtime/source/`
scripts/dev_bootstrap.py        local environment/dependency/bootstrap launcher
scripts/dev_seed.py             local-only account seed
scripts/oracle_inventory.sh     read-only production inventory helper
docs/ARCHITECTURE.md            architecture notes
```

The baseline bundle is temporary plumbing for the initial archive import. As the application is refactored, source files will move into a normal directly tracked package layout.

## Production caution

Do not deploy this branch to Oracle simply by replacing the live directory. Before production deployment we will compare this baseline with the live instance (or a sanitized source snapshot supplied by the owner), preserve production paths/data, create a backup and only then build the deployment/rollback flow.
