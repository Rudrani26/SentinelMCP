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
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from mcp import Client, types
from mcp.server.context import ServerRequestContext

from sentinelmcp.gateway.identity import current_principal
from sentinelmcp.policy.engine import (
    evaluate_argument_policy,
    is_tool_allowed,
    resolve_tool_policy,
    validate_input_schema,
)
from sentinelmcp.policy.models import PolicyConfig

ListToolsHandler = Callable[
    [ServerRequestContext[Client], types.PaginatedRequestParams | None], Awaitable[types.ListToolsResult]
]
CallToolHandler = Callable[[ServerRequestContext[Client], types.CallToolRequestParams], Awaitable[types.CallToolResult]]


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


def make_list_tools_handler(policy: PolicyConfig) -> ListToolsHandler:
    """Build an on_list_tools handler that filters to the caller's allowed tools."""

    async def handle_list_tools(
        ctx: ServerRequestContext[Client], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        upstream: Client = ctx.lifespan_context
        principal = current_principal()
        result = await upstream.list_tools()
        allowed = [tool for tool in result.tools if is_tool_allowed(policy, principal, tool.name)]
        return types.ListToolsResult(tools=allowed)

    return handle_list_tools


def make_call_tool_handler(policy: PolicyConfig) -> CallToolHandler:
    """Build an on_call_tool handler that independently authorizes every call.

    Pipeline (see docs/policy-semantics.md): tool authorization, then
    upstream inputSchema validation, then SentinelMCP argument-constraint
    evaluation, then forward the arguments *unchanged*. `allow` never
    bypasses schema validation or argument constraints; each stage's
    rejection is worded distinguishably from the others.
    """

    async def handle_call_tool(
        ctx: ServerRequestContext[Client], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        principal = current_principal()
        tool_policy = resolve_tool_policy(policy, principal, params.name)
        if tool_policy is None:
            return _denied_result(params.name)

        upstream: Client = ctx.lifespan_context
        arguments = params.arguments or {}

        upstream_tools = await upstream.list_tools()
        upstream_tool = next((t for t in upstream_tools.tools if t.name == params.name), None)
        if upstream_tool is None:
            # Allowed by policy, but the upstream doesn't actually have it -
            # still "unknown", for the same reason a denied call is.
            return _denied_result(params.name)

        schema_violation = validate_input_schema(upstream_tool.input_schema, arguments)
        if schema_violation is not None:
            return _schema_invalid_result(params.name, schema_violation)

        policy_violation = evaluate_argument_policy(tool_policy, arguments)
        if policy_violation is not None:
            return _policy_invalid_result(params.name, policy_violation)

        return await upstream.call_tool(params.name, arguments)

    return handle_call_tool
