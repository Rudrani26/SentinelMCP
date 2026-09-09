# Threat Model

This describes SentinelMCP's actual v1 implementation - what it protects
against, what it explicitly does not, and why. It is not a general-purpose
security architecture document; it is scoped to exactly what this project
builds.

## Purpose

SentinelMCP constrains which MCP tool actions can reach a protected upstream
MCP server, even when a downstream client or AI agent behaves unexpectedly
or has been manipulated. It does **not** claim to prevent prompt injection -
an agent that has been manipulated into *requesting* a policy-compliant,
authorized tool call with in-policy arguments will still have that call
succeed. SentinelMCP's job is to make sure that whatever gets through is
something the caller was actually allowed to do, with arguments that were
actually allowed - not to reason about the caller's intent.

## Assets

- The upstream MCP server and the tools/data it exposes.
- API key material (real values live only in environment variables; never
  logged, audited, or returned in any response).
- Audit records themselves, once written (they may contain, even after
  redaction, information about what was attempted).

## Actors

- **A legitimate, correctly-behaving downstream client**, holding a valid
  API key, calling tools it's authorized for with valid arguments.
- **An authenticated-but-overprivileged or malfunctioning client** - has a
  valid key, but attempts calls or arguments outside its authorization
  (whether through a bug, a misconfigured agent, or a manipulated prompt).
- **An unauthenticated network client** - no valid key at all.
- **The upstream MCP server** - trusted, not modeled as an adversary (see
  "Threats not addressed").
- **The gateway operator** - configures policy, identity bindings, and
  limits; SentinelMCP does not defend against a malicious operator.

## Trust boundaries

One boundary: the SentinelMCP process itself. A request is "inside" once
authenticated; the upstream server is trusted once a request reaches it (see
`docs/architecture.md`'s diagram).

## Threats addressed

Each of these has automated tests - see `docs/adversarial-tests.md` for the
category-to-test mapping and the full 18-invariant mapping.

- Missing or invalid caller authentication (`BearerAuthMiddleware`, fails
  closed on missing/malformed/unknown/ambiguous credentials).
- An authenticated but overprivileged or malfunctioning client (default-deny,
  exact-name tool authorization; every call independently re-authorized;
  hiding a tool from discovery is never treated as authorization).
- Unauthorized tool calls (same as above).
- Arguments that violate configured constraints (upstream `inputSchema`
  validation, then SentinelMCP's own argument-constraint evaluation; both
  use strict, non-coercing type semantics).
- Excessive tool-call frequency (per-principal token-bucket rate limiting).
- Excessive concurrent upstream executions (per-principal concurrency
  ceiling, proven race-free under 100+ simultaneous attempts, released on
  success/exception/timeout/cancellation).
- Malformed protocol requests that would otherwise crash the gateway
  (invalid JSON, non-JSON-RPC bodies, unsupported methods, wrong param
  types, malformed/repeated request identifiers, deeply nested garbage -
  the gateway stays responsive after each).
- Accidental disclosure of configured sensitive fields through audit logs
  (recursive, name-based redaction, default-on, tested with a Hypothesis
  property test for leak-freedom at any nesting depth or key-casing
  variant).

## Threats not addressed

Stated explicitly, per CLAUDE.md's requirement to document these
boundaries honestly - none of the following are claimed:

- A malicious or compromised upstream MCP server. SentinelMCP trusts the
  upstream's responses and tool schemas (a malformed schema is handled
  *safely* - it can't crash the gateway - but a schema that's valid and
  malicious is not distinguished from a benign one).
- Compromise of the gateway host or process itself.
- Stolen valid API keys (a stolen key is, by definition, treated as its
  legitimate holder).
- Denial-of-service from unauthenticated network traffic (rate limiting
  applies only after authentication; an unauthenticated flood is a
  transport/infrastructure concern outside this project).
- Prompt injection that produces a policy-compliant call. SentinelMCP
  constrains *actions*, not *intent* - explicitly, always.
- Harmful behavior inside an authorized tool's own implementation.
- Leakage through a legitimate tool's own result content (SentinelMCP
  forwards upstream results unchanged; it does not inspect or filter tool
  *output*).
- Distributed rate-limit or concurrency evasion across multiple gateway
  processes (both are explicitly single-process, in-memory only - see
  `sentinelmcp/gateway/limits.py`).
- Rollback of any upstream side effect a permitted call causes.
- Semantic risks that cannot be expressed by configured policy (e.g., "this
  combination of otherwise-individually-valid calls is suspicious" - v1's
  policy language has no cross-call state).
- Vulnerabilities in the MCP SDK or any other dependency.
- Network-layer attacks outside the application boundary (TLS termination,
  DNS, etc. - the deployment environment's responsibility; see
  "Transport security" below).

## Assumptions

- The deployment environment supplies TLS/transport security; SentinelMCP's
  v1 application layer does not implement or claim to provide it. All
  loopback communication in this project's tests, demo, and benchmarks is
  plain HTTP.
- The upstream MCP server is genuinely trusted, per the operator's own
  judgment - not verified or sandboxed by SentinelMCP.
- Real API key material is provisioned via environment variables by
  whoever deploys the gateway; SentinelMCP has no key-management or
  rotation feature.
- A single gateway process serves a single upstream server (CLAUDE.md's
  explicit v1 scope) - no dynamic upstream routing or multi-upstream
  fan-out exists.

## Known limitations (beyond the explicit non-goals above)

- No hot policy reload - a policy change requires restarting the gateway.
- `deny_unknown_arguments` inspects only top-level argument names; a nested
  unknown field is not itself flagged by the policy engine (the upstream
  schema layer, checked first, is where that's expected to be caught when
  the schema restricts it - see `docs/policy-semantics.md`).
- The gateway re-fetches the upstream's tool list on every `tools/call` (no
  caching) to validate arguments against its schema - a measured, real
  contributor to gateway latency (see `docs/benchmark-methodology.md`), not
  a security gap, but worth knowing.
- Rate-limit and concurrency state resets on gateway restart (in-memory
  only, by design for v1).
- Whether the gateway's downstream-facing side should reject a client's
  attempt to negotiate the SDK's newer sessionless protocol era is an open
  question, never resolved in v1 (see `docs/architecture.md`'s final
  section) - no test client in this project's suite ever exercises it.
