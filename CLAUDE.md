# SentinelMCP — Implementation Instructions

You are building **SentinelMCP**, a Python authorization gateway for MCP (Model Context Protocol) tool servers.

SentinelMCP sits between an MCP client and **one configured upstream MCP server per gateway instance**. It authenticates callers, filters tool discovery, applies default-deny tool and argument policies, enforces per-principal rate and concurrency limits, and emits redacted audit records.

This is a portfolio/resume project. **Correctness, protocol compatibility, security reasoning, tests, reproducible measurements, and interview-defensibility matter more than feature count.**

The entire v1 project must be buildable, runnable, testable, benchmarkable, and demonstrable locally without paid APIs, paid infrastructure, cloud services, or a real LLM.

---

# Working Agreement

* Stay strictly within the v1 scope below.
* Do not add adjacent features without asking me first.
* Work on only one phase at a time.
* Write tests alongside the implementation.
* Prefer the smallest correct implementation.
* Do not generate empty modules or speculative abstractions merely to match a proposed repository structure.
* If the repository already contains work, inspect and preserve it.
* Before editing, inspect applicable repository instructions and current git status.
* Before using MCP SDK/FastMCP APIs, inspect the installed package, package source, or current authoritative documentation. Do not invent SDK methods, protocol behavior, or signatures from memory.
* If this specification conflicts with the actual MCP protocol or current SDK behavior, stop, explain the conflict, and propose the smallest standards-compliant adjustment.
* Do not claim behavior that has not been exercised by a real test.
* Do not invent benchmark, scale, test-count, performance, or resume metrics.
* Do not optimize implementation merely to manufacture attractive resume numbers.
* Do not silently move to another phase.

At the end of each phase:

1. Run the relevant tests.
2. Run the phase exit-criteria verification.
3. Show the exact commands and results.
4. Summarize files changed.
5. Explain important architecture/security decisions.
6. State security properties proven by tests where applicable.
7. State limitations or issues discovered.
8. Propose a clear phase-specific git commit message.
9. Stop.

Do not make the commit unless I explicitly approve it.

Do not begin the next phase until I explicitly say:

```text
continue
```

---

# V1 Threat Model

SentinelMCP should protect a trusted upstream MCP server from:

* missing or invalid caller authentication
* authenticated but malfunctioning or overprivileged clients
* unauthorized tool calls
* arguments that violate configured constraints
* excessive tool-call frequency
* excessive concurrent upstream executions
* malformed protocol requests that would otherwise crash the gateway
* accidental disclosure of configured sensitive fields through audit logs

SentinelMCP does **not** claim to protect against:

* a malicious or compromised upstream MCP server
* compromise of the gateway host/process
* stolen valid API keys
* denial-of-service from unauthenticated network traffic
* prompt injection that produces policy-compliant calls
* harmful behavior inside an authorized tool
* leakage through legitimate tool results
* distributed rate-limit evasion across gateway processes
* vulnerabilities in the MCP SDK or other dependencies
* rollback of upstream side effects
* semantic risks that cannot be expressed by configured policies
* network-layer attacks outside the application's boundary

Document these boundaries honestly.

SentinelMCP does **not** claim to prevent prompt injection.

Its purpose is to constrain which MCP tool actions can reach protected upstream servers even when a client or AI agent behaves unexpectedly or has been manipulated.

---

# Explicit V1 Scope

Build:

* Python implementation
* Streamable HTTP only
* one configured upstream MCP server per gateway instance
* downstream MCP server behavior
* upstream MCP client behavior
* separate downstream and upstream MCP sessions
* locally configured API-key authentication
* exact-name, default-deny tool policies
* filtering of unauthorized tools from `tools/list`
* independent authorization of every `tools/call`
* strict upstream tool-input JSON Schema validation
* argument constraints
* per-principal token-bucket rate limiting
* per-principal upstream concurrency ceilings
* structured, correlated, redacted audit records
* unit tests
* integration tests
* adversarial tests
* property-based tests
* concurrency tests
* reproducible direct-versus-gateway benchmarks
* Docker packaging
* CI
* documentation
* executable local demo

---

# Explicit V1 Non-Goals

Do NOT implement:

* stdio transport
* multiple upstream MCP servers
* dynamic upstream routing
* OAuth
* OIDC
* SSO
* JWT authentication architecture
* cloud IAM
* user-facing policy administration APIs
* hot policy reload
* wildcard tool selectors
* regex tool selectors
* distributed state
* Redis
* distributed rate-limiting guarantees
* anomaly detection
* agent-quality evaluation
* agent correctness scoring
* LLM evaluation harness
* circuit breakers
* automatic retries of side-effecting tools
* Kubernetes
* cloud deployment
* multi-region support
* Grafana
* full Prometheus stack
* full OpenTelemetry backend
* hosted observability services
* LLM integrations
* OpenAI API dependency
* Anthropic API dependency
* Groq API dependency
* any paid API
* any required real LLM
* RAG
* vector databases
* unrelated agent orchestration frameworks
* frontend
* hosted SaaS
* languages other than Python
* support for MCP resources, prompts, sampling, elicitation, or other MCP capabilities unless minimally required for correct protocol initialization/operation

If something in this list appears necessary, stop and explain why before implementing it.

Do not add infrastructure solely because it creates another resume keyword.

---

# Technology Constraints

Use:

* Python 3.12+
* official `mcp` Python SDK
* FastMCP where appropriate
* SDK Streamable HTTP primitives
* SDK session-management primitives
* FastAPI only where it integrates cleanly with the official SDK
* Pydantic for SentinelMCP configuration models
* a standards-compatible JSON Schema validator for arbitrary upstream tool schemas
* `asyncio`
* PyYAML or another small YAML parser
* pytest
* pytest-asyncio
* Hypothesis
* an async HTTP client suitable for MCP
* a custom async benchmark harness unless another dependency provides a clear, justified benefit
* Docker
* Docker Compose
* Git
* GitHub
* GitHub Actions

Do not hand-roll the complete MCP or JSON-RPC protocol if the official SDK already provides the required behavior.

Avoid unnecessary dependencies.

Prefer simple, explicit Python when it is correct and easier to test and explain than another dependency.

During Phase 0:

1. select and pin an exact MCP SDK version
2. record the corresponding MCP protocol/specification version
3. verify the actual current SDK APIs from authoritative documentation/package source

Do not code against an unspecified moving target.

---

# Real LLM Policy

A real LLM is **not required**.

The entire core project must work with deterministic MCP clients and test fixtures.

Conceptually:

```text
Deterministic MCP Client
Mock Agent
Local LLM Agent       ──optional future demo only
Hosted LLM Agent      ──not part of v1
        |
        v
   SentinelMCP
        |
        v
 Upstream MCP Server
        |
        v
       Tools
```

SentinelMCP must remain model-agnostic.

Do not integrate OpenAI, Anthropic, Groq, Gemini, or another hosted LLM provider during v1.

Do not require an API key for an LLM provider.

---

# Core Architecture

SentinelMCP is **not a transparent byte-forwarding proxy**.

It acts as:

* an MCP server toward the downstream client
* an MCP client toward the upstream server

These are **separate protocol sessions**.

Conceptually:

```text
                         TRUST BOUNDARY
                              |
                              v
+----------------+     +---------------------------+     +----------------+
|                |     |                           |     |                |
|  MCP Client    | --> |       SentinelMCP         | --> |  Upstream MCP  |
|                |     |                           |     |     Server     |
+----------------+     |  Authentication           |     +-------+--------+
                       |  Tool Discovery Filter    |             |
 Downstream MCP        |  Schema Validation        |             v
 Session               |  Policy Engine            |           Tools
                       |  Rate Limiter             |
                       |  Concurrency Limiter      |
                       |  Audit                    |
                       +---------------------------+
                                   |
                          Separate upstream
                            MCP session
```

The gateway must:

1. authenticate every relevant downstream HTTP request
2. establish/manage a separate upstream MCP session
3. negotiate only capabilities it actually supports
4. obtain upstream tool definitions
5. filter `tools/list` based on the authenticated principal
6. independently authorize every `tools/call`, even if the tool was hidden from discovery
7. validate arguments against the upstream tool's `inputSchema`
8. apply SentinelMCP policy constraints to the same uncoerced values
9. apply rate limiting
10. apply upstream-execution concurrency control
11. forward only allowed invocations
12. return protocol-correct results or errors
13. emit the appropriate terminal audit record

Never forward downstream session identifiers upstream as though downstream and upstream represented the same MCP session.

---

# Security Invariants

The implementation and tests must establish these invariants:

