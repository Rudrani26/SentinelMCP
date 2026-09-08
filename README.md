# SentinelMCP

A Python authorization gateway for MCP (Model Context Protocol) tool
servers. SentinelMCP sits between an MCP client and one configured upstream
MCP server per gateway instance: it authenticates callers, filters tool
discovery, independently authorizes every tool call against a default-deny
policy, validates arguments against both the upstream tool's own schema and
its own constraints, enforces per-principal rate and concurrency limits, and
emits redacted, correlated audit records - all over real Streamable HTTP,
with no paid APIs, cloud services, or real LLM required to build, run, test,
or benchmark it.

This is a portfolio project. Correctness, protocol compatibility, security
reasoning, tests, and reproducible measurements matter more than feature
count - see [Limitations](#limitations) and [Non-goals](#non-goals) below
for what this deliberately does not claim.

## Architecture

```text
                         TRUST BOUNDARY
                              |
                              v
+----------------+     +---------------------------+     +----------------+
|                |     |                           |     |                |
|  MCP Client    | --> |       SentinelMCP         | --> |  Upstream MCP  |
|                |     |                           |     |     Server     |
+----------------+     |  BearerAuthMiddleware     |     +-------+--------+
                       |  Rate limiter             |             |
 Downstream MCP        |  Policy engine            |             v
 Session               |  Concurrency limiter      |           Tools
 (own session ID)      |  Audit logger             |
                       +---------------------------+
                                   |
                          Separate upstream
                         MCP session (own ID),
                          held for the gateway's
                            whole process lifetime
```

SentinelMCP is not a byte-forwarding proxy: it terminates a real MCP server
role toward the downstream client and independently holds a real MCP client
role toward the upstream server - two distinct, provably separate protocol
sessions. Full detail: [`docs/architecture.md`](docs/architecture.md).

## Quick start

Requires Python 3.12+.

```bash
git clone https://github.com/Rudrani26/SentinelMCP.git
cd SentinelMCP
python -m venv .venv
.venv/Scripts/activate   # or: source .venv/bin/activate  (macOS/Linux)
pip install -e ".[dev]"

pytest                        # 178 tests
python -m benchmarks.run      # direct-vs-gateway benchmark, ~a few minutes
```

Or with Docker (starts the upstream fixture server and the gateway together,
using the bundled example policy):

```bash
docker compose up -d
python -m examples.demo       # from the host, against the running gateway
```

## Policy example

From [`sentinelmcp/policy/policies/example.yaml`](sentinelmcp/policy/policies/example.yaml)
(the file `docker compose up -d` actually runs with):

```yaml
principals:
  diagnostics-agent:
    api_key_env: SENTINELMCP_DIAGNOSTICS_AGENT_API_KEY
    tools:
      database.query_stats:
        effect: allow
        deny_unknown_arguments: true
        required: [database, limit]
        arguments:
          database:
            in: [staging, production]
          limit:
            min: 1
            max: 100
          include_query_text:
            equals: false

      # Explicit deny for readability; behaviorally identical to omitting
      # this tool entirely (both deny).
      database.execute_write:
        effect: deny
```

No plaintext key material ever appears in a policy file - `api_key_env`
names an environment variable the operator sets separately. Full semantics
(every operator, strict type rules, nested paths, startup validation):
[`docs/policy-semantics.md`](docs/policy-semantics.md).

## Demo

`python -m examples.demo` (against a running gateway - either
`docker compose up -d`, or the two processes below) shows, against a real
gateway over real Streamable HTTP:

```text
=== diagnostics-agent: authorized, constrained call ===
tools visible to diagnostics-agent: ['database.query_stats', 'benchmark.noop', 'benchmark.fixed_latency']

--- permitted call succeeds ---
is_error=False
{
  "database": "staging",
  "limit": 10,
  "include_query_text": false,
  "rows": []
}

--- schema-invalid: limit as a string ---
is_error=True
Schema-invalid arguments for database.query_stats: 'ten' is not of type 'integer'

--- policy-invalid: database not in the allowed set ---
is_error=True
Argument policy violation for database.query_stats: 'database' is not one of the allowed values

--- hidden tool, denied identically to an unknown one ---
is_error=True
Unknown tool: database.execute_write

=== readonly-agent: a narrower policy, same gateway ===
tools visible to readonly-agent: ['benchmark.noop']

=== credential failures ===
missing credentials: rejected, as expected
unknown credentials: rejected, as expected
```

Without Docker, run the two processes directly:

```bash
export SENTINELMCP_DIAGNOSTICS_AGENT_API_KEY=demo-only-not-a-real-secret-1
export SENTINELMCP_READONLY_AGENT_API_KEY=demo-only-not-a-real-secret-2

python -m examples.upstream_server --port 8765 &
python -m sentinelmcp.gateway.server \
    --upstream-url http://127.0.0.1:8765/mcp \
    --policy sentinelmcp/policy/policies/example.yaml \
    --port 8080 &

python -m examples.demo --host 127.0.0.1 --port 8080
```

## Testing

```bash
pytest              # everything: unit + integration
pytest tests/unit    # fast, no real network
pytest tests/integration   # real loopback Streamable HTTP servers
```

178 tests (unit + integration), including two Hypothesis property tests
(no argument-policy mutation across arbitrary type-confused inputs; no
sensitive-value leak through redaction at any nesting depth or key-casing
variant). Every one of the 18 numbered security invariants in this
project's build instructions maps to at least one automated test - see the
Phase 6 build log and [`docs/adversarial-tests.md`](docs/adversarial-tests.md)
for the full mapping.

## Benchmark results

One real, committed baseline run (loopback Streamable HTTP, direct
client->upstream vs. client->SentinelMCP->upstream), 5 trials per
configuration with a discarded warm-up each, 0% errors and 0% timeouts
across all 27,900 requests in the run:

**Environment**: Windows-11-10.0.26200-SP0, Intel64 8 cores, CPython 3.14.2,
mcp 2.2.0, uvicorn 0.52.4, httpx2 2.12.0, jsonschema 4.26.0.

| Tool | Concurrency | Direct p50 | Gateway p50 | Overhead |
|---|---|---|---|---|
| noop | 1 | 6.3ms | 30.9ms | +24.6ms (+389%) |
| noop | 10 | 79.6ms | 329.9ms | +250.2ms (+314%) |
| noop | 25 | 217.9ms | 838.9ms | +621.0ms (+285%) |
| noop | 50 | 476.4ms | 1730.0ms | +1253.6ms (+263%) |
| noop | 100 | 998.9ms | 3513.2ms | +2514.3ms (+252%) |
| fixed_latency (10ms) | 1 | 32.3ms | 60.9ms | +28.6ms (+89%) |
| fixed_latency (10ms) | 100 | 927.9ms | 3572.8ms | +2644.9ms (+285%) |

Policy-evaluation latency alone (from the same run's audit log, 27,900
records): **p50=0.98ms, p95=2.23ms, p99=2.77ms** - a small, stable fraction
of total gateway latency; the bulk of the overhead is elsewhere (a leading
suspect, not yet investigated further: the gateway's per-call upstream
`tools/list` round trip for schema lookup - see
[Limitations](#limitations)).

Both paths' throughput plateaus by concurrency=10 in this environment (a
single-machine, sandboxed-VM benchmark - not a production capacity claim);
per CLAUDE.md's own instruction, concurrency was not pushed past 100 once
that plateau was evident. Full raw results:
[`benchmarks/results/`](benchmarks/results/). Full methodology, including
exactly what "overhead" does and doesn't claim:
[`docs/benchmark-methodology.md`](docs/benchmark-methodology.md).

## Threat model summary

SentinelMCP protects a trusted upstream MCP server from: missing/invalid
authentication, an authenticated-but-overprivileged or malfunctioning
client, unauthorized tool calls, out-of-policy arguments, excessive call
frequency, excessive concurrent upstream executions, malformed protocol
input, and accidental sensitive-field disclosure through audit logs.

It does **not** protect against: a malicious/compromised upstream server,
compromise of the gateway host, stolen valid API keys,
unauthenticated-traffic DoS, prompt injection that produces a
policy-compliant call, harmful behavior inside an authorized tool, leakage
through a tool's own legitimate output, or distributed rate-limit evasion
across multiple gateway processes. Full detail, including assets, actors,
and assumptions: [`docs/threat-model.md`](docs/threat-model.md).

## Limitations

- Single-process, in-memory rate limiting and concurrency control - no
  distributed guarantee across multiple gateway processes; state resets on
  restart.
- No hot policy reload - a policy change requires restarting the gateway.
- `deny_unknown_arguments` inspects only top-level argument names; the
  upstream's own `inputSchema` (checked first) is what's expected to catch
  a nested unknown field when the schema restricts it.
- The gateway re-fetches the upstream's tool list on every `tools/call`
  (no caching) specifically to validate arguments against its schema - a
  real, measured contributor to gateway latency, not addressed in v1
  (no optimization was attempted without first measuring - see the
  benchmark methodology).
- Application-layer only: TLS/transport security is the deployment
  environment's responsibility, not implemented here.
- Benchmark numbers are from one sandboxed development VM, not dedicated
  hardware - illustrative of relative (direct vs. gateway) behavior, not a
  production capacity claim.

## Non-goals

Explicitly out of scope for v1 (see `docs/threat-model.md` for why):
prompt-injection prevention, a production identity architecture (no OAuth/
OIDC/SSO/JWT - Bearer API keys only, real key material from environment
variables), multiple upstream servers or dynamic routing, distributed
rate-limiting/concurrency guarantees, hot policy reload, wildcard/regex
tool selectors, anomaly detection, agent-quality evaluation, circuit
breakers, automatic retries of side-effecting tools, Kubernetes or cloud
deployment, a full observability stack, any required real/paid LLM or API,
RAG or vector databases, a frontend, or enterprise-readiness claims of any
kind.

## Project layout

```text
sentinelmcp/
├── gateway/       # downstream server, upstream client, auth, rate/concurrency limits
├── policy/        # policy models, engine, and the bundled example policy
└── telemetry/     # structured JSONL audit records
examples/          # upstream fixture server, direct client, demo script
benchmarks/        # direct-vs-gateway benchmark harness and committed results
tests/
├── unit/          # no real network
└── integration/   # real loopback Streamable HTTP servers
docs/              # architecture, policy semantics, benchmark methodology, threat model
```
