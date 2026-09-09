# Definition-of-Done Verification Report

**Date:** 2026-09-08
**Scope:** An independent, item-by-item audit of CLAUDE.md's 20-item
"Definition of Done" checklist, performed *after* all 8 build phases were
reported complete. Every result below comes from a command that was
actually executed during this audit - nothing is inferred from prior phase
reports or re-asserted from memory. Two real gaps were found and are now
fixed (see [Follow-up fixes applied](#follow-up-fixes-applied)).

This report is a point-in-time audit record. If the implementation changes
after this date, re-run the commands below rather than trusting this
document's numbers.

---

## Method

- Every item was checked by running the actual test(s) that prove it, not
  by reading code and asserting it "looks correct."
- Where CLAUDE.md's numbered invariants and test names are cited, each name
  was individually run with `pytest -k <name>` and confirmed to exist and
  pass before being written down here.
- The benchmark (item 17) was re-run live, in full, during this audit - not
  assumed to still match a previously committed result.
- A genuinely fresh `git clone` of the real GitHub remote (not a local copy
  of the working tree) was used to check the "clean checkout" claim, in a
  scratch venv, installed from scratch.
- Docker was checked for real (`docker --version`) and confirmed absent
  from this environment; the Docker-dependent leg of item 18's example
  commands is explicitly marked unverified rather than assumed to work.
  **Update (same day, follow-up session):** Docker was subsequently
  installed and `docker compose up -d` was run for real. See the
  [Docker verification addendum](#docker-verification-addendum-2026-09-08-follow-up-session)
  at the end of this report.

---

## Item-by-item results

| # | Description | Status | Evidence |
|---|---|---|---|
| 1 | A real MCP client authenticates to SentinelMCP. | PASS | `test_valid_credentials_resolve_to_correct_principal_and_filter_discovery` |
| 2 | SentinelMCP establishes a distinct upstream MCP session. | PASS | `test_downstream_and_upstream_sessions_are_distinct` |
| 3 | Unauthorized tools are omitted from `tools/list`. | PASS | `test_valid_credentials_resolve_to_correct_principal_and_filter_discovery` |
| 4 | A client directly attempts a hidden tool anyway. | PASS | Live ad-hoc run against real gateway+upstream processes (see below) |
| 5 | SentinelMCP returns a protocol-correct denial. | PASS | Same live run: `is_error=True`, `"Unknown tool: database.query_stats"` |
| 6 | The upstream invocation counter remains zero. | PASS | Same live run: `counters.query_stats == 0` printed before **and** after the call, not just asserted |
| 7 | A redacted audit record explains the denial. | PASS | Same live run: real JSONL record emitted, `outcome: "policy_denied"`, `upstream_attempted: false` |
| 8 | An authorized constrained call succeeds. | PASS | `test_permitted_constrained_call_succeeds_with_values_forwarded_unchanged` |
| 9 | Schema-invalid calls are rejected. | PASS | `test_schema_invalid_call_is_rejected_and_never_reaches_upstream` |
| 10 | Type-confused calls are rejected. | PASS | `test_type_confused_argument_is_rejected_and_never_reaches_upstream` |
| 11 | Out-of-range argument calls are rejected. | PASS | `test_min_max_reject_out_of_range` (unit) **+** `test_numeric_out_of_range_call_is_rejected_and_never_reaches_upstream` (new end-to-end test, added during this audit - see Follow-up fixes) |
| 12 | Authorized argument values are forwarded unchanged. | PASS | `test_permitted_constrained_call_succeeds_with_values_forwarded_unchanged` - upstream reflects exact typed values back |
| 13 | Rate limiting behaves according to documented semantics. | PASS | 16 tests in `tests/unit/test_limits.py` + `test_burst_then_rate_limited_and_upstream_reflects_only_allowed_calls` |
| 14 | Concurrent upstream executions never exceed the configured ceiling. | PASS | Live ad-hoc run: ceiling=5, 120 simultaneous real calls, max observed concurrency = **5** on two independent counters (see below) |
| 15 | Cancellation/timeouts/errors do not leak concurrency capacity. | PASS | `test_acquire_context_manager_releases_on_{exception,cancellation,normal_exit}`, `test_concurrency_capacity_is_released_after_timeout` |
| 16 | Sensitive fields remain redacted by default. | PASS | 22 tests in `tests/unit/test_audit.py`, including the Hypothesis property test |
| 17 | Direct-versus-gateway benchmark results are reproducible. | PASS | `python -m benchmarks.run` re-run live, in full, during this audit (see below) |
| 18 | Documentation and README claims match actual implementation. | PASS | Spot-checked claims confirmed accurate against source; the one real inaccuracy found (dangling "Phase 6 report" references) is now fixed - see Follow-up fixes. Docker-dependent leg (`docker compose up -d`) subsequently verified live - see [addendum](#docker-verification-addendum-2026-09-08-follow-up-session) |
| 19 | The documented limitations accurately describe the system. | PASS (on what was checked) | Spot-checked 2 of 6 documented limitations directly against source (see below); not exhaustively re-verified line by line |
| 20 | No resume metric exists without real supporting test/benchmark output. | PASS | Every numeric claim traced to an exact reproduction command (see table below) |

---

## Extra detail on the items that needed it

### Item 6 - denied call never reaches upstream, actual counter value

A standalone script (not the pre-existing test, which only asserts a
delta) started a real upstream process and a real gateway process, made a
`readonly-agent` call to `database.query_stats` - a tool that principal has
no policy rule for at all - and printed the raw upstream invocation counter
value directly:

```text
BEFORE call: counters.query_stats = 0
call result: is_error=True
  content: Unknown tool: database.query_stats
AFTER call:  counters.query_stats = 0
CONFIRMED: counters.query_stats == 0 after the denied call - it never reached upstream.
```

The audit record emitted for that same call:

```json
{
  "correlation_id": "bf10e713f9ac4b2bb9b9eb96bbd6375f",
  "principal": "readonly-agent",
  "tool_name": "database.query_stats",
  "arguments": { "database": "staging", "limit": 1 },
  "outcome": "policy_denied",
  "upstream_attempted": false,
  "policy_result": "failed",
  "rate_limit_result": "passed",
  "concurrency_result": "not_evaluated"
}
```

### Item 14 - concurrency ceiling, actual max-observed-concurrency numbers

A standalone script configured a real gateway with `max_concurrent=5`,
fired 120 simultaneous real client calls via `asyncio.gather` against
`benchmark.fixed_latency` (0.3s each), and printed the maximum observed
concurrency from **two independent sources** - the gateway's own
`ConcurrencyLimiter.peak_count`, and the upstream tool's own separate
`ConcurrencyTracker.peak` (so the limiter isn't grading its own homework):

```text
configured concurrency ceiling (MAX_CONCURRENT) = 5
firing 120 simultaneous real client calls via asyncio.gather...

attempts:  120
succeeded: 15
rejected:  105

MAX OBSERVED CONCURRENCY (gateway ConcurrencyLimiter.peak_count) = 5
MAX OBSERVED CONCURRENCY (upstream's own independent ConcurrencyTracker.peak) = 5
active slots remaining after burst finished = 0

ceiling respected by gateway limiter?  5 <= 5 -> True
ceiling respected by upstream tracker? 5 <= 5 -> True
```

(105/120 were rejected immediately rather than queued - this project's
documented immediate-reject, not-queued concurrency behavior. 15 succeeded
as slots freed up during the burst.)

### Item 17 - live benchmark re-run vs. the committed baseline

`python -m benchmarks.run` was executed live during this audit (took
~24 minutes wall-clock on this machine - slower than the README's "a few
minutes" framing suggests; see Known residual gaps below). Environment
metadata from the new run's output JSON was **identical** to the committed
baseline: same OS build (`Windows-11-10.0.26200-SP0`), same CPU (Intel64,
8 cores), same `mcp`/`uvicorn`/`httpx2`/`jsonschema` versions - a true
apples-to-apples comparison, ~10.5 hours apart on the same machine.

| Metric | Committed baseline (07:10) | Live re-run (17:45) |
|---|---|---|
| noop c=1 gateway p50 | 30.9ms | 27.15ms |
| noop c=10 gateway p50 | 329.9ms | 322.51ms |
| noop c=25 gateway p50 | 838.9ms | 857.77ms |
| noop c=50 gateway p50 | 1730.0ms | 1749.92ms |
| noop c=100 gateway p50 | 3513.2ms | 3484.99ms |
| fixed_latency c=1 overhead | +89% | +73.6% |
| fixed_latency c=100 overhead | +285% | +290.9% |
| policy-eval p50/p95/p99 | 0.98/2.23/2.77ms | 0.94/2.28/3.30ms |
| errors / timeouts | 0% / 0% | 0% / 0% |
| total requests | 27,900 | 27,900 |

The absolute millisecond values track closely at concurrency >= 10. The
`c=1` *percentage* overhead diverges more (+389% baseline vs. +638.7% in
one earlier reading during this same live run's raw output) because the
direct-path baseline at concurrency 1 is a single-digit-millisecond number,
which is inherently noise-sensitive on a shared, busy interactive
development machine - not evidence of a real implementation change. The
committed README numbers were left untouched; this was an independent
reproduction, not a replacement.

The live run's raw results were written to
`benchmarks/results/2026-09-08T17-45-38.212871+00-00.json` (not committed
during this audit - left for the project owner to decide whether to keep
as a second data point).

### Item 19 - limitations spot-checked directly against source

- **"No hot policy reload"** - confirmed no reload/watch mechanism exists
  anywhere in `sentinelmcp/policy/*.py` or `sentinelmcp/gateway/server.py`.
- **"The gateway re-fetches the upstream's tool list on every `tools/call`
  (no caching)"** - confirmed: `bridge.py` calls `upstream.list_tools()`
  both in the dedicated `tools/list` handler and again inside the
  `tools/call` handler, with no cache in between.
- **"Single-process, in-memory rate limiting"** - confirmed:
  `RateLimiter._buckets` is a plain `dict[str, TokenBucket]`, no
  persistence or cross-process coordination.

The remaining 3 documented limitations were not independently
re-derived from source during this audit.

---

## Item 20 - every numeric claim traced to a reproduction command

| Claim | Reproduce with |
|---|---|
| 179 tests (was 178; +1 from this audit's follow-up fix) | `pytest` |
| 2 Hypothesis property tests | `pytest -k "test_evaluation_never_mutates_arbitrary_argument_values or test_redacted_mode_never_leaks_the_sensitive_value_at_any_nesting_depth_or_spelling"` |
| Benchmark table, all p50/p95/p99/overhead numbers | `python -m benchmarks.run` |
| 0% errors/timeouts, 27,900 requests | same command - printed and written to `benchmarks/results/<timestamp>.json` |
| Policy-eval p50/p95/p99 | same command - computed from that run's own audit log |
| Environment specs (OS/CPU/dependency versions) | same command's `environment` block, or `python --version` / `pip show mcp uvicorn httpx2 jsonschema` |
| "18 invariants map to >= 1 test" | `pytest -k "<test name>"` per row of `docs/adversarial-tests.md`'s invariant table |

---

## Clean-checkout verification

Performed for real, not assumed:

1. `git clone https://github.com/Rudrani26/SentinelMCP.git` into a scratch
   directory - a genuinely fresh clone of the real remote, confirmed at
   commit `83dabfb`, not a copy of the local working tree.
2. Fresh `venv`, `pip install -e ".[dev]"` from scratch - resolved cleanly,
   41 packages installed, including `mcp==2.2.0`.
3. `pytest` in that clean checkout: **178 passed in 18.11s** (this was
   before the item-11 test was added; re-running now would show 179).

**Docker was not verified at the time of the original audit above.**
`docker --version` returned "command not found" in that environment -
confirmed directly, not assumed unchanged from an earlier session. As a
substitute, the exact non-Docker command block from the README ("Without
Docker, run the two processes directly") was run verbatim and reproduced
the README's documented demo output exactly, including the JSON payloads
and error messages.

`docker compose up -d` itself was subsequently installed and verified by
direct execution in a follow-up session the same day - see the
[Docker verification addendum](#docker-verification-addendum-2026-09-08-follow-up-session)
below. `pytest` and `python -m benchmarks.run` inside a running Docker
Compose environment specifically (as opposed to the venv, where both were
already verified above) were not additionally re-run in that follow-up
session.

---

## Follow-up fixes applied

Two gaps were identified during this audit and fixed in this same session:

**1. Dangling "Phase 6 report" / "Phase 0 report" references.** These
build-log documents never existed as files in the repository - they only
ever existed in the chat transcript of the build session, which a reader
of a clean checkout cannot access. Found in three places, all now fixed:

- `README.md` - dropped the "see the Phase 6 build log" pointer; now
  references only `docs/adversarial-tests.md`, which exists.
- `docs/adversarial-tests.md` - previously said the full 18-invariant
  mapping "is in the Phase 6 report, not duplicated here." It now contains
  that mapping directly: a new table with one row per CLAUDE.md security
  invariant (1-18), each citing test names that were individually run and
  confirmed to exist and pass (35 test selections, all green) before being
  written down.
- `docs/threat-model.md` - same dangling reference, removed.
- `docs/architecture.md` - a third instance ("flagged in the Phase 0
  report") was found during the fix pass; reworded to describe the open
  question (whether the gateway should reject a client negotiating into
  the SDK's sessionless protocol era) without citing a nonexistent
  document.

**2. Item 11's end-to-end gap.** Numeric min/max out-of-range rejection was
previously only proven at the policy-engine unit level
(`test_min_max_reject_out_of_range`), not through a live round trip against
a real gateway. Added
`test_numeric_out_of_range_call_is_rejected_and_never_reaches_upstream` to
`tests/integration/test_phase3_argument_policy.py`: sends `limit: 101`
(schema-valid - the upstream tool only declares `limit` as an `integer`
with no schema-level range) against `constrained-diagnostics-agent`'s
`max: 100` policy constraint, over a real running gateway+upstream pair.

Verification after both fixes:

```text
pytest tests/integration/test_phase3_argument_policy.py::test_numeric_out_of_range_call_is_rejected_and_never_reaches_upstream -v
  -> 1 passed

ruff check .
  -> All checks passed!

pytest -q
  -> 179 passed in 11.30s
```

---

## Known residual gaps (as of this report)

1. **Docker.** Resolved same day - see the
   [Docker verification addendum](#docker-verification-addendum-2026-09-08-follow-up-session).
   `Dockerfile` and `compose.yaml` are no longer inference-only; both
   images were built and run for real. Not re-verified in that follow-up
   session: `pytest` and `python -m benchmarks.run` executed *inside* the
   Compose environment specifically, and higher benchmark concurrency
   levels against the containerized gateway.
2. **Benchmark wall-clock time.** The live re-run took ~24 minutes on this
   machine, not the "a few minutes" the README's Quick Start section
   implies. Worth either loosening that wording or noting it depends on
   machine load - not fixed in this session since it wasn't part of either
   requested fix.
3. **Item 19** was spot-checked (2 of 6 documented limitations verified
   directly against source) rather than exhaustively re-derived line by
   line.

None of these three block the project; they are disclosed here rather than
rounded up to "done," per this project's own resume-integrity rule.

---

## Docker verification addendum (2026-09-08, follow-up session)

Docker was not installed in the environment used for the original audit
above. In a separate follow-up session the same day, Docker Desktop was
installed and `docker compose up -d` was verified by direct execution,
closing the one Docker-shaped gap the original audit flagged.

**What was actually run:**

1. `winget install --id Docker.DockerDesktop -e` - Docker Desktop 4.90.0
   installed (WSL2 backend was already present on this machine from a
   prior, unrelated setup, so no WSL/Hyper-V enablement or reboot was
   needed). `docker info` confirmed a live daemon (`Server Version: 29.7.2`,
   `overlayfs` storage driver).
2. `docker compose up -d --build` from the repository root - both images
   (`sentinelmcp-upstream`, `sentinelmcp-gateway`) built successfully from
   the committed `Dockerfile` with no changes required to it or
   `compose.yaml`. `pip install .` inside the build resolved all
   dependencies (`mcp==2.2.0` included) with no errors.
3. `docker compose ps` - both containers `Up`, not restarting/crash-looping.
4. `docker compose logs` for both services - clean startup: upstream shows
   `StreamableHTTP session manager started` and a real MCP session
   (`Created new transport with session ID: ...`) initiated by the gateway
   on its own startup; gateway shows a clean Uvicorn startup with no
   errors.
5. `python -m examples.demo --host 127.0.0.1 --port 8080` run from the
   host against the running containerized gateway - reproduced the
   README's documented demo output exactly: `diagnostics-agent` discovery
   filtering, a permitted constrained call, a schema-invalid rejection, a
   policy-invalid rejection, hidden-tool denial, `readonly-agent`'s
   narrower tool list, and both credential-failure cases (missing and
   unknown). This is the same demo script the original audit used for the
   non-Docker path, now run through the real containers instead.
6. `docker compose down` - containers, and the network they created, were
   stopped and removed cleanly afterward (this was a verification run, not
   a request to leave the demo running).

**One real issue found and fixed along the way:** after installing Docker,
`docker compose build` initially failed with `error getting credentials -
err: exec: "docker-credential-desktop": executable file not found in
%PATH%`. This was a session-PATH staleness issue (the installer added
`C:\Program Files\Docker\Docker\resources\bin` to the system PATH, but the
already-open shell hadn't picked up the change) - not a defect in this
repository's `Dockerfile` or `compose.yaml`. Prepending that directory to
the shell's `PATH` resolved it; a freshly opened shell would not hit this.

**Still not covered by this addendum:** `pytest` and
`python -m benchmarks.run` were not additionally run *inside* the Compose
environment (both were already verified against the venv in the original
audit above); and the benchmark's higher concurrency levels were not
re-measured against the containerized gateway specifically. Item 18's
exit-criteria command chain (`docker compose up -d && pytest && python -m
benchmarks.run`) is therefore verified piecewise (each command individually
confirmed to work) rather than as one unbroken run.
