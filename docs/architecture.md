# Architecture

This describes SentinelMCP's actual implementation. It does not describe
planned functionality; see `docs/threat-model.md` for what is explicitly out
of scope.

## Overview

```text
                         TRUST BOUNDARY
                              |
                              v
+----------------+     +---------------------------+     +----------------+
|                |     |                           |     |                |
|  MCP Client    | --> |       SentinelMCP         | --> |  Upstream MCP  |
|                |     |                           |     |     Server     |
+----------------+     |  BearerAuthMiddleware     |     +-------+--------+
                       |  (identity.py)            |             |
 Downstream MCP        |  Rate limiter (limits.py) |             v
 Session               |  Policy engine (policy/)  |           Tools
 (Streamable HTTP,     |  Concurrency limiter      |
  own session ID)      |  Audit logger (telemetry/)|
                       +---------------------------+
                                   |
                          Separate upstream
                         MCP session (own ID),
                         held for the gateway's
                            whole process lifetime
```

SentinelMCP is **not a byte-forwarding proxy**. It terminates a real MCP
server role toward the downstream client and independently holds a real MCP
client role toward the upstream server. These are two distinct MCP protocol
sessions - see "Downstream and upstream sessions" below for exactly how
distinctness is implemented and proven.

## Downstream session

- Implemented with the official SDK's low-level `Server`
  (`mcp.server.lowlevel.server.Server`), not the `MCPServer`/`FastMCP`
  decorator API - the gateway needs to bridge whatever tools the upstream
  *actually* has at request time, not a statically-declared set.
- Served over Streamable HTTP (`Server.streamable_http_app()`), wrapped by
  `BearerAuthMiddleware` (a plain ASGI middleware, not the SDK's OAuth
  machinery - see "Authentication" below).
- Only `on_list_tools` and `on_call_tool` handlers are registered, so the
  SDK's own capability derivation advertises only the `tools` capability -
  no resources, prompts, logging, or completions.
