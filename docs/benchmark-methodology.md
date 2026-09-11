# Benchmark Methodology

This documents what `python -m benchmarks.run` (`benchmarks/run.py`) actually
does. It describes implemented behavior, not a plan - and it stays that way
if the code changes.

## What is compared

Two real, loopback Streamable HTTP paths, run in the same process against
the same real upstream fixture server (`examples/upstream_server.py`):

```text
Direct:  client -> upstream
Gateway: client -> SentinelMCP -> upstream
```

Both servers run on ephemeral, OS-assigned ports on `127.0.0.1`, started and
torn down by the benchmark script itself - there is no separate manual setup
step.

## Benchmark tools

- `benchmark.noop` - returns immediately. Approximates transport/session/
  gateway overhead with upstream work contributing effectively nothing.
- `benchmark.fixed_latency` - sleeps for a fixed, known duration
  (`FIXED_LATENCY_DURATION_SECONDS`, currently 10ms) before returning.
  Approximates gateway behavior when upstream work is a real, known
  contributor to total latency.

## Connection and session behavior

Both paths use the same shape: `concurrency` persistent client connections
("workers"), each opening exactly one MCP session (`mode="legacy"`,
`initialize` handshake) and then issuing a fixed number of sequential
`tools/call` requests over that same session before disconnecting. Only the
`tools/call` round trips are timed - not `initialize` or teardown. The
gateway path is identical except each connection carries a valid
`Authorization: Bearer` header. This keeps the comparison to "what does the
gateway itself add," not "who reconnects more."

## Concurrency levels, trials, warm-up

- Concurrency levels: 1, 10, 25, 50, 100 (`CONCURRENCY_LEVELS`). 250/500 are
  intentionally not included in v1 - CLAUDE.md only calls for them "if the
  machine remains stable and the measurements remain meaningful," and the
  initial baseline at 100 was not evidence that pushing further would be.
- Each (path, tool, concurrency) configuration runs one **discarded**
  warm-up trial, then `TRIALS_PER_CONFIGURATION` (5) **measured** trials.
- Each trial is a **fixed request count**, not a fixed duration: `concurrency`
  workers each make a fixed number of sequential calls
  (`NOOP_REQUESTS_PER_CLIENT` = 20 for `benchmark.noop`,
  `FIXED_LATENCY_REQUESTS_PER_CLIENT` = 5 for `benchmark.fixed_latency`, kept
  low since it has a real fixed cost per call). Total requests per trial =
  `concurrency * requests_per_client`.

## Metrics recorded per trial

- Request count, wall-clock seconds, throughput (requests/second).
- p50 / p95 / p99 latency (linear-interpolation percentile, computed from
  every request's own client-observed round-trip time).
- Mean latency.
- Error rate and timeout rate (a `TimeoutError` while awaiting the call is
  counted as both an error and a timeout; any other exception is an error).

## Aggregation across trials

The 5 trials per configuration are **not** averaged together directly.
For each metric, the script reports:

- The **median** across the 5 trials' own values for that metric.
- The **variability**: `max - min` across the 5 trials' values for that
  metric.

Both are in the raw JSON output (`median` and `variability` per
configuration) and the median is what the console summary and the gateway-
overhead table use.

## Gateway overhead

For each (tool, concurrency) pair present in both the direct and gateway
results, the script reports:

- `absolute_overhead_ms` = gateway median p50 - direct median p50.
- `percentage_overhead` = that difference as a percentage of the direct
  median p50.

This is a **p50-to-p50** comparison specifically - it does not claim
anything about the tail (p95/p99) overhead, which is reported alongside but
not differenced.

## Policy-evaluation latency, measured separately

The gateway's own audit log (`sentinelmcp/telemetry/audit.py`, always on -
see Phase 5) already records `policy_latency_ms` (the combined time for
upstream-`inputSchema` validation plus SentinelMCP argument-constraint
evaluation) on every `tools/call` attempt. After a benchmark run, the script
reads that log and reports p50/p95/p99/mean policy-evaluation latency
**separately** from the total gateway p50/overhead numbers above - it is not
presented as, or subtracted from, total gateway overhead. This uses the
exact same instrumentation the gateway always runs with in production, not
a separate measurement pass.