1. No request reaches a protected tool without an authenticated principal.
2. No tool absent from the principal's allow policy can execute.
3. Hiding a tool from `tools/list` is not treated as authorization.
4. Every `tools/call` is independently authorized.
5. Values evaluated by policy are the same values forwarded upstream.
6. Security validation does not coerce strings into numbers, booleans, or other privileged types.
7. Malformed policy configuration prevents startup.
8. Unknown principals fail closed.
9. Unknown tools fail closed.
10. Unknown constraint operators fail closed at configuration time.
11. Unknown policy fields fail closed.
12. A denied call never reaches the upstream tool.
13. Concurrent upstream execution never exceeds the configured ceiling.
14. Cancellation, timeout, and exceptions release concurrency capacity.
15. Sensitive values are not included in audit records under default configuration.
16. Every authenticated, structurally valid `tools/call` attempt produces exactly one terminal audit record.
17. Upstream failures are distinguishable from authentication, schema, policy, rate-limit, and concurrency failures.
18. Authorization evaluation never mutates tool argument values.

Whenever an important security feature is implemented, ask:

> What automated test proves this invariant?

---

# Identity Model

Use the standard HTTP `Authorization` header:

```http
Authorization: Bearer <api-key>
```

API keys map to principal names.

Conceptually:

```text
API key
   |
   v
Principal
   |
   v
Authorization Policy
```

Requirements:

* authenticate every relevant downstream HTTP request involved in the MCP session
* missing credentials fail closed
* malformed credentials fail closed
* unknown credentials fail closed
* ambiguously configured credentials fail closed
* caller cannot select its principal through a request field or arbitrary identity header
* real key material comes from environment variables
* configuration may reference environment-variable names
* configuration must not contain production-looking plaintext keys
* never include API keys in logs, traces, exceptions, audit records, or responses
* demo credentials must be obviously fake and documented as local-demo-only
* document that production TLS/transport security is supplied by the deployment environment and is outside v1 application scope

Avoid naive equality loops over secret material.

Where explicit credential equality comparison is performed, use a constant-time comparison such as `hmac.compare_digest`.

Keep the v1 credential implementation simple. Do not introduce external secret-management infrastructure.

---

# Tool Discovery

For `tools/list`:

* fetch or use current upstream tool definitions
* preserve upstream tool name
* preserve upstream tool description
* preserve upstream `inputSchema`
* return only tools authorized for the authenticated principal
* omit unauthorized tools
* do not mutate upstream schemas merely to represent SentinelMCP policy constraints
* handle malformed/unsupported upstream schemas safely

Call-time authorization remains mandatory.

A client that already knows a hidden tool's exact name must still be denied when it invokes `tools/call`.

A hidden tool is not a security boundary.

The authorization engine is the security boundary.

---

# Argument Validation Pipeline

Use this conceptual order:

```text
Decoded JSON arguments
        |
        v
Validate against upstream inputSchema
        |
        v
Evaluate SentinelMCP constraints
        |
        v
Forward the SAME values unchanged
```

Do not use permissive Pydantic coercion for arbitrary tool arguments.

Pydantic may validate SentinelMCP's own configuration models, but it must not silently transform security-sensitive tool arguments before policy evaluation.

---

# Argument Policy Constraints

At minimum support:

* `equals`
* `in`
* numeric `min`
* numeric `max`
* string `min_length`
* string `max_length`
* anchored regular-expression `pattern`
* required fields
* forbidden fields
* nested object paths
* list-length limits
* unknown-field rejection

Keep the operator set deliberately small.

Do not evolve this into a general-purpose policy language.

Use exact tool names only in v1.

There are no wildcard tool rules and therefore no broad-rule precedence system.

Policy semantics:

* no matching tool rule -> deny
* `effect: deny` -> deny
* `effect: allow` -> continue schema/policy validation
* allow never bypasses schema validation
* allow never bypasses argument constraints
* explicit deny is permitted for readability but is equivalent to default denial for an exact tool
* unknown argument fields are rejected by default
* unknown constraint operators are configuration errors
* unknown policy fields are configuration errors
* invalid YAML prevents startup
* invalid policy structure prevents startup
* policy evaluation must not modify argument values

Document all semantics in:

```text
docs/policy-semantics.md
```

---

# Strict Type Semantics

Authorization uses strict type semantics.

Do not silently coerce values.

For authorization purposes:

```text
100     != "100"
100     != 100.0
True    != 1
False   != 0
```

unless a future explicitly documented policy feature introduces coercion.

Examples worth testing:

```text
100
100.0
"100"
"0100"
True
False
null
[100]
{"value": 100}
```

Use Hypothesis where useful to test type confusion and boundary values.

The core invariant is:

> The value authorized is the value forwarded.

---

# Example Policy

```yaml
principals:
  diagnostics-agent:
    tools:
      database.query_stats:
        effect: allow
        deny_unknown_arguments: true
        required:
          - database
          - limit

        arguments:
          database:
            in:
              - staging
              - production

          limit:
            min: 1
            max: 100

          include_query_text:
            equals: false

      database.execute_write:
        effect: deny
```

