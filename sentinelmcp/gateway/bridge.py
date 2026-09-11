"""The bridge between the downstream MCP server role and the upstream MCP
client role: authorized `tools/list` filtering and independently authorized
`tools/call`, forwarding whatever the upstream returns unchanged.

Handlers read the upstream connection from `ctx.lifespan_context` (see
`sentinelmcp.gateway.server.build_gateway_server`), never from anything
downstream-session-specific, and read the caller's principal from
`current_principal()` (resolved by `BearerAuthMiddleware` before this code
ever runs) rather than from any downstream-supplied field. Authorization is
evaluated fresh on every `tools/list` and `tools/call` - hiding a tool from
discovery is not treated as authorization; a direct call to a hidden tool is
independently denied here, before the upstream is ever contacted.

Every `tools/call` attempt that reaches `handle_call_tool` produces exactly
one terminal `AuditRecord` (see sentinelmcp/telemetry/audit.py), regardless
of which pipeline stage it exits at, and the response carries the same
correlation ID (in `CallToolResult.meta`) so a caller, a log line, and the
audit record for one attempt can all be tied together without exposing
credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from mcp import Client, types
from mcp.server.context import ServerRequestContext

from sentinelmcp.gateway.identity import current_principal
from sentinelmcp.gateway.limits import ConcurrencyLimiter, RateLimiter
from sentinelmcp.policy.engine import (
    evaluate_argument_policy,
    is_tool_allowed,
    resolve_tool_policy,
    validate_input_schema,
)
from sentinelmcp.policy.models import PolicyConfig
from sentinelmcp.telemetry.audit import AuditLogger, AuditOutcome, AuditRecord, LatencyTimer, new_correlation_id

ListToolsHandler = Callable[
    [ServerRequestContext[Client], types.PaginatedRequestParams | None], Awaitable[types.ListToolsResult]
]
CallToolHandler = Callable[[ServerRequestContext[Client], types.CallToolRequestParams], Awaitable[types.CallToolResult]]


class UpstreamToolCache:
    """Caches the upstream's `tools/list` result for the lifetime of one
    upstream connection.

    This gateway holds exactly one upstream `Client` connection for the
    whole process (see `build_gateway_server`'s `lifespan`), shared across
    every downstream session - so "cached for this connection's lifetime"
    and "cached gateway-wide" are the same scope here, not two different
    designs to choose between.

    Race-safe under concurrent first callers: `get()` holds the lock across
    the fetch itself, so a cold cache never issues more than one real
    `tools/list` round trip even if many calls arrive at once - later
    callers simply wait for the lock and then see the now-populated cache,
    rather than each starting their own redundant fetch.

    There is currently no code path in this gateway that reconnects a
    dropped upstream connection - `lifespan` opens the `Client` once and
    the gateway process does not attempt to re-establish it. So there is no
    "stale after reconnect" scenario to handle automatically today.
    `invalidate()` exists as an explicit, testable seam for an operator or a
    future reconnect implementation to force a re-fetch; nothing currently
    calls it on its own. See docs/architecture.md for the full reasoning,
    including why a TTL or a `tools/list_changed` notification subscriber
    was deliberately not added.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tools: list[types.Tool] | None = None

    async def get(self, upstream: Client) -> list[types.Tool]:
        async with self._lock:
            if self._tools is None:
                result = await upstream.list_tools()
                self._tools = result.tools
            return self._tools

    async def invalidate(self) -> None:
        async with self._lock:
            self._tools = None


def _denied_result(tool_name: str) -> types.CallToolResult:
    # Deliberately worded and shaped exactly like the upstream SDK's own
    # "unknown tool" tool-level error (see mcp.server.mcpserver.tools.tool_manager),
    # so a caller cannot distinguish "this tool exists but is not authorized
    # for you" from "this tool does not exist" - hiding a tool from discovery
    # is not a security boundary, but it shouldn't be an *information*
    # boundary either: a denied call and an unknown-tool call should look
    # the same from the outside.
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"Unknown tool: {tool_name}")],
        is_error=True,
    )


def _schema_invalid_result(tool_name: str, reason: str) -> types.CallToolResult:
    # Distinguishable from _denied_result (unauthorized/unknown) and from
    # _policy_invalid_result (a SentinelMCP-specific constraint), per the
    # Phase 3 exit criterion that schema and policy rejections must be
    # distinguishable from each other and from upstream results.
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"Schema-invalid arguments for {tool_name}: {reason}")],
        is_error=True,
    )


