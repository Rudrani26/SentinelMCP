# SentinelMCP — Project Deep Dive

**Purpose of this document:** a complete, honest explanation of what SentinelMCP
is, how it was actually built, why it was built this way, a full code
walkthrough of one real request through the entire system, and a candid
assessment of whether this project is a good addition to your SWE / AI-agent
developer resume given your actual background.

This is a personal/portfolio-analysis document, not a description of
implemented product behavior — it deliberately lives outside `docs/`, which
CLAUDE.md reserves for `architecture.md`, `policy-semantics.md`,
`benchmark-methodology.md`, and `threat-model.md`.

---

## 1. What SentinelMCP is, in one paragraph

SentinelMCP is a Python authorization gateway that sits between an MCP
(Model Context Protocol) client — typically an LLM agent — and one upstream
MCP tool server. It terminates its own MCP server session toward the client
and independently holds its own MCP client session toward the upstream
server. In between, it authenticates the caller, filters which tools that
caller can even discover, independently re-authorizes every single tool
call regardless of what discovery showed, validates arguments against both
the upstream tool's own JSON Schema and a separate SentinelMCP-specific
policy, enforces a per-principal token-bucket rate limit and an
upstream-concurrency ceiling, and writes exactly one structured, redacted,
correlated audit record per call attempt. Nothing about this requires a
real LLM, a paid API, or cloud infrastructure — it runs entirely locally.

---

## 2. Why this project (the real motivation, not the marketing version)

Two honest reasons, in order of how much they actually drove the work:

**1. It generalizes something you already did professionally.** Your Zoox
internship bullet describes "a zero-trust LLM execution pipeline (FastMCP)
decoupling agent routing from AWS operations, constraining agents to 28+
predefined skills across 30+ tools via read-only guardrails, scoped IAM
access, and short-lived Vault credentials." That is, structurally, the same
problem SentinelMCP solves: an LLM agent must be allowed to call *some*
tools against a real backend, but not all of them, not with arbitrary
arguments, and not without an audit trail — and the enforcement has to sit
*outside* the agent, because you cannot trust the agent's own judgment
about what it should be allowed to do. At Zoox this was built against real
production infrastructure you can't take with you or talk about in detail
in an interview beyond the bullet point. SentinelMCP is the from-scratch,
fully-owned, fully-testable, fully-explainable version of the same
architectural pattern — built specifically so you can open any file in it
during a technical interview and defend every decision, which you cannot
do with an employer's private codebase.

**2. MCP is new enough that "gateway/guardrail for MCP" is a genuinely
underexplored problem**, not a solved-a-thousand-times tutorial exercise
(unlike, say, "build a URL shortener" or "build a chat app"). Implementing
it forces engagement with real protocol semantics (session lifecycle,
capability negotiation, JSON-RPC error shapes) rather than an imagined
simplification of them.

The project's own `CLAUDE.md` working agreement made this concrete and
kept it from sprawling: strict phase-by-phase scope, a hard "no resume
metric without a real test/benchmark behind it" rule, and an explicit list
of adjacent things *not* to build (OAuth, multi-upstream routing, hot
policy reload, wildcard rules, distributed rate limiting) so the project
stayed small enough to fully understand rather than becoming a shallow
wrapper around a dozen frameworks.

---

## 3. What it actually demonstrates (mapped to skills, not buzzwords)