A permitted call might be:

```text
database.query_stats(
    database="production",
    limit=50,
    include_query_text=false
)
```

A denied call might be:

```text
database.query_stats(
    database="production",
    limit=10000
)
```

---

# Rate-Limit Semantics

Implement single-process, in-memory enforcement only.

Maintain a token bucket per principal.

Use:

* monotonic clock
* `asyncio`
* `asyncio.Lock` or another appropriate synchronization primitive around refill/deduction state

V1 semantics:

* every authenticated, structurally valid `tools/call` attempt consumes a token
* this includes calls later denied by policy
* authentication failures do not consume a principal's bucket
* document bucket capacity
* document refill rate
* document burst behavior
* document restart behavior
* document in-memory limitations

Use deterministic or injectable clocks in unit tests.

Avoid tests that depend on arbitrary real-time sleeps when deterministic timing can be used.

---

# Concurrency Semantics

Concurrency limits apply to active upstream executions.

Apply the ceiling only after a request has passed:

1. authentication
2. structural/schema validation
3. policy validation
4. rate-limit check

Requirements:

* count active upstream executions, not policy evaluations
* do not allow unbounded waiting
* either reject immediately when capacity is full or use a short configured acquisition timeout
* document the selected behavior
* release capacity in a `finally` block
* release capacity after success
* release capacity after upstream exception
* release capacity after timeout
* release capacity after cancellation
* test client disconnect behavior where practical
* race conditions must not permit temporary oversubscription

A hostile concurrent test must record maximum observed upstream concurrency and assert that it never exceeds the configured limit.

---

# Audit Model

Emit **one terminal structured JSON audit record** per authenticated, structurally valid `tools/call` attempt.

Every record gets a unique correlation/request ID.

Include:

* timestamp
* correlation/request ID
* principal
* MCP session identifier or safe internal session ID
* tool name
* safely represented arguments according to redaction mode
* final decision/outcome
* matched policy rule identifier when applicable
* schema-validation result
* policy result
* rate-limit result
* concurrency result
* whether upstream execution began
* upstream result category
* policy latency
* upstream latency
* total gateway latency

Use explicit outcomes such as:

```text
policy_denied
schema_rejected
rate_limited
concurrency_rejected
upstream_success
upstream_error
upstream_timeout
cancelled
internal_error
```

Every applicable request must produce exactly one terminal outcome.

Protect concurrent JSONL writes so records cannot become interleaved or corrupted.

Correlation IDs should make it possible to connect relevant responses, logs, and audit records without exposing credentials.

---

# Redaction

Support:

```text
none
keys_only
redacted
full
```

The safe default must be:

```text
redacted
```

Recursively redact common sensitive names, including:

* password
* secret
* token
* api_key
* authorization
* credential

Do not leak sensitive values through nested objects.

Do not leak them through exception/error paths.

Document clearly that `full` can expose sensitive data and is intended only for controlled local debugging.

Test recursive redaction explicitly.

---

# Malformed Input Handling

The gateway must survive malformed input without crashing or hanging.

Use official SDK behavior where appropriate rather than manually reimplementing protocol parsing.

Test cases should include, where applicable:

* invalid JSON
* invalid JSON-RPC envelopes
* unsupported methods
* missing required fields
* incorrect JSON types
* unexpected nested structures
* unknown tools
* unknown arguments
* markdown-fenced JSON where relevant
* malformed policy files
* malformed authorization headers
* malformed upstream schemas

Return protocol-correct errors where MCP defines them.

Do not invent custom protocol semantics unnecessarily.

Malformed-input handling is robustness infrastructure, not the project's headline feature.

---

# Testing Strategy

Use:

* pytest
* pytest-asyncio
* Hypothesis

Organize tests approximately as:

```text
tests/
├── unit/
├── integration/
├── adversarial/
└── concurrency/
```

Do not optimize for raw test count or coverage percentage.

Optimize for proven properties.

Required properties include:

1. missing credentials fail closed
2. malformed credentials fail closed
3. unknown credentials fail closed
4. caller cannot self-select another principal
5. unauthorized tools are hidden from discovery
6. direct invocation of a hidden tool is denied
7. denied invocation never reaches upstream
8. explicit deny behaves correctly
9. schema-valid allowed arguments succeed
10. schema-invalid arguments fail
11. policy-invalid arguments fail
12. schema rejection and policy rejection are distinguishable
13. unknown arguments fail when configured
14. unknown policy fields prevent unsafe startup
15. unknown constraint operators prevent unsafe startup
16. type confusion cannot bypass policy
17. nested malformed structures cannot bypass policy
18. forwarded values exactly match authorized values
19. token-bucket state remains correct under concurrent callers
20. concurrent upstream calls never exceed configured ceiling
21. cancellation releases concurrency capacity
22. timeout releases concurrency capacity
23. exceptions release concurrency capacity
24. upstream timeout differs from policy denial
25. upstream error differs from policy denial
26. sensitive fields are recursively redacted
27. concurrent audit writes remain valid
28. every applicable request produces exactly one terminal audit record
29. correlation IDs connect the appropriate lifecycle information
30. malformed input cannot crash/hang the gateway

