# Health, readiness and request timing

## Status boundary

Implemented and covered by local/CI runtime checks:

- `GET /healthz` proves only that the Flask process can answer HTTP;
- `GET /readyz` performs bounded, read-only checks of the persistent library index
  and the configured state-read backend;
- every non-static response receives a random `X-Request-ID`;
- request events contain only `request_id`, Flask route template, method, status and
  `duration_ms`, plus present-only bounded `sqlite_ms`, `filesystem_ms`, `epub_ms`
  and `job_ms` component totals;
- instrumented responses expose the same bounded totals through `Server-Timing` so
  the loopback benchmark does not need private process-log access.

This is repository/runtime capability. It is not evidence that production probes,
log retention or alerts are configured. Those remain unknown until production
inventory is available.

## Endpoint contract

Successful responses are deliberately small and path-free:

```json
{"status": "ok"}
```

```json
{"status": "ready"}
```

`/readyz` returns HTTP 503 with only `{"status":"not_ready"}` when the library
index is missing/incompatible/inconsistent, the active SQLite state backend is
missing/incompatible, or the state backend setting is invalid. It never returns
filesystem paths, schema details, users, counts, tokens or exception text.

The readiness check does not run full SQLite integrity scans on every probe. Full
library-index verification remains `scripts/reindex_library.py --verify-only`; full
state parity/integrity remains `scripts/verify_read_cutover_readiness.py`.

## Request timing events

Example:

```text
[REQUEST] request_id=32-lowercase-hex route=/api/library method=POST status=200 duration_ms=8.421 sqlite_ms=2.134
```

The route is the bounded Flask route template, not the request URL or query string.
Normal request events do not include IP addresses, email addresses, payloads,
cookies, authorization values or user agents. Static and reader-asset events are
omitted to avoid high-volume noise; responses still receive `X-Request-ID`.

Component totals are additive scopes, not a full allocation of request time:

- `sqlite` covers persistent library-index queries used by library, novel and reader
  discovery;
- `filesystem` covers instrumented community reads, upload copies and chapter reads;
- `epub` covers upload archive validation/extraction and chapter sanitization;
- `job` covers package-queue admission/status/cancellation operations.

Uninstrumented framework, authentication, serialization and state-adapter time
remains visible only in total `duration_ms`. Component names are fixed and neither
logs nor `Server-Timing` include paths, identities, query strings or payload labels.

For a bounded process log, create a sanitized percentile report:

```text
python scripts/summarize_request_timings.py \
  --log private-process.log \
  --output new-request-timings.json
```

The report groups only by route template, method and status and emits sample count
plus p50/p95/p99 total and present component durations. It excludes request IDs and
the input log path and refuses to overwrite an existing report.

The deterministic loopback workload uses the seeded local library and measures the
actual authenticated HTTP paths without storing credentials in its report:

```text
set ARCHIVEDB_BENCHMARK_PASSWORD=<local fixture password>
python scripts/benchmark_http_routes.py --warmups 3 --repetitions 20 \
  --profile local-legacy --output new-http-performance.json
```

The runner refuses non-loopback URLs. `--profile` is only a bounded operator label;
run separate controlled processes/configurations for comparisons rather than
claiming a backend or deployment that the runner cannot independently prove.

## Production use

Configure liveness and readiness probes separately. Do not restart a live process
solely because `/readyz` reports a dependency problem without first preserving logs
and checking the rollback/runbook. Keep raw process logs private because other
explicit audit events may contain operational identities even though request timing
events do not.