| Area | What was built | Why it's a real signal |
|---|---|---|
| Protocol-level systems design | Two independently-lifecycled MCP sessions (downstream server role, upstream client role) bridged through one process, using the official SDK's low-level `Server` + `Client` primitives rather than a naive byte-forwarding proxy | Most "gateway" tutorials fake this by literally piping bytes through; this one had to reason about *session* semantics, not just HTTP |
| Async correctness under concurrency | A token-bucket rate limiter and a concurrency ceiling, both behind `asyncio.Lock`, both proven race-free with a live 120-way concurrent burst that never exceeded a configured ceiling of 5, verified from *two independent counters* (the limiter's own peak counter and the upstream tool's own separate tracker) | This is the kind of correctness bug that "looks fine" in a code review and only breaks under real concurrent load — proving it under load, not just reading the code, is the actual skill |
| Security-minded engineering, generalized | Fail-closed authentication and authorization, strict type semantics (`100 != "100"`, `True != 1`) so policy can't be bypassed by type confusion, default-deny policy evaluation, constant-time credential comparison | Transfers directly to any SWE role that touches auth, permissions, or input validation — not just "security roles" |
| Test discipline | 179 automated tests across unit / integration / adversarial / concurrency / property-based (Hypothesis) suites, each invariant traced to a named test that was actually re-run to confirm it exists and passes (not just written down from memory) | Shows you test for behavior under adversarial and boundary conditions, not just the happy path |
| Observability | Structured JSONL audit records with correlation IDs connecting a response to its audit line, explicit distinguishable outcome categories (`policy_denied` vs. `schema_rejected` vs. `rate_limited` vs. `concurrency_rejected` vs. `upstream_error` vs. `upstream_timeout` vs. `cancelled`), recursive sensitive-field redaction | This is the part of "production-mindedness" that's easy to skip in a portfolio project and immediately obvious to a senior interviewer when it's missing |
| Reproducible measurement | A real direct-vs-gateway benchmark harness (not vibes), 5 trials/config, 27,900 requests, policy-evaluation latency measured *separately* from total gateway overhead | Shows you know the difference between "I measured X" and "I assume X," and can defend a number when asked how it was produced |
| DevOps basics | Docker/Docker Compose packaging (verified working this session, not just written), GitHub Actions CI running lint + full suite on every push, confirmed green | Not the headline of the project, but proves you can ship something that a stranger can actually run from a clean checkout |

---

## 4. How it was actually implemented: the phase-by-phase build

The build followed a strict phase discipline defined in the project's own
`CLAUDE.md` (a checked-in instructions file establishing a working
agreement, scope, and reporting format for the whole build) — each phase
had explicit exit criteria, required passing tests before moving on, and
was never allowed to silently expand scope. In order:

- **Phase 0 — protocol spike.** Before writing any gateway code, built a
  minimal real upstream MCP server (`examples/upstream_server.py`) exposing
  four demo tools, and a real MCP SDK client, over real Streamable HTTP —
  to prove the actual SDK's behavior firsthand rather than assume it from
  memory or documentation. Pinned the exact SDK version (`mcp==2.2.0`).
- **Phase 1 — minimal bridge.** The smallest possible end-to-end bridge:
  downstream session in, upstream session out, `tools/list` and
  `tools/call` forwarded with zero authorization logic — to prove the
  two-session architecture itself worked before adding any policy on top
  of it.
- **Phase 2 — authentication and authorized discovery.** Bearer API-key
  auth, principal resolution, default-deny tool policy, and `tools/list`
  filtering — proved with an observable upstream invocation counter that a
  denied call never reaches the real tool.
- **Phase 3 — schema and argument policy enforcement.** Upstream JSON
  Schema validation plus SentinelMCP's own constraint operators
  (`equals`, `in`, `min`/`max`, `min_length`/`max_length`, `pattern`,
  `required`, `forbidden`, nested paths, unknown-field rejection), with
  strict type semantics enforced throughout.
- **Phase 4 — rate and concurrency enforcement.** The token bucket and
  concurrency ceiling described above, with deterministic/injectable-clock
  unit tests plus a live hostile concurrency test.
- **Phase 5 — audit records.** Structured JSONL, correlation IDs, recursive
  redaction, safe concurrent writes.
- **Phase 6 — adversarial and property-based hardening.** Explicit tests
  for every documented security invariant, plus Hypothesis-generated
  nested/boundary/type-confused argument structures.