Use Hypothesis for at least one meaningful property-based test generating nested, malformed, boundary, or type-confused argument structures.

Maintain a short mapping between adversarial test categories and the security invariants they prove.

Do not create meaningless tests merely to inflate resume numbers.

---

# Benchmarking

Benchmark actual loopback Streamable HTTP.

Compare:

```text
Direct:
MCP Client -> Upstream MCP Server
```

against:

```text
Gateway:
MCP Client -> SentinelMCP -> Upstream MCP Server
```

Use consistent connection/session behavior between baseline and gateway measurements.

Implement two upstream benchmark tools.

## `benchmark.noop`

Returns immediately.

Purpose:

Approximate gateway/transport/authorization overhead.

## `benchmark.fixed_latency`

Waits for a known duration and returns.

Purpose:

Measure gateway behavior when upstream work contributes meaningfully to total latency.

Use concurrency levels:

```text
1
10
25
50
100
```

Only extend to:

```text
250
500
```

if the machine remains stable and the measurements remain meaningful.

Do not treat 500 clients as a success criterion.

If the system saturates earlier, identify and document the saturation point.

For each configuration:

* warm up before measurement
* use a fixed duration or fixed request count
* run at least five trials
* record throughput
* record p50
* record p95
* record p99
* record errors
* record timeouts
* record denied-request latency
* calculate absolute gateway overhead
* calculate percentage gateway overhead
* record machine specifications
* record OS
* record Python version
* record MCP SDK version
* record relevant dependency versions
* save raw machine-readable results
* report median results
* report variability

When practical, measure **authorization/policy-evaluation latency separately from total end-to-end gateway overhead**.

Do not present policy-evaluation latency as total gateway overhead or vice versa.

Use this decomposition to determine whether observed overhead primarily comes from:

* authorization/policy evaluation
* gateway/session handling
* serialization
* HTTP/transport
* upstream execution

Do not optimize before establishing a baseline.

If proposing an optimization:

1. identify the observed bottleneck
2. provide evidence
3. explain the proposed change
4. state the expected effect
5. benchmark before
6. benchmark after

Do not manipulate benchmark configuration to manufacture attractive resume metrics.

---

# Suggested Repository Shape

Allow the project to grow approximately into:

```text
sentinelmcp/
├── gateway/
│   ├── server.py
│   ├── bridge.py
│   ├── identity.py
│   ├── limits.py
│   └── errors.py
│
├── policy/
│   ├── engine.py
│   ├── models.py
│   └── policies/
│       └── example.yaml
│
├── telemetry/
│   └── audit.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── adversarial/
│   └── concurrency/
│
├── benchmarks/
│   ├── upstream.py
│   └── run.py
│
├── examples/
│   ├── upstream_server.py
│   └── direct_client.py
│
├── docs/
│   ├── architecture.md
│   ├── policy-semantics.md
│   ├── benchmark-methodology.md
│   └── threat-model.md
│
├── Dockerfile
├── compose.yaml
├── pyproject.toml
└── README.md
```

Do not create every file before it is needed.

Do not create abstraction layers merely because the final repository tree looks cleaner with them.

Let the structure emerge with implementation needs.

---

# Documentation

Maintain:

```text
docs/
├── architecture.md
├── policy-semantics.md
├── benchmark-methodology.md
└── threat-model.md
```

Documentation must describe implemented behavior.

Do not document planned functionality as though it exists.

## `architecture.md`

Explain:

* downstream session
* upstream session
* trust boundaries
* authentication flow
* discovery filtering
* schema validation
* policy evaluation
* rate limiting
* concurrency control
* upstream execution
* audit lifecycle

## `policy-semantics.md`

Document:

* default-deny behavior
* exact tool matching
* explicit deny behavior
* strict type semantics
* schema validation
* argument constraints
* unknown-field behavior
* malformed-policy behavior
* token accounting
* concurrency accounting
* cancellation behavior

## `benchmark-methodology.md`

Document:

* direct baseline
* gateway path
* benchmark tools
* connection/session behavior
* warm-up
* trials
* concurrency
* metrics
* machine environment
* raw results
* variability
* limitations

## `threat-model.md`

Document:

* assets
* actors
* trust boundaries
* threats addressed
* threats not addressed
* assumptions
* known limitations

---

# Engineering Rules

## Rule 1 — Fail Closed

Security-relevant ambiguity should deny execution rather than permit it.

## Rule 2 — Denied Calls Never Reach Upstream

Prove this with an observable invocation counter on the fake upstream tool.

## Rule 3 — Tool Hiding Is Not Authorization

Every call is independently authorized.

## Rule 4 — Strict Security Types

Do not silently coerce security-sensitive argument values.

## Rule 5 — Authorized Value = Forwarded Value

Policy evaluation must not transform the argument values later sent upstream.

## Rule 6 — Test Security Properties

Every significant security property requires an automated test.

## Rule 7 — Explicit Concurrency Safety

Do not rely on assumptions about `asyncio` scheduling.

Protect shared state deliberately.

## Rule 8 — Use Monotonic Time

Use monotonic clocks for rate-limit calculations and duration-sensitive behavior.

## Rule 9 — Deterministic Tests Where Possible

Use injectable clocks and explicit synchronization rather than arbitrary sleeps.

## Rule 10 — No Fake Scale

Never invent benchmark results.

## Rule 11 — No Premature Optimization

Measure first.

## Rule 12 — No Resume-Driven Architecture

Do not add technology merely because it looks impressive.

## Rule 13 — Prefer Simplicity

Prefer the smallest implementation that is correct, secure, testable, maintainable, and explainable.

## Rule 14 — Preserve Interview Explainability

I should eventually be able to explain every major component myself.

For non-obvious designs, explain:

* the problem
* alternatives
* chosen approach
* why it was selected
* limitations
* tradeoffs

## Rule 15 — Verify, Don't Assume

Do not claim success because code appears correct.

Run it.

---

# Phase 0 — Protocol Research and Direct-Server Spike

Before building the gateway:

1. inspect the current workspace
2. inspect current git status
3. inspect applicable repository instructions
4. read current authoritative MCP Python SDK documentation/examples for Streamable HTTP
5. inspect installed SDK/package source where useful
6. select and pin the exact MCP SDK version
7. record the corresponding MCP protocol/specification version
8. identify any differences between this specification and actual current SDK behavior
9. provide a concise Phase 0 implementation plan

Then build a minimal real upstream MCP server exposing:

* `database.query_stats`
* `database.execute_write`
* `benchmark.noop`
* `benchmark.fixed_latency`

These are harmless test/demo tools.

Instrument tools with observable invocation counters where useful.

Also create a minimal real MCP SDK client.

The client must prove:

* session initialization
* `tools/list`
* real input schemas
* successful `tools/call`
* protocol-correct handling over Streamable HTTP

### Phase 0 Exit Criteria

* upstream server runs independently
* real SDK client connects over Streamable HTTP
* initialization succeeds
* `tools/list` returns real schemas
* safe `tools/call` succeeds
* automated integration test proves direct round trip
* pinned SDK/protocol versions are recorded

At completion:

* show versions
* show files changed
* show exact commands
* show passing tests
* explain SDK limitations discovered
* propose a commit message

Then STOP.

Do not begin Phase 1.

---

# Phase 1 — Minimal MCP Bridge

Implement the smallest end-to-end bridge.

Requirements:

* downstream MCP client connects to SentinelMCP
* SentinelMCP terminates the downstream session
* SentinelMCP creates a distinct upstream session
* initialization is handled correctly
* only supported capabilities are negotiated
* `tools/list` is bridged without filtering
* `tools/call` is bridged without authorization/policy enforcement
* upstream results preserve protocol-correct meaning
* upstream errors preserve protocol-correct meaning
* unsupported capabilities receive protocol-correct handling

No authentication or policy logic yet.

### Phase 1 Exit Criteria

* same unmodified SDK client works through SentinelMCP
* initialization works
* tool listing works
* successful tool call works
* representative upstream error works
* automated tests prove downstream and upstream sessions are distinct
* no policy logic exists
* no authentication logic exists

Then STOP.

---

# Phase 2 — Authentication and Authorized Discovery

Add:

* Bearer API-key authentication
* key-to-principal resolution
* environment-based secret loading
* fail-closed authentication
* exact-name tool permissions
* default-deny authorization
* principal-specific filtering of `tools/list`
* independent authorization at `tools/call`

### Phase 2 Exit Criteria