def _policy_invalid_result(tool_name: str, reason: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"Argument policy violation for {tool_name}: {reason}")],
        is_error=True,
    )


def _rate_limited_result(tool_name: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"Rate limit exceeded for {tool_name}")],
        is_error=True,
    )


def _concurrency_rejected_result(tool_name: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"Concurrency limit exceeded for {tool_name}")],
        is_error=True,
    )


def _upstream_timeout_result(tool_name: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"Upstream timeout for {tool_name}")],
        is_error=True,
    )


def _upstream_error_result(tool_name: str, reason: str) -> types.CallToolResult:
    # Mirrors _upstream_timeout_result: an upstream failure (connection
    # dropped, upstream crashed mid-call, etc.) is still surfaced to the
    # downstream caller as a well-formed, protocol-correct CallToolResult -
    # not as a raised exception that would tear down the caller's own
    # transport-level stream with a raw, undistinguishable error (verified
    # empirically: before this, a real killed-upstream test showed the
    # downstream client receiving `MCPError("SSE stream ended without a
    # response")` instead of a clean tool-level error).
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"Upstream error for {tool_name}: {reason}")],
        is_error=True,
    )


def make_list_tools_handler(policy: PolicyConfig, tool_cache: UpstreamToolCache | None = None) -> ListToolsHandler:
    """Build an on_list_tools handler that filters to the caller's allowed tools.

    `tool_cache` defaults to a private cache scoped to this one handler
    closure if not given; pass the same `UpstreamToolCache` instance used by
    `make_call_tool_handler` (as `build_gateway_server` does) so `tools/list`
    and `tools/call` share one cached fetch rather than each maintaining
    their own.
    """
    cache = tool_cache if tool_cache is not None else UpstreamToolCache()

    async def handle_list_tools(
        ctx: ServerRequestContext[Client], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        upstream: Client = ctx.lifespan_context
        principal = current_principal()
        tools = await cache.get(upstream)
        allowed = [tool for tool in tools if is_tool_allowed(policy, principal, tool.name)]
        return types.ListToolsResult(tools=allowed)

    return handle_list_tools


def make_call_tool_handler(
    policy: PolicyConfig,
    rate_limiter: RateLimiter,
    concurrency_limiter: ConcurrencyLimiter,
    audit_logger: AuditLogger,
    *,
    upstream_call_timeout_seconds: float | None = None,
    tool_cache: UpstreamToolCache | None = None,
) -> CallToolHandler:
    """Build an on_call_tool handler that independently authorizes every call.

    Pipeline (see docs/policy-semantics.md and sentinelmcp/gateway/limits.py):

    1. rate limit - every authenticated, structurally valid attempt consumes
       a token, *including* one this pipeline goes on to deny below
    2. tool authorization - including confirming the upstream still actually
       has the tool, via the cached `tools/list` result (see `tool_cache`
       and `UpstreamToolCache`)
    3. upstream inputSchema validation
    4. SentinelMCP argument-constraint evaluation
    5. concurrency ceiling - acquired only for a call that survived 1-4,
       immediately before contacting the upstream, and always released
    6. forward the arguments *unchanged*

    `allow` never bypasses schema validation or argument constraints; each
    stage's rejection is worded distinguishably from the others, and exactly
    one terminal `AuditRecord` is emitted no matter which stage the call
    exits at (including an unexpected exception at any stage).

    `tool_cache` defaults to a private cache scoped to this one handler
    closure if not given; pass the same instance used by
    `make_list_tools_handler` (as `build_gateway_server` does) so both
    handlers share one cached fetch.
    """
    cache = tool_cache if tool_cache is not None else UpstreamToolCache()

    async def handle_call_tool(
        ctx: ServerRequestContext[Client], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        correlation_id = new_correlation_id()
        principal = current_principal()
        request = getattr(ctx, "request", None)
        session_id = request.headers.get("mcp-session-id") if request is not None else None
        tool_name = params.name
        arguments = params.arguments or {}
        total_timer = LatencyTimer()

        rate_limit_result: str = "failed"
        schema_result: str = "not_evaluated"
        policy_result: str = "not_evaluated"
        concurrency_result: str = "not_evaluated"
        matched_rule: str | None = None
        policy_latency_ms: float | None = None
        upstream_latency_ms: float | None = None
        audit_emitted = False

        async def emit(
            outcome: AuditOutcome, *, upstream_attempted: bool = False, upstream_result_category: str | None = None
        ) -> None:
            nonlocal audit_emitted
            await audit_logger.emit(
                AuditRecord(
                    correlation_id=correlation_id,
                    principal=principal,
                    session_id=session_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    outcome=outcome,
                    matched_rule=matched_rule,
                    schema_result=schema_result,
                    policy_result=policy_result,
                    rate_limit_result=rate_limit_result,
                    concurrency_result=concurrency_result,
                    upstream_attempted=upstream_attempted,
                    upstream_result_category=upstream_result_category,
                    policy_latency_ms=policy_latency_ms,
                    upstream_latency_ms=upstream_latency_ms,
                    total_latency_ms=total_timer.elapsed_ms(),
                )
            )
            audit_emitted = True

        def finalize(result: types.CallToolResult) -> types.CallToolResult:
            return result.model_copy(update={"meta": {**(result.meta or {}), "correlation_id": correlation_id}})

        try:
            if not await rate_limiter.try_acquire(principal):
                await emit(AuditOutcome.RATE_LIMITED)
                return finalize(_rate_limited_result(tool_name))
            rate_limit_result = "passed"

            tool_policy = resolve_tool_policy(policy, principal, tool_name)
            principal_policy = policy.principals.get(principal)
            if principal_policy is not None and principal_policy.tools.get(tool_name) is not None:
                matched_rule = tool_name
            if tool_policy is None:
                policy_result = "failed"
                await emit(AuditOutcome.POLICY_DENIED)
                return finalize(_denied_result(tool_name))

            upstream: Client = ctx.lifespan_context
            upstream_tools = await cache.get(upstream)
            upstream_tool = next((t for t in upstream_tools if t.name == tool_name), None)
            if upstream_tool is None:
                # Allowed by policy, but the upstream doesn't actually have it -
                # still "unknown", for the same reason a denied call is.
                policy_result = "failed"
                await emit(AuditOutcome.POLICY_DENIED)
                return finalize(_denied_result(tool_name))

            policy_timer = LatencyTimer()
            schema_violation = validate_input_schema(upstream_tool.input_schema, arguments)
            if schema_violation is not None:
                schema_result = "failed"
                policy_latency_ms = policy_timer.elapsed_ms()
                await emit(AuditOutcome.SCHEMA_REJECTED)
                return finalize(_schema_invalid_result(tool_name, schema_violation))
            schema_result = "passed"

            policy_violation = evaluate_argument_policy(tool_policy, arguments)
            policy_latency_ms = policy_timer.elapsed_ms()
            if policy_violation is not None:
                policy_result = "failed"
                await emit(AuditOutcome.POLICY_DENIED)
                return finalize(_policy_invalid_result(tool_name, policy_violation))
            policy_result = "passed"

            async with concurrency_limiter.acquire(principal) as acquired:
                if not acquired:
                    concurrency_result = "failed"
                    await emit(AuditOutcome.CONCURRENCY_REJECTED)
                    return finalize(_concurrency_rejected_result(tool_name))
                concurrency_result = "passed"

                upstream_timer = LatencyTimer()
                try:
                    if upstream_call_timeout_seconds is None:
                        result = await upstream.call_tool(tool_name, arguments)
                    else:
                        result = await asyncio.wait_for(
                            upstream.call_tool(tool_name, arguments), timeout=upstream_call_timeout_seconds
                        )
                except TimeoutError:
                    upstream_latency_ms = upstream_timer.elapsed_ms()
                    await emit(
                        AuditOutcome.UPSTREAM_TIMEOUT, upstream_attempted=True, upstream_result_category="timeout"
                    )
                    return finalize(_upstream_timeout_result(tool_name))
                except asyncio.CancelledError:
                    upstream_latency_ms = upstream_timer.elapsed_ms()
                    await emit(AuditOutcome.CANCELLED, upstream_attempted=True, upstream_result_category="cancelled")
                    raise
                except Exception as exc:
                    upstream_latency_ms = upstream_timer.elapsed_ms()
                    await emit(AuditOutcome.UPSTREAM_ERROR, upstream_attempted=True, upstream_result_category="error")
                    return finalize(_upstream_error_result(tool_name, str(exc)))

                upstream_latency_ms = upstream_timer.elapsed_ms()
                await emit(AuditOutcome.UPSTREAM_SUCCESS, upstream_attempted=True, upstream_result_category="success")
                return finalize(result)
        except asyncio.CancelledError:
            if not audit_emitted:
                await emit(AuditOutcome.CANCELLED)
            raise
        except Exception:
            if not audit_emitted:
                await emit(AuditOutcome.INTERNAL_ERROR)
            raise

    return handle_call_tool