- **Phase 7 — reproducible benchmarking.** The direct-vs-gateway harness.
- **Phase 8 — packaging, CI, docs, demo.** Dockerfile, Compose, GitHub
  Actions, and the four `docs/` files.

After all eight phases were reported complete, a **separate independent
self-audit** was performed against the project's own 20-item "Definition of
Done" checklist (`docs/verification-report.md`) — re-running the actual
named tests rather than trusting the phase reports, doing a genuinely fresh
`git clone` into a scratch directory to check the "clean checkout" claim,
and re-running the full benchmark live to compare against the committed
baseline. That audit found two real gaps (a missing end-to-end test for
numeric range rejection, and dangling documentation references to a
"Phase 6 report" that never existed as an actual file) and fixed both.
Docker itself wasn't installed in that audit's environment, so its
Docker-dependent claim was explicitly left "unverified" rather than assumed
— and was only closed out in a later session (see the report's Docker
addendum) by actually installing Docker Desktop and running
`docker compose up -d` for real. This pattern — audit your own claims,
distinguish "I verified this" from "this looks right on inspection," and
close gaps instead of hiding them — is itself part of what makes this a
strong interview story, independent of the gateway's own architecture.

---

## 5. Full code walkthrough: one complete request, end to end

This traces a single real scenario through every layer of the actual
codebase: **`diagnostics-agent` calls `database.query_stats` with valid
arguments, and it succeeds** — then, in section 5.9, the same call is
retraced for a **denied** case (a hidden tool) to show how the same
pipeline produces a different, but equally well-defined, outcome.

### 5.1 The request arrives: HTTP + Bearer auth

A client sends an HTTP POST to the gateway's `/mcp` endpoint with:

```http
POST /mcp HTTP/1.1
Authorization: Bearer demo-only-not-a-real-secret-1
Content-Type: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "database.query_stats",
            "arguments": {"database": "staging", "limit": 10, "include_query_text": false}}}
```

This hits `BearerAuthMiddleware` first — it wraps the **entire** gateway
ASGI app (`sentinelmcp/gateway/server.py:build_gateway_app`), not just
tool-related routes, so even the MCP session's `initialize` call is
authenticated before any MCP-level code runs at all:

```python
# sentinelmcp/gateway/identity.py
async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        await self._app(scope, receive, send)
        return

    header_values = [
        value.decode("latin-1")
        for name, value in scope["headers"]
        if name.decode("latin-1").lower() == "authorization"
    ]
    principal = self._resolver.resolve(header_values)
    if principal is None:
        response = Response(content=_UNAUTHENTICATED_BODY, status_code=401, ...)
        await response(scope, receive, send)
        return

    token = _principal_var.set(principal)
    try:
        await self._app(scope, receive, send)
    finally:
        _principal_var.reset(token)
```

`IdentityResolver.resolve` fails closed on every ambiguous case — zero
`Authorization` headers, more than one, a scheme that isn't literally
`Bearer`, an empty token, or a token that doesn't match any configured
principal — by returning `None` for all of them, which the middleware
uniformly turns into an HTTP 401. Note what the caller **cannot** do:
there is no field anywhere in the request (no `principal=` param, no
`X-Principal` header) that lets a caller choose who it is. The only input
consulted is the `Authorization` header, matched against a
digest-to-principal table built once at startup:

```python
digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
```

Tokens are compared as SHA-256 digests looked up in a dict, not via a loop
of raw-string equality checks against each configured principal — so no
comparison's timing depends on which principal (if any) matches or how
many are configured.

On success, the principal name (`"diagnostics-agent"`) is stashed in a
`contextvars.ContextVar`, scoped to this one request, and the request is
handed to the actual MCP server underneath.

### 5.2 The MCP server dispatches to the call-tool handler

