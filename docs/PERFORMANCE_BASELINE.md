# ArchiveDB performance baselines

## Purpose

Record reproducible measurements before and after request-path performance changes.
Local results establish code-level regressions and memory shape; they are not OCI
capacity claims and do not replace production measurements.

## EPUB packaging I/O — 2026-08-22

Command:

```text
PYTHONPATH=. python scripts/benchmark_epub_io.py --payload-mib 16 --repetitions 5
```

Environment:

- Python 3.13.14;
- Windows 10 Pro;
- Intel Core i5-12400;
- one valid EPUB containing a 16 MiB incompressible binary entry;
- five repetitions per implementation;
- Python allocations measured with `tracemalloc`.

The legacy comparison function reproduces the removed `all_entries = {name:
archive.read(name) ...}` pattern. The streaming implementation performs the current
structure/CRC/size validation and copies non-text entries in 1 MiB chunks.

| Implementation | duration p50 | p95 | p99 | Python peak p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|---:|
| legacy whole-archive materialization | 462.789 ms | 490.002 ms | 494.814 ms | 61.610 MiB | 61.640 MiB | 61.646 MiB |
| bounded streaming + full verification | 538.299 ms | 561.974 ms | 566.182 ms | 4.313 MiB | 4.314 MiB | 4.314 MiB |

The measured p50 Python peak decreased by approximately 93%. The local p50 runtime
increased by approximately 16% because the new path deliberately performs full
archive validation/CRC reads in addition to packaging. This is an accepted safety
trade-off: request memory is bounded by entry/chunk limits instead of archive size.
Moving this remaining CPU/I/O work out of Flask requests is Phase 6.

The benchmark creates only temporary data and emits path-free JSON. Payload size and
repetition count are configurable; use the same command and inputs for later
comparisons.
