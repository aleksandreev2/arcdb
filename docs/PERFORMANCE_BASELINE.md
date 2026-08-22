# ArchiveDB performance baselines

## Purpose

Record reproducible measurements before and after request-path performance changes.
Local results establish code-level regressions and memory shape; they are not OCI
capacity claims and do not replace production measurements.

The web runtime emits payload-free `[REQUEST]` events with route template, method,
status and duration. For a bounded log, `scripts/summarize_request_timings.py`
produces per-route p50/p95/p99 JSON without request IDs or the input path. No
production request baseline has been collected yet.

## Seeded authenticated HTTP workload — 2026-08-22

Command:

```text
set ARCHIVEDB_BENCHMARK_PASSWORD=<local fixture password>
python scripts/benchmark_http_routes.py --warmups 3 --repetitions 20 \
  --profile local-legacy --output new-http-performance.json
```

Environment: the same Windows/Python/CPU host documented below, the seven-title
generated seed library, one local Flask development process, legacy state reads and
20 measured requests after three warmups for every scenario. The loopback-only
runner reads each response completely and aggregates client-observed time plus
bounded `Server-Timing` components.

| Scenario | HTTP p50 | p95 | p99 | measured component p50 |
|---|---:|---:|---:|---:|
| library page | 3.256 ms | 27.481 ms | 27.928 ms | SQLite 0.861 ms |
| library search | 3.829 ms | 28.033 ms | 28.235 ms | SQLite 1.090 ms |
| novel detail | 14.441 ms | 21.486 ms | 27.351 ms | SQLite 0.732 ms |
| reader page | 4.906 ms | 25.573 ms | 26.283 ms | SQLite 2.236 ms |
| reader chapter | 3.146 ms | 26.837 ms | 26.851 ms | SQLite 0.705 ms; filesystem 0.110 ms; EPUB 0.056 ms |
| collections | 2.057 ms | 25.155 ms | 25.773 ms | not instrumented |
| community overview | 2.373 ms | 24.614 ms | 26.984 ms | filesystem 0.236 ms |

The similar approximately 25–28 ms local tail across unrelated routes is outside
the small measured component totals and is not evidence of SQLite or filesystem
contention. It can include the Windows scheduler, development server and local
security software. This fixture proves workload/report wiring and provides a
repeatable code-regression baseline; it is too small to predict real library scale,
concurrency, Gunicorn or OCI Block Volume behavior.

The report contains scenario names, route templates, methods/statuses, counts and
percentiles only. It excludes the base URL, credentials, concrete novel/chapter
identifiers, request IDs and response payloads. CI repeats the same workload with
three samples per scenario as a functional contract check, not a capacity gate.

## Upload sequential-write/fsync baseline — 2026-08-22

Command:

```text
python scripts/benchmark_upload_io.py --payload-mib 16 --repetitions 5
```

The benchmark reproduces the removed flush+fsync-per-1-MiB-chunk path and compares
it with the current sibling-temp sequential copy, one final flush/fsync and atomic
publication. On the same local host:

| Implementation | fsync calls/copy | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| legacy per-chunk sync | 16 | 32.367 ms | 44.532 ms | 46.905 ms |
| current atomic single sync | 1 | 22.232 ms | 31.238 ms | 31.266 ms |

Both paths use generated temporary data and enforce the same size bound. The result
confirms the request-path I/O shape and a local latency reduction; it is not an OCI
Block Volume durability or throughput claim.

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