`build_gateway_server` (`sentinelmcp/gateway/server.py`) constructed a
low-level SDK `Server` at startup with only two capabilities registered —
`on_list_tools` and `on_call_tool` — so this gateway never advertises or
negotiates resources, prompts, sampling, or logging capabilities it
doesn't implement. The SDK parses the JSON-RPC envelope, recognizes
`tools/call`, and invokes `handle_call_tool` (built by
`make_call_tool_handler` in `sentinelmcp/gateway/bridge.py`) with a
`ServerRequestContext` carrying the upstream `Client` connection (opened
once for the whole gateway process lifetime, in the `lifespan` context
manager) and the parsed `CallToolRequestParams`.

### 5.3 Setup: correlation ID, principal, and per-call state

```python
correlation_id = new_correlation_id()          # uuid4 hex, unique per attempt
principal = current_principal()                # "diagnostics-agent", from the contextvar
tool_name = params.name                         # "database.query_stats"
arguments = params.arguments or {}               # {"database": "staging", "limit": 10, "include_query_text": False}
total_timer = LatencyTimer()                     # monotonic-clock stopwatch
```

Several `*_result` locals start in a "not yet evaluated" state
(`"failed"` for rate limit — deliberately pessimistic until proven
otherwise; `"not_evaluated"` for schema/policy/concurrency). These exist
purely so that no matter which stage the function returns from, `emit()`
can write one complete, honest audit record describing exactly how far the
request actually got — not a record that pretends every stage ran when
some were skipped.

### 5.4 Stage 1 — rate limiting (before anything else)

```python
if not await rate_limiter.try_acquire(principal):
    await emit(AuditOutcome.RATE_LIMITED)
    return finalize(_rate_limited_result(tool_name))
rate_limit_result = "passed"
```

Inside `RateLimiter.try_acquire` → `TokenBucket.try_consume`
(`sentinelmcp/gateway/limits.py`):

```python
async def try_consume(self, amount: float = 1.0) -> bool:
    async with self._lock:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False
```

`diagnostics-agent`'s bucket started full (20 tokens, the gateway's
default capacity) and refills lazily — computed from elapsed monotonic
time at the moment of the call, not by a background task ticking on a
timer — at 5 tokens/second. This call consumes one token whether or not it
is later denied by policy: **every authenticated, structurally valid
attempt** counts against the bucket, which is a deliberate, documented
choice (an attacker probing for which tools exist by hammering `tools/call`
with denied names still burns their own rate budget).

### 5.5 Stage 2 — tool authorization

```python
tool_policy = resolve_tool_policy(policy, principal, tool_name)
principal_policy = policy.principals.get(principal)
if principal_policy is not None and principal_policy.tools.get(tool_name) is not None:
    matched_rule = tool_name
if tool_policy is None:
    policy_result = "failed"
    await emit(AuditOutcome.POLICY_DENIED)
    return finalize(_denied_result(tool_name))
```

`resolve_tool_policy` (`sentinelmcp/policy/engine.py`) is the actual
default-deny gate:

```python
def resolve_tool_policy(policy: PolicyConfig, principal: str, tool_name: str) -> ToolPolicy | None:
    principal_policy = policy.principals.get(principal)
    if principal_policy is None:
        return None
    tool_policy = principal_policy.tools.get(tool_name)
    if tool_policy is None or tool_policy.effect != "allow":
        return None
    return tool_policy
```

Three completely different denial reasons — an unrecognized principal, no
rule for this exact tool name, or an explicit `effect: deny` rule —
collapse to the exact same `None` return value and, downstream, the exact
same error text (`"Unknown tool: {tool_name}"`). This is a deliberate
design decision: it means a caller who already knows the exact name of a
tool hidden from their `tools/list` gets **no additional information** by
attempting to call it directly versus calling a tool that plain doesn't
exist. For `diagnostics-agent` calling `database.query_stats`, the
policy YAML (`sentinelmcp/policy/policies/example.yaml`) has an explicit
`effect: allow` rule, so `tool_policy` is returned and the pipeline
continues.