* valid credentials resolve to correct principal
* missing credentials are rejected
* malformed credentials are rejected
* invalid credentials are rejected
* unauthorized tools are absent from discovery
* authorized tools remain visible
* directly invoking a hidden tool is denied
* upstream invocation counter proves denied call never executed
* caller cannot spoof/select another principal

Then STOP.

---

# Phase 3 — Schema and Argument Policy Enforcement

Add:

* validation against upstream `inputSchema`
* strict type preservation
* documented argument constraint operators
* nested-path evaluation
* list-length constraints
* unknown-field rejection
* startup validation of policy configuration
* protocol-correct errors for invalid tool arguments
* malformed protocol-input handling using official SDK behavior where possible

Test:

* invalid JSON-RPC envelopes
* unsupported methods
* missing fields
* incorrect JSON types
* unknown tools
* nested bypass attempts
* type confusion
* malformed policies
* malformed upstream schemas where practical

### Phase 3 Exit Criteria

* values evaluated by policy are forwarded unchanged
* malformed policies prevent startup
* schema-invalid and policy-invalid requests are distinguishable
* representative type-confusion attacks fail
* representative nested bypass attempts fail
* permitted constrained call succeeds end-to-end
* unknown policy operators/fields fail safely

Then STOP.

---

# Phase 4 — Rate and Concurrency Enforcement

Implement:

* documented per-principal token bucket
* monotonic timing
* synchronization around token state
* per-principal upstream concurrency ceiling
* bounded acquisition behavior
* cleanup on success
* cleanup on exceptions
* cleanup on timeout
* cleanup on cancellation

Use deterministic/injectable clocks in unit tests.

### Phase 4 Exit Criteria

* token-bucket mathematics are unit tested
* token accounting semantics are tested
* 100+ simultaneous attempts cannot violate concurrency ceiling
* maximum observed upstream concurrency is recorded in test and asserted
* cancellation releases capacity
* timeout releases capacity
* upstream exceptions release capacity
* single-process/in-memory limitation is documented

Then STOP.

---

# Phase 5 — Audit Records

Implement:

* one terminal audit record per applicable request
* correlation IDs
* recursive redaction
* safe concurrent JSONL writing
* explicit outcome categories
* relevant latency measurements

### Phase 5 Exit Criteria

* every tested tool-call outcome emits exactly one terminal audit record
* concurrent writes remain valid
* default audit output contains no test secrets
* policy denial is distinguishable
* schema rejection is distinguishable
* rate limiting is distinguishable
* concurrency rejection is distinguishable
* upstream success is distinguishable
* upstream error is distinguishable
* upstream timeout is distinguishable
* cancellation is distinguishable
* correlation IDs connect relevant response/audit information

Then STOP.

---

# Phase 6 — Adversarial and Property-Based Hardening

Add tests for all security invariants.

At minimum include:

* hidden-tool direct invocation
* unauthorized invocation never reaches upstream
* unknown principal
* unknown tool
* nested unknown arguments
* string/number/boolean type confusion
* malformed schemas
* malformed policies
* unknown policy operators
* concurrency races
* cancellation races
* token-bucket boundaries
* recursive sensitive-data redaction
* concurrent audit writes
* upstream timeout
* upstream failure
* malformed envelopes
* repeated/malformed request identifiers where relevant
* at least one meaningful Hypothesis test generating nested/boundary/type-confused argument values

Maintain a short table mapping adversarial test categories to the security properties they prove.

### Phase 6 Exit Criteria

* full suite passes repeatedly
* no avoidable timing-flaky tests
* each security invariant maps to at least one automated test
* each adversarial test category has a clear one-sentence security purpose

Then STOP.

---

# Phase 7 — Reproducible Benchmarking

Benchmark actual loopback Streamable HTTP:

```text
Direct:
client -> upstream
```

versus:

```text
Gateway:
client -> SentinelMCP -> upstream
```

Benchmark:

* `benchmark.noop`
* `benchmark.fixed_latency`

Use:

```text
1, 10, 25, 50, 100
```

concurrent clients initially.

Only add:

```text
250, 500
```

if measurements remain stable and meaningful.

Requirements:

* same meaningful connection/session behavior between baseline and gateway
* warm-up
* at least five trials per configuration
* fixed duration or request count
* throughput
* p50
* p95
* p99
* error rate
* timeout rate
* absolute gateway overhead
* percentage gateway overhead
* denied-request latency
* machine/software versions
* median results
* variability
* raw machine-readable output
* policy-evaluation latency separately where practical

### Phase 7 Exit Criteria

Running:

```bash
python -m benchmarks.run
```