## Denied-request latency

A separate, single-configuration benchmark (10 concurrent connections, 10
requests each) calls a tool the benchmark principal is not authorized for
(`database.execute_write`, no policy rule at all). This has no equivalent on
the direct path (there is no authorization layer to deny anything), so it is
reported on its own, not as a row in the direct-vs-gateway table.

## Rate and concurrency limits during the benchmark

The gateway is configured with an effectively unlimited token bucket
(capacity and refill rate of 1,000,000) and concurrency ceiling (10,000).
This benchmark measures the gateway's *baseline* overhead - session
handling, authorization, schema/policy evaluation, audit logging - not
Phase 4's deliberate throttling behavior, which has its own dedicated tests
(`tests/integration/test_phase4_limits.py`).

## Logging during the benchmark

Constructing the SDK's server objects installs an INFO-level root logging
handler that would otherwise emit one line per HTTP request/response. That
volume of synchronous logging is itself slow enough to dominate the very
latencies being measured, so the script forces the root and `httpx2`/`mcp`/
`uvicorn` loggers to `WARNING` immediately after constructing both server
apps, before any timed trial runs.

## Environment and versions recorded

Every run's JSON output includes: wall-clock timestamp, `platform.platform()`,
processor, CPU count, Python version and implementation, and the installed
`mcp`, `uvicorn`, `httpx2`, and `jsonschema` package versions.

## Raw output

Every run writes the complete report (environment, every trial's full
`TrialStats`, both aggregation fields, the overhead table, and the policy-
latency summary) as machine-readable JSON to
`benchmarks/results/<timestamp>.json`. This is the source of truth; the
console output is a human-readable summary of the same numbers.

## Known limitations

- This is a single-machine, loopback benchmark. It says nothing about
  network-attached deployment, TLS termination overhead, or multi-process/
  multi-replica behavior (out of scope for v1 - see the threat model).
- The gateway's per-call upstream `tools/list` round trip - previously
  re-fetched on every single `tools/call` - was identified as a leading
  overhead suspect from the original baseline below, then measured,
  cached (`UpstreamToolCache`, see `docs/architecture.md`), and
  re-measured. See the before/after note at the end of this document for
  the actual result; do not assume caching improved anything without
  reading that measured comparison.
- Results depend on the host machine and will vary run to run and machine to
  machine; only the JSON output from an actual run on a specific machine
  should be cited as a number, never a number from this document.
- Measurements were taken in a sandboxed development VM, not dedicated
  benchmarking hardware; absolute numbers should be read as illustrative of
  relative (direct vs. gateway) behavior, not as production capacity
  planning figures.

## Tool-list caching: measured before/after

Two real runs on the same machine, same methodology, same code except for
the presence of `UpstreamToolCache` (see `docs/architecture.md`'s
"Tool-list caching" section for the change itself):

- Before: `benchmarks/results/2026-09-08T17-45-38.212871+00-00.json`
- After: `benchmarks/results/2026-09-10T19-40-16.462078+00-00.json`

Gateway p50 dropped by roughly 25-35% at every concurrency level tested,
for both `benchmark.noop` and `benchmark.fixed_latency` - see the README's
"Measured effect of caching the upstream's `tools/list` result" section for
the full table. Policy-evaluation latency (from the audit log,
independent of this change) stayed within measurement noise of its
pre-change value (~0.9ms p50 both runs) - consistent with the improvement
coming specifically from removing a redundant network round trip, not from
an unrelated change to policy evaluation itself. This does not eliminate
gateway overhead; it removed one identified, measured contributor to it.