The gateway then re-fetches the upstream's live tool list
(`await upstream.list_tools()`) and confirms `database.query_stats` is
actually there — deliberately, on every call, with no cache — and treats
"allowed by policy, but the upstream doesn't actually have it" identically
to a denial, for the same "don't leak information" reasoning.

### 5.6 Stage 3 — upstream schema validation

```python
policy_timer = LatencyTimer()
schema_violation = validate_input_schema(upstream_tool.input_schema, arguments)
if schema_violation is not None:
    schema_result = "failed"
    ...
    return finalize(_schema_invalid_result(tool_name, schema_violation))
schema_result = "passed"
```

`validate_input_schema` (`sentinelmcp/policy/engine.py`) runs the
**upstream tool's own** JSON Schema (fetched from the live upstream, not
duplicated or guessed at by SentinelMCP) through the standard `jsonschema`
library:

```python
def validate_input_schema(input_schema, arguments) -> str | None:
    if input_schema is None:
        return None
    try:
        jsonschema.validate(instance=arguments, schema=input_schema)
    except ValidationError as exc:
        return exc.message
    except SchemaError as exc:
        return f"upstream inputSchema is invalid: {exc.message}"
    except Exception as exc:
        return f"upstream inputSchema could not be evaluated: {exc}"
    return None
```

Every failure mode — a genuinely invalid schema, or one `jsonschema` can't
even evaluate — is caught and turned into a violation reason rather than
allowed to raise, so one malformed upstream tool schema can never crash the
gateway process for every other tool and principal sharing it. For this
call, `{"database": "staging", "limit": 10, "include_query_text": false}`
satisfies the upstream's declared schema (`database: string`,
`limit: integer`, `include_query_text: boolean`), so this stage passes.

### 5.7 Stage 4 — SentinelMCP's own argument-constraint policy

```python
policy_violation = evaluate_argument_policy(tool_policy, arguments)
policy_latency_ms = policy_timer.elapsed_ms()
if policy_violation is not None:
    ...
    return finalize(_policy_invalid_result(tool_name, policy_violation))
policy_result = "passed"
```

`evaluate_argument_policy` walks the policy's `required`, `forbidden`,
`deny_unknown_arguments`, and `arguments` (per-path constraints) checks in
that order, on the **exact arguments object the caller sent** — never a
copy, never a coerced version. The per-constraint check
(`_check_constraint`) is where the project's strict-type invariant lives:

```python
def _strict_equal(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b
```

Plain Python `==` treats `100 == 100.0` and `True == 1` as equal — exactly
the kind of type confusion that could let a malicious or malfunctioning
agent slip a boolean past an `equals: false` check, or a float past an
`in: [staging, production]` check expecting strings. `type(a) is type(b)`
rejects the type mismatch before `==` is even evaluated. The
`example.yaml` policy for this tool requires `database`/`limit`, forbids
nothing extra by name, sets `deny_unknown_arguments: true`, constrains
`database` to `in: [staging, production]`, `limit` to `min: 1, max: 100`,
and `include_query_text` to `equals: false`. This call's arguments satisfy
every one of those, so `evaluate_argument_policy` returns `None` (no
violation) — and, critically, the `arguments` dict itself was never
mutated by any of this evaluation.

### 5.8 Stage 5 — concurrency ceiling, then the real upstream call

```python
async with concurrency_limiter.acquire(principal) as acquired:
    if not acquired:
        ...
        return finalize(_concurrency_rejected_result(tool_name))
    concurrency_result = "passed"

    upstream_timer = LatencyTimer()
    try:
        result = await upstream.call_tool(tool_name, arguments)
    except TimeoutError:
        ...
    except asyncio.CancelledError:
        ...
        raise
    except Exception:
        ...
        raise

    upstream_latency_ms = upstream_timer.elapsed_ms()
    await emit(AuditOutcome.UPSTREAM_SUCCESS, upstream_attempted=True, upstream_result_category="success")
    return finalize(result)
```