produces a reproducible report whose methodology is documented in:

```text
docs/benchmark-methodology.md
```

Do not optimize until the initial baseline exists.

Do not alter methodology to manufacture attractive results.

Then STOP.

---

# Phase 8 — Packaging, CI, Documentation, and Demo

Only after behavior is correct, add:

* root-level `Dockerfile`
* root-level `compose.yaml`
* one-command local demo
* GitHub Actions
* linting if useful and lightweight
* full test execution in CI
* architecture documentation
* policy-semantics documentation
* threat model
* benchmark methodology
* complete README

README must include:

* concise pitch
* architecture diagram
* quick start
* policy example
* demo
* testing instructions
* measured benchmark results
* benchmark methodology link
* threat-model summary
* explicit limitations
* explicit non-goals

Example commands must actually work:

```bash
docker compose up -d
pytest
python -m benchmarks.run
```

Do not claim:

* enterprise readiness
* distributed guarantees
* complete MCP support
* prompt-injection prevention
* production identity architecture
* protection beyond the documented threat model

### Phase 8 Exit Criteria

* clean checkout can run the demo
* all documented commands have actually been tested
* CI passes
* benchmark claims come from reproducible committed results
* documentation accurately matches implementation
* limitations are explicit
* project can be explained honestly without verbal caveats

Then STOP.

---

# Definition of Done

SentinelMCP v1 is complete only when a reproducible demonstration proves:

1. A real MCP client authenticates to SentinelMCP.
2. SentinelMCP establishes a distinct upstream MCP session.
3. Unauthorized tools are omitted from `tools/list`.
4. A client directly attempts a hidden tool anyway.
5. SentinelMCP returns a protocol-correct denial.
6. The upstream invocation counter remains zero.
7. A redacted audit record explains the denial.
8. An authorized constrained call succeeds.
9. Schema-invalid calls are rejected.
10. Type-confused calls are rejected.
11. Out-of-range argument calls are rejected.
12. Authorized argument values are forwarded unchanged.
13. Rate limiting behaves according to documented semantics.
14. Concurrent upstream executions never exceed the configured ceiling.
15. Cancellation/timeouts/errors do not leak concurrency capacity.
16. Sensitive fields remain redacted by default.
17. Direct-versus-gateway benchmark results are reproducible.
18. Documentation and README claims match actual implementation.
19. The documented limitations accurately describe the system.
20. No resume metric exists without real supporting test/benchmark output.

---

# Resume Integrity Rule

Never manufacture:

* requests/sec
* latency
* p95/p99
* concurrency
* test counts
* performance improvements
* scale claims
* security claims

Always distinguish:

```text
IMPLEMENTED
MEASURED
PLANNED
```

Planned functionality must never be described as implemented.

Architecture facts must never be presented as measured performance facts.

---

# Phase Reporting Format

At the end of every phase, respond exactly in this structure:

## Completed

What was implemented.

## Files Changed

Important files added or modified.

## Verification

Exact commands executed and their results.

Do not claim success without running them.

## Architecture Decisions

Important choices and reasoning.

## Security Properties Proven

List properties now backed by automated tests.

Use `N/A` for Phase 0 if appropriate.

## Known Limitations

Anything intentionally excluded or discovered.

## SDK / Protocol Notes

Any relevant MCP SDK/protocol behavior discovered during implementation.

## Proposed Commit

Provide a concise commit message.

Do not commit unless I approve it.

## Next Phase

State the next phase briefly.

Then STOP and wait for:

```text
continue
```

---

# Your Task Right Now

Begin with **Phase 0 only**.

Before writing code:

1. inspect the current workspace
2. inspect git status
3. inspect applicable repository instructions
4. report what already exists
5. inspect the actual current official MCP Python SDK/FastMCP documentation and APIs available to you
6. verify the current supported Streamable HTTP server/client APIs
7. select the MCP SDK version to pin
8. identify the corresponding protocol/specification version
9. identify any conflicts between this specification and actual SDK behavior
10. give me a concise Phase 0 implementation plan

Then:

11. implement Phase 0
12. run the upstream MCP server
13. connect with a real MCP SDK client over Streamable HTTP
14. initialize the session
15. list the real tools and schemas
16. execute the safe tools
17. run the integration test
18. prove the exit criteria
19. report using the required phase-report format
20. STOP

Do NOT implement authentication, authorization, discovery filtering, policy constraints, rate limiting, concurrency enforcement, audit logging, benchmarking infrastructure beyond the Phase 0 fixture tools, Docker, CI, or later-phase functionality yet.

Do not continue until I explicitly say:

```text
continue
```
