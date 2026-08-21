# Local dev EPUB fixtures

Large EPUB/ZIP fixtures are intentionally not stored in Git.

## Easiest workflow

Put the supplied `Downloads.zip` here:

```text
dev-fixtures/inbox/Downloads.zip
```

Then double-click `seed-dev.bat` from the repository root.

You can also:

- put `Downloads.zip` in the repository root;
- drag a ZIP, EPUB, or folder onto `seed-dev.bat`;
- run `seed-dev.bat "C:\path\to\Downloads.zip"`.

The seeder ignores non-EPUB files, scans EPUB metadata, rebuilds only local `data/`, and writes `data/dev-seed-report.json`.

The mapping between fixture files and local library records is defined in `seed-manifest.json`. Re-running the seed is deterministic.

Production paths are rejected: the seeder only runs with `ARCHIVEDB_LOCAL_DEV=1`, loopback `HOST`, and data paths inside this checkout's `data/` directory.