`concurrency_limiter.acquire` is only entered **after** every prior gate
has passed — a call that will be denied never touches concurrency
accounting at all. `ConcurrencyLimiter.acquire` (`sentinelmcp/gateway/limits.py`)
is an async context manager that increments a per-principal active count
under a lock, yields whether capacity was actually available, and —
critically — releases that capacity in a `finally` block regardless of how
the `async with` block exits: normal return, an upstream exception, a
wrapped timeout, or `asyncio.CancelledError` all release it identically.
`diagnostics-agent` is well under its concurrency ceiling, so capacity is
acquired, and `upstream.call_tool("database.query_stats", arguments)` is
sent as a real MCP `tools/call` request over the gateway's own
long-lived upstream session — the **same arguments object** that was just
policy-evaluated, forwarded completely unchanged.

On the upstream (`examples/upstream_server.py`), the real tool function
runs:

```python
@server.tool(name="database.query_stats")
def query_stats(database: str, limit: int, include_query_text: bool = False) -> dict:
    counters.query_stats += 1
    return {"database": database, "limit": limit, "include_query_text": include_query_text, "rows": []}
```

and returns a result, which the SDK wraps into a `CallToolResult` and
sends back over the upstream MCP session. Back in the gateway, that result
is what gets returned to the *downstream* caller — SentinelMCP does not
transform or re-shape a successful upstream payload.

### 5.9 Audit emission and correlation, on every path (including this success)

Every one of the return points above — rate-limited, policy-denied,
schema-rejected, concurrency-rejected, upstream-timeout,
upstream-error, cancelled, and this success case — funnels through the
same closure:

```python
async def emit(outcome, *, upstream_attempted=False, upstream_result_category=None):
    await audit_logger.emit(AuditRecord(
        correlation_id=correlation_id, principal=principal, session_id=session_id,
        tool_name=tool_name, arguments=arguments, outcome=outcome, matched_rule=matched_rule,
        schema_result=schema_result, policy_result=policy_result,
        rate_limit_result=rate_limit_result, concurrency_result=concurrency_result,
        upstream_attempted=upstream_attempted, upstream_result_category=upstream_result_category,
        policy_latency_ms=policy_latency_ms, upstream_latency_ms=upstream_latency_ms,
        total_latency_ms=total_timer.elapsed_ms(),
    ))
```

For this call, the resulting JSONL line (after default `redacted`-mode
argument representation — none of these particular argument names match
a sensitive-field fragment, so nothing here actually gets replaced) looks
like:

```json
{"correlation_id":"a1b2c3...","principal":"diagnostics-agent","session_id":"57e347b9...",
 "tool_name":"database.query_stats","arguments":{"database":"staging","limit":10,"include_query_text":false},
 "outcome":"upstream_success","matched_rule":"database.query_stats","schema_result":"passed",
 "policy_result":"passed","rate_limit_result":"passed","concurrency_result":"passed",
 "upstream_attempted":true,"upstream_result_category":"success",
 "policy_latency_ms":0.94,"upstream_latency_ms":12.3,"total_latency_ms":14.1,
 "timestamp":"2026-09-08T12:41:03.221+00:00"}
```

The response's `finalize()` step stamps the **same** `correlation_id`
into `CallToolResult.meta`, so a client, this audit line, and (in a real
deployment) any transport-level log line for the same HTTP request can all
be tied together — without that correlation ID ever containing or being
derived from the caller's credential.

`AuditLogger.emit` itself (`sentinelmcp/telemetry/audit.py`) serializes the
record to one complete JSON string *before* touching the file, then
performs one `asyncio.to_thread`-wrapped `write()` call, serialized behind
one `asyncio.Lock` — so two concurrent calls' audit lines can never
interleave mid-write, regardless of how the OS schedules the underlying
write syscalls.

### 5.10 The same pipeline, denied: a hidden tool

