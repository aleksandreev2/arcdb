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
trade-off: memory is bounded by entry/chunk limits instead of archive size. Phase 6
now executes this measured implementation in the separate packager process; the
numbers remain a function-level baseline, not an end-to-end queue latency benchmark.

The benchmark creates only temporary data and emits path-free JSON. Payload size and
repetition count are configurable; use the same command and inputs for later
comparisons.

## Async queue admission — 2026-08-22

Command:

```text
python scripts/benchmark_job_queue.py --repetitions 200
```

On the same Windows/Python/CPU environment, one WAL enqueue plus an owner status read
measured p50 7.912 ms, p95 22.664 ms and p99 23.550 ms. The previous 16 MiB bounded
package function baseline above measured p50 538.299 ms and ran inside finalize.
These are different scopes, so the comparison proves request-path separation rather
than end-to-end speedup: the HTTP path now persists control-plane work and returns,
while EPUB CPU/I/O continues asynchronously in the worker. Production queue wait,
Block Volume latency and total completion time remain unmeasured.

## Persistent library query — 2026-08-22

Command:

```text
python -m scripts.benchmark_library_index --items 10000 --repetitions 200
```

On the same local Windows/Python/CPU environment, a synthetic 10,000-title atomic
index rebuild took 14,193.523 ms. A warmed search + tag filter + views sort + first
page query matched 100 titles and measured p50 9.685 ms, p95 12.587 ms and p99
14.640 ms with FTS5 trigram enabled.

The benchmark uses only generated metadata and a temporary database; it excludes
real chapter filesystem scans, HTTP/auth overhead and OCI Block Volume behavior.
Its purpose is a reproducible indexed-query regression baseline. The architectural
improvement is that rebuild/scanning is now explicit and outside the request path,
not a claim that these local numbers predict production latency.