- Negotiates the classic session-based `initialize` handshake era
  (`2025-11-25`, the SDK's newest handshake-era protocol revision) - see
  "SDK protocol era" below for why this is pinned rather than left to the
  SDK's default negotiation.

## Upstream session

- One `mcp.Client(upstream_url, mode="legacy")` connection, opened once when
  the gateway's `lifespan` starts and closed when it stops - i.e. once per
  gateway *process*, not once per downstream connection.
- Shared across every downstream session. A downstream session's own
  initialize/terminate lifecycle is completely independent of it: closing
  one downstream session has no effect on the shared upstream session, and
  a brand-new downstream session immediately reuses the same one.
- `mode="legacy"` is set explicitly for the same reason as the downstream
  side - see "SDK protocol era".

## Downstream and upstream sessions are distinct

This is proven, not just asserted:

- `tests/integration/test_phase1_bridge.py::test_downstream_and_upstream_sessions_are_distinct`
  opens two independent downstream sessions in sequence; both are served by
  the *same* shared upstream session, and terminating the first has no
  effect on the second's or the third's ability to call tools.
- The bridge code (`sentinelmcp/gateway/bridge.py`) never reads a
  downstream-session-derived value and forwards it upstream as if it were
  the same session - the upstream `Client` object is read from
  `ctx.lifespan_context` (the gateway process's own state), never from
  anything on the downstream request.

## Trust boundary

The single trust boundary is the SentinelMCP process itself: everything
between an authenticated downstream client and the point where a request is
forwarded to the upstream server is inside SentinelMCP's control. The
upstream server itself is *trusted* (see the threat model - SentinelMCP does
not defend against a malicious or compromised upstream).

## Request pipeline (`tools/call`)

Implemented in `sentinelmcp/gateway/bridge.py::make_call_tool_handler`, in
this exact order:

1. **Authentication** (`BearerAuthMiddleware`, before any MCP-level
   processing) - resolves `Authorization: Bearer <key>` to a principal via
   a SHA-256 digest lookup (`sentinelmcp/gateway/identity.py`). Missing,
   malformed, or unknown credentials get an HTTP 401 with a JSON-RPC-shaped
   body, before `initialize` or any other method is ever reached.
2. **Rate limit** (`sentinelmcp/gateway/limits.py::RateLimiter`) - a
   per-principal token bucket. Every authenticated, structurally valid
   attempt consumes a token, *including* one this same pipeline goes on to
   deny below.
3. **Tool authorization** (`sentinelmcp/policy/engine.py::resolve_tool_policy`)
   - exact tool name, default-deny. A denied call (no rule, explicit deny,
   unknown principal, or a tool the upstream doesn't actually have) is
   shaped identically to a genuinely nonexistent tool
   (`"Unknown tool: <name>"`), so hiding a tool from discovery is not an
   information boundary either.
4. **Upstream schema validation**
   (`sentinelmcp/policy/engine.py::validate_input_schema`) - the caller's
   arguments validated against the upstream tool's own `inputSchema`, via a
   standards-compliant JSON Schema validator.
5. **SentinelMCP argument-constraint evaluation**
   (`sentinelmcp/policy/engine.py::evaluate_argument_policy`) - the exact
   same, uncoerced arguments evaluated against `equals`/`in`/`min`/`max`/
   length/pattern/required/forbidden/nested-path constraints (see
   `docs/policy-semantics.md`).
6. **Concurrency ceiling**
   (`sentinelmcp/gateway/limits.py::ConcurrencyLimiter`) - acquired only for
   a call that survived every stage above, immediately before the upstream
   is contacted, and always released (success, exception, timeout,
   cancellation - a `finally` block via an async context manager).
7. **Forward to upstream** - the exact arguments the caller sent, unchanged,
   via the one shared upstream `Client`.

At every exit point, exactly one terminal `AuditRecord` is emitted (step 8,
below) before the response returns.

## Tool discovery (`tools/list`)

`sentinelmcp/gateway/bridge.py::make_list_tools_handler` fetches the
upstream's real tool list on every request (no caching), preserving each
tool's real name/description/`inputSchema` unchanged, and filters to only
the tools the authenticated principal is allowed to call. Hiding a tool from
this list is explicitly **not** a security boundary - `tools/call`
independently re-checks authorization for every call, including one for a
tool never shown in discovery.

## Authentication

`sentinelmcp/gateway/identity.py`. Plain Bearer-token validation (not
OAuth): real key material comes from environment variables, referenced by
name in policy configuration, never as plaintext in the file itself. A
presented token is compared as a SHA-256 digest via a dict lookup - not a
loop over each configured principal's raw key - so no comparison's timing
depends on how many principals are configured or which one (if any)
matches. Ambiguous configuration (two principals resolving to the same key)
is a startup error, not a runtime ambiguity.

## Rate limiting and concurrency control

`sentinelmcp/gateway/limits.py`. Single-process, in-memory only - state is
lost on restart and never shared across gateway processes. See that
module's docstring and `docs/policy-semantics.md`'s neighbor documents for
full documented semantics (bucket capacity, refill rate, burst behavior,
concurrency-rejection-is-immediate-not-queued).

## Audit lifecycle

`sentinelmcp/telemetry/audit.py`. Every `tools/call` attempt that reaches
the handler produces exactly one terminal JSON-line audit record,
regardless of which pipeline stage it exits at (including an unexpected
exception, guarded against double-emission). Each record carries a unique
correlation ID, which is also stamped into the response's `meta` field, so
a caller, a log line, and the audit record for one attempt can be tied
together without ever exposing a credential to do so. Concurrent writes are
serialized behind one `asyncio.Lock`. Argument values are redacted by
default (recursive, name-based, casing-insensitive) - see the module's own
docstring and `docs/adversarial-tests.md`'s redaction rows for the exact
behavior.

## SDK protocol era

The installed SDK (`mcp==2.2.0`) supports both the classic, session-based
`initialize`-handshake protocol eras (`2024-11-05` through `2025-11-25`) and
a newer, **sessionless** per-request-envelope era (`2026-07-28`). Since
SentinelMCP's whole architecture assumes distinct, session-based downstream
and upstream sessions, `mode="legacy"` is pinned explicitly wherever
SentinelMCP acts as a client, forcing the classic handshake rather than
risking the SDK's default auto-negotiation silently landing on the
sessionless era. Whether the gateway's own downstream-facing side should
also *reject* a client's attempt to negotiate into the sessionless era
remains an open, unresolved question, identified during initial SDK
research and never revisited, since no test client in this project's own
test suite triggers it.