Now retrace the identical code path for `diagnostics-agent` calling
`database.execute_write` — a tool present upstream, but configured
`effect: deny` in the policy, and therefore **also absent from that
principal's `tools/list`** (see `make_list_tools_handler`, which filters
via the exact same `is_tool_allowed` check used at call time). A client
that already knows this tool's exact name from reading the source code, or
from a previous session as a different principal, tries it anyway:

- Stage 1 (rate limit) still consumes a token — denial doesn't refund it.
- Stage 2 (`resolve_tool_policy`) finds a rule for `database.execute_write`,
  but its `effect` is `"deny"`, not `"allow"` — so the function returns
  `None`, exactly as it would for a tool with no rule at all.
- The handler emits `AuditOutcome.POLICY_DENIED` and returns
  `_denied_result("database.execute_write")`, whose text is
  `"Unknown tool: database.execute_write"` — **identical in shape** to
  what an actually-nonexistent tool name would produce.
- Schema validation, argument-constraint evaluation, and the concurrency
  ceiling are never reached — `schema_result` and `concurrency_result`
  stay `"not_evaluated"` in the audit record.
- The real upstream `execute_write` Python function is never called — its
  `counters.execute_write` observable counter stays exactly where it was
  before the attempt. This is the literal, testable proof behind the
  project's central claim: hiding a tool from discovery is not a security
  boundary; the independent per-call authorization check is.

---

## 6. Notable engineering decisions (and the tradeoffs behind them)

- **Immediate-reject concurrency, not a wait queue.** When a principal's
  concurrency ceiling is full, the call is rejected immediately rather than
  queued to wait for a slot. Simpler to reason about and test for
  correctness under load; the tradeoff is that a legitimate burst of calls
  from one well-behaved agent can get some of its calls rejected rather
  than smoothly throttled. Documented explicitly rather than left implicit.
- **Fail-closed collapsing of denial reasons into one indistinguishable
  response.** An unknown principal, a missing rule, and an explicit deny
  all look the same to the caller. This trades a small amount of debugging
  convenience (an operator has to check the audit log, not the API
  response, to know *why* something was denied) for not leaking which
  tools exist to a caller who shouldn't be able to enumerate them.
- **Single-process, in-memory rate limiting and concurrency accounting,
  explicitly not distributed.** A real multi-instance deployment would
  need shared state (e.g. Redis) for these guarantees to hold across
  processes — this is called out as an explicit v1 non-goal rather than
  silently assumed away, because building it convincingly would have meant
  building and testing a distributed system, a different (and much larger)
  project.
