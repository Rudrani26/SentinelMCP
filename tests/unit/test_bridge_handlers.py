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

from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp import types

from sentinelmcp.gateway.bridge import make_call_tool_handler, make_list_tools_handler
from sentinelmcp.gateway.identity import authenticated_as
from sentinelmcp.policy.models import PolicyConfig

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
    handler = make_call_tool_handler(POLICY)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": 1})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_awaited_once_with("allowed.tool", {"x": 1})
    assert not result.is_error


async def test_call_tool_denies_when_upstream_does_not_actually_have_the_tool():
    """Policy allows it, but the upstream reports a different tool set - still
    "unknown", the same as an unauthorized call, and never forwarded."""
    upstream = _upstream_with_tool("some.other.tool")
    handler = make_call_tool_handler(POLICY)
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
    handler = make_call_tool_handler(POLICY)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": "not-an-integer"})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error
    assert "Schema-invalid" in result.content[0].text


async def test_call_tool_rejects_argument_policy_violation_without_reaching_upstream():
    upstream = _upstream_with_tool("constrained.tool")
    handler = make_call_tool_handler(POLICY)
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
    handler = make_call_tool_handler(POLICY)
    params = types.CallToolRequestParams(name="constrained.tool", arguments={"database": "staging"})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    assert result.is_error
    assert "Schema-invalid" in result.content[0].text
    assert "Argument policy violation" not in result.content[0].text


async def test_call_tool_forwards_constrained_call_with_exact_values_unchanged():
    upstream = _upstream_with_tool("constrained.tool")
    upstream.call_tool.return_value = types.CallToolResult(content=[], is_error=False)
    handler = make_call_tool_handler(POLICY)
    params = types.CallToolRequestParams(name="constrained.tool", arguments={"database": "staging", "limit": 50})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_awaited_once_with("constrained.tool", {"database": "staging", "limit": 50})
    assert not result.is_error


async def test_call_tool_denies_without_reaching_upstream():
    upstream = AsyncMock()
    handler = make_call_tool_handler(POLICY)
    params = types.CallToolRequestParams(name="denied.tool", arguments={})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error


async def test_call_tool_denies_tool_with_no_policy_rule_without_reaching_upstream():
    upstream = AsyncMock()
    handler = make_call_tool_handler(POLICY)
    params = types.CallToolRequestParams(name="unmentioned.tool", arguments={})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error


async def test_call_tool_denies_for_unknown_principal_without_reaching_upstream():
    upstream = AsyncMock()
    handler = make_call_tool_handler(POLICY)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})

    with authenticated_as("no-such-agent"):
        result = await handler(_fake_ctx(upstream), params)

    upstream.call_tool.assert_not_awaited()
    assert result.is_error
