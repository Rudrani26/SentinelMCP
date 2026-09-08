"""Unit tests for the bridge handlers, isolated from any real transport,
session, or HTTP-level authentication.

These pin down, at the code level: (1) authorization is re-evaluated on
every call from `current_principal()` and a `PolicyConfig`, never from
anything downstream-session-specific; (2) an authorized call forwards
exactly `(name, arguments)` upstream, nothing more; (3) a denied call never
reaches the upstream client at all. The integration tests in
tests/integration/test_phase2_auth.py prove the same invariants
behaviorally, over real transports.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types

from sentinelmcp.gateway.bridge import make_call_tool_handler, make_list_tools_handler
from sentinelmcp.gateway.identity import authenticated_as
from sentinelmcp.gateway.limits import ConcurrencyLimiter, RateLimiter
from sentinelmcp.policy.models import PolicyConfig
from sentinelmcp.telemetry.audit import AuditLogger

_NULL_AUDIT_LOGGER = AuditLogger(os.devnull)

POLICY = PolicyConfig.model_validate(
    {
        "principals": {
            "agent": {
                "api_key_env": "SOME_ENV_VAR",
                "tools": {
                    "allowed.tool": {"effect": "allow", "deny_unknown_arguments": False},
                    "denied.tool": {"effect": "deny"},
                    "constrained.tool": {
                        "effect": "allow",
                        "required": ["database"],
                        "arguments": {"database": {"in": ["staging", "production"]}, "limit": {"min": 1, "max": 100}},
                    },
                },
            }
        }
    }
)

_OPEN_SCHEMA = {"type": "object"}


def _fake_ctx(upstream: AsyncMock) -> SimpleNamespace:
    """A stand-in for ServerRequestContext exposing only what the bridge reads."""
    return SimpleNamespace(lifespan_context=upstream)


def _upstream_with_tool(name: str, input_schema: dict | None = _OPEN_SCHEMA) -> AsyncMock:
    """An upstream mock whose tools/list reports one tool named `name`."""
    upstream = AsyncMock()
    upstream.list_tools.return_value = types.ListToolsResult(
        tools=[types.Tool(name=name, input_schema=input_schema)]
    )
    return upstream


def _make_handler(policy: PolicyConfig = POLICY, *, upstream_call_timeout_seconds: float | None = None):
    """A handler with fresh, generous rate/concurrency limits - these tests
    are about authorization/schema/policy, not limits (see test_limits.py)."""
    rate_limiter = RateLimiter(capacity=1000.0, refill_rate=1000.0)
    concurrency_limiter = ConcurrencyLimiter(max_concurrent=1000)
    return make_call_tool_handler(
        policy,
        rate_limiter,
        concurrency_limiter,
        _NULL_AUDIT_LOGGER,
        upstream_call_timeout_seconds=upstream_call_timeout_seconds,
    )


async def test_list_tools_filters_to_only_authorized_tools():
    upstream = AsyncMock()
    upstream.list_tools.return_value = types.ListToolsResult(
        tools=[
            types.Tool(name="allowed.tool", input_schema={"type": "object", "properties": {}}),
            types.Tool(name="denied.tool", input_schema={"type": "object", "properties": {}}),
            types.Tool(name="unmentioned.tool", input_schema={"type": "object", "properties": {}}),
        ]
    )
    handler = make_list_tools_handler(POLICY)

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), None)

    assert [t.name for t in result.tools] == ["allowed.tool"]


async def test_call_tool_forwards_authorized_call_unchanged():
    upstream = _upstream_with_tool("allowed.tool")
    upstream.call_tool.return_value = types.CallToolResult(content=[], is_error=False)
    handler = _make_handler()
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": 1})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_awaited_once_with("allowed.tool", {"x": 1})
    assert not result.is_error


async def test_call_tool_denies_when_upstream_does_not_actually_have_the_tool():
    """Policy allows it, but the upstream reports a different tool set - still
    "unknown", the same as an unauthorized call, and never forwarded."""
    upstream = _upstream_with_tool("some.other.tool")
    handler = _make_handler()
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error


async def test_call_tool_rejects_schema_invalid_arguments_without_reaching_upstream():
    upstream = _upstream_with_tool(
        "allowed.tool",
        input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    handler = _make_handler()
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": "not-an-integer"})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error
    assert "Schema-invalid" in result.content[0].text


async def test_call_tool_rejects_argument_policy_violation_without_reaching_upstream():
    upstream = _upstream_with_tool("constrained.tool")
    handler = _make_handler()
    # "database" is required and constrained to {staging, production}.
    params = types.CallToolRequestParams(name="constrained.tool", arguments={"database": "prod-typo"})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error
    assert "Argument policy violation" in result.content[0].text


async def test_schema_and_policy_rejections_are_distinguishable():
    upstream = _upstream_with_tool(
        "constrained.tool",
        input_schema={"type": "object", "properties": {"database": {"type": "string"}}, "required": ["nonexistent"]},
    )
    handler = _make_handler()
    params = types.CallToolRequestParams(name="constrained.tool", arguments={"database": "staging"})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    assert result.is_error
    assert "Schema-invalid" in result.content[0].text
    assert "Argument policy violation" not in result.content[0].text


async def test_call_tool_forwards_constrained_call_with_exact_values_unchanged():
    upstream = _upstream_with_tool("constrained.tool")
    upstream.call_tool.return_value = types.CallToolResult(content=[], is_error=False)
    handler = _make_handler()
    params = types.CallToolRequestParams(name="constrained.tool", arguments={"database": "staging", "limit": 50})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_awaited_once_with("constrained.tool", {"database": "staging", "limit": 50})
    assert not result.is_error


async def test_call_tool_denies_without_reaching_upstream():
    upstream = AsyncMock()
    handler = _make_handler()
    params = types.CallToolRequestParams(name="denied.tool", arguments={})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error


async def test_call_tool_denies_tool_with_no_policy_rule_without_reaching_upstream():
    upstream = AsyncMock()
    handler = _make_handler()
    params = types.CallToolRequestParams(name="unmentioned.tool", arguments={})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error


async def test_call_tool_denies_for_unknown_principal_without_reaching_upstream():
    upstream = AsyncMock()
    handler = _make_handler()
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})

    with authenticated_as("no-such-agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error


# --- rate limiting -----------------------------------------------------------


async def test_rate_limited_call_is_rejected_without_reaching_upstream():
    upstream = _upstream_with_tool("allowed.tool")
    upstream.call_tool.return_value = types.CallToolResult(content=[], is_error=False)
    rate_limiter = RateLimiter(capacity=1, refill_rate=0.0001)
    concurrency_limiter = ConcurrencyLimiter(max_concurrent=1000)
    handler = make_call_tool_handler(POLICY, rate_limiter, concurrency_limiter, _NULL_AUDIT_LOGGER)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": 1})

    with authenticated_as("agent"):
        first = await handler(_fake_ctx(upstream), params)
        second = await handler(_fake_ctx(upstream), params)

    assert not first.is_error
    assert second.is_error
    assert "Rate limit" in second.content[0].text
    upstream.call_tool.assert_awaited_once()


async def test_rate_limit_is_consumed_even_for_a_call_later_denied_by_policy():
    """Every authenticated, structurally valid attempt consumes a token -
    including one this same pipeline goes on to deny."""
    upstream = AsyncMock()
    rate_limiter = RateLimiter(capacity=1, refill_rate=0.0001)
    concurrency_limiter = ConcurrencyLimiter(max_concurrent=1000)
    handler = make_call_tool_handler(POLICY, rate_limiter, concurrency_limiter, _NULL_AUDIT_LOGGER)
    params = types.CallToolRequestParams(name="denied.tool", arguments={})

    with authenticated_as("agent"):
        first = await handler(_fake_ctx(upstream), params)
        assert first.is_error and "Unknown tool" in first.content[0].text

        second = await handler(_fake_ctx(upstream), params)

    # The bucket is exhausted even though the first call was denied by
    # policy, not because it succeeded.
    assert second.is_error
    assert "Rate limit" in second.content[0].text


# --- concurrency ---------------------------------------------------------------


async def test_concurrency_rejected_call_never_reaches_upstream():
    upstream = _upstream_with_tool("allowed.tool")
    concurrency_limiter = ConcurrencyLimiter(max_concurrent=1)
    await concurrency_limiter.try_acquire("agent")  # occupy the only slot
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), concurrency_limiter, _NULL_AUDIT_LOGGER)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": 1})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error
    assert "Concurrency limit" in result.content[0].text


async def test_concurrency_capacity_is_released_after_upstream_exception():
    upstream = _upstream_with_tool("allowed.tool")
    upstream.call_tool.side_effect = RuntimeError("upstream blew up")
    concurrency_limiter = ConcurrencyLimiter(max_concurrent=1)
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), concurrency_limiter, _NULL_AUDIT_LOGGER)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": 1})

    with authenticated_as("agent"), pytest.raises(RuntimeError):
        await handler(_fake_ctx(upstream), params)

    assert await concurrency_limiter.active_count("agent") == 0


async def test_concurrency_capacity_is_released_after_timeout():
    upstream = _upstream_with_tool("allowed.tool")

    async def slow_call_tool(*_args, **_kwargs):
        await asyncio.sleep(10)

    upstream.call_tool.side_effect = slow_call_tool
    concurrency_limiter = ConcurrencyLimiter(max_concurrent=1)
    handler = make_call_tool_handler(
        POLICY,
        RateLimiter(1000.0, 1000.0),
        concurrency_limiter,
        _NULL_AUDIT_LOGGER,
        upstream_call_timeout_seconds=0.05,
    )
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": 1})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    assert result.is_error
    assert "Upstream timeout" in result.content[0].text
    assert await concurrency_limiter.active_count("agent") == 0


async def test_concurrency_capacity_is_released_after_cancellation():
    upstream = _upstream_with_tool("allowed.tool")
    upstream_call_started = asyncio.Event()

    async def slow_call_tool(*_args, **_kwargs):
        upstream_call_started.set()
        await asyncio.sleep(10)

    upstream.call_tool.side_effect = slow_call_tool
    concurrency_limiter = ConcurrencyLimiter(max_concurrent=1)
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), concurrency_limiter, _NULL_AUDIT_LOGGER)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": 1})

    with authenticated_as("agent"):
        task = asyncio.create_task(handler(_fake_ctx(upstream), params))
        await upstream_call_started.wait()  # deterministic: concurrency is acquired before this fires
        assert await concurrency_limiter.active_count("agent") == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await concurrency_limiter.active_count("agent") == 0