- **Schema and policy validation both run against the exact same argument
  object, never a coerced copy.** This is *the* invariant the whole
  project organizes around ("the value authorized is the value
  forwarded") — it rules out an entire class of bugs where a permissive
  parser silently turns a dangerous string into a permitted number (or
  vice versa) between the check and the actual call.

---

## 7. Is this a good SWE project? Honest assessment

**Yes — and specifically because of what it is not.** It's not a CRUD app,
not a wrapper around an LLM API call, not a copy of a well-known tutorial.
It required reading and correctly using a real, still-evolving SDK; it
required getting async concurrency correctness right and then *proving*
it under real concurrent load rather than asserting it; it required
building and defending a small but coherent security model with explicit,
stated boundaries (a threat model that says what it does *not* protect
against is a stronger signal of maturity than one that claims everything);
and it required a benchmark discipline that most portfolio projects skip
entirely (comparing against a real baseline, decomposing where the
overhead actually comes from, refusing to manufacture numbers).

The things that make it *specifically* strong, not just "fine":

1. **It's independently verifiable, on the spot, in an interview.** Every
   claim in this document and in the README traces to a real command
   someone can run: `pytest`, `python -m benchmarks.run`,
   `docker compose up -d`. An interviewer who says "prove it" has an
   actual answer, not a hand-wave.
2. **It demonstrates judgment, not just execution.** The `CLAUDE.md`
   working agreement, the phase discipline, the explicit non-goals list,
   and the follow-up self-audit that found and fixed its own gaps all show
   you can scope a project sanely and hold yourself to a real standard —
   which reads as more senior than raw feature count.
3. **It sits exactly on the seam between "systems engineering" and
   "AI/agent infrastructure,"** which is a genuinely scarce combination
   right now. Most AI-agent portfolio projects are entirely about the
   agent's reasoning loop (prompting, tool selection, memory) and have
   almost no systems rigor underneath. Most systems-engineering portfolio
   projects have nothing to do with agents at all. This project is
   explicitly the connective tissue — the part of agent infrastructure
   that has to be boring, correct, and adversarially tested, because it's
   the part standing between an LLM's output and a real backend.

**The honest limitation, for the roles you actually want:** SentinelMCP
deliberately contains **zero agent-reasoning code**. There is no LLM
integration, no tool-selection loop, no prompt design, no evaluation
harness for agent quality — all of that was an explicit, correct
"non-goal" for this project (a real LLM was never required, by design).
That's the right call for what this project is trying to prove, but it
means SentinelMCP alone does not demonstrate that you can build the
*agent* side of an agentic system — only the guardrail/infrastructure side
of one.

## 8. Does it suit your profile, given your actual resume?

Looking at what's already there: your **Zoox internship** bullet is
almost a mirror of this project's problem statement (FastMCP, zero-trust
agent execution, guardrails, scoped tool access) — which is genuinely
unusual and valuable. Most candidates listing an "agentic AI guardrails"
internship bullet have no way to prove they understood *why* it was built
that way, because the actual code isn't theirs to show. You can. That
pairing — "I built this professionally at Zoox, and here's a from-scratch
version I built myself to fully understand and defend every design
decision, with 179 tests and a real benchmark to back it up" — is a
strong, coherent interview narrative, not two disconnected bullet points.

Your **Maya** project already shows LLM integration experience (LLaMA 3
70B via Groq, Wav2Vec2, VITS) and your coursework includes **Agentic AI**
and **Applied NLP** — so the "agent reasoning" side of your profile is not
empty; it's just in a different project. SentinelMCP's job on your resume
isn't to duplicate that — it's to prove the *other* half: that you also
understand how to build the infrastructure that constrains and observes an
agent once it's allowed to touch anything real. Together, Maya + Zoox +
SentinelMCP tell a complete, believable story: you can build the agent,
and you can build what stands guard over it.

**Concretely, for the two role types you named:**

- **SWE roles:** strong fit as-is. Lead with the systems/protocol/testing
  framing (see the SWE-oriented resume bullets already drafted earlier in
  this conversation) rather than the security framing — a general SWE
  interviewer cares more about "did you get async concurrency right and
  prove it" than "did you build a policy engine."
- **AI agent developer roles:** strong fit, but frame it explicitly as
  *infrastructure for agents*, not *an agent*. In an interview, be ready to
  say plainly: "this doesn't do agent reasoning — it's the layer that
  makes it safe to let an agent that *does* do reasoning touch real
  tools," and point to Maya or the Zoox bullet for the reasoning-side
  evidence. If you want a resume/portfolio project that more directly
  showcases agent-reasoning skills (tool-selection logic, an actual
  multi-step agent loop, evaluation of agent decisions), that would be a
  good *complement* to this one, not a replacement for it — but that's a
  separate, new project, not something to bolt onto SentinelMCP (which
  would violate its own explicit non-goals and dilute what it currently
  proves cleanly).

**Bottom line:** keep it, lead with it, and pair it narratively with your
Zoox bullet and your Maya project rather than presenting it in isolation.
It is one of the stronger "prove you can build real infrastructure"
signals available to you right now, precisely because it's small, fully
own-authored, fully tested, and fully explainable — not because it's
large or feature-rich.
