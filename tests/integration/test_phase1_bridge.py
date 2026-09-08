"""Phase 1 bridging invariants (tools/list and tools/call bridging, upstream
error/capability handling, downstream/upstream session distinctness),
re-verified now that Phase 2's authentication and authorization sit in front
of the bridge.

Every connection here is made by a principal fully authorized for every
tool, so these tests isolate the *bridging* behavior from the *authorization*
behavior (covered separately in test_phase2_auth.py) - filtering is not what
these tests are about, but it can no longer be bypassed, so every client here
authenticates like any other caller would have to.

Uses the shared session-scoped `gateway_url` fixture from conftest.py.
"""

from __future__ import annotations

import mcp_types
import pytest
from mcp import MCPError

from examples.upstream_server import counters
from tests.integration.conftest import ALLOW_ALL_KEY
from tests.support import authed_client

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXPECTED_TOOL_NAMES = {
    "database.query_stats",
    "database.execute_write",
    "benchmark.noop",
    "benchmark.fixed_latency",
}


async def test_gateway_bridges_initialization_and_tool_listing(gateway_url: str):
    async with authed_client(gateway_url, ALLOW_ALL_KEY) as client:
        assert client.protocol_version
        assert client.server_info is not None
        assert client.server_info.name == "sentinelmcp-gateway"

        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == EXPECTED_TOOL_NAMES

        # Only the tools capability was registered on the gateway, so only
        # the tools capability should be negotiated.
        assert client.server_capabilities.tools is not None
        assert client.server_capabilities.resources is None
        assert client.server_capabilities.prompts is None


async def test_gateway_bridges_successful_tool_call(gateway_url: str):
    initial_noop = counters.noop

    async with authed_client(gateway_url, ALLOW_ALL_KEY) as client:
        result = await client.call_tool("benchmark.noop", {})
        assert not result.is_error

    # The call reached the real upstream tool implementation, not a stub.
    assert counters.noop == initial_noop + 1


async def test_gateway_bridges_representative_upstream_error(gateway_url: str):
    """Calling a tool the upstream server does not have surfaces as a
    protocol-correct tool-level error (CallToolResult.is_error=True),
    not silently swallowed or crashed on. Note this principal is not even
    authorized for "no.such.tool" either way - see test_phase2_auth.py for
    the policy-denial case, which is deliberately shaped identically."""
    async with authed_client(gateway_url, ALLOW_ALL_KEY) as client:
        result = await client.call_tool("no.such.tool", {})
        assert result.is_error


async def test_gateway_rejects_unsupported_capability_protocol_correctly(gateway_url: str):
    """The gateway advertises no resources capability; a client that calls
    resources/list anyway gets a protocol-correct error, not a hang or crash."""
    async with authed_client(gateway_url, ALLOW_ALL_KEY) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.list_resources()
        assert exc_info.value.code == mcp_types.METHOD_NOT_FOUND


async def test_downstream_and_upstream_sessions_are_distinct(gateway_url: str):
    """Two independent downstream sessions are both served by the gateway's
    single, longer-lived upstream session: closing the first downstream
    session has no effect on the second, and the shared upstream tool
    invocation counter reflects both calls. This is only possible if the
    downstream session(s) and the upstream session are genuinely separate -
    a downstream session is never treated as if it were the upstream one.
    """
    initial_noop = counters.noop

    async with authed_client(gateway_url, ALLOW_ALL_KEY) as client_a:
        result_a = await client_a.call_tool("benchmark.noop", {})
        assert not result_a.is_error
    # client_a's downstream session is now fully terminated (DELETE sent
    # on __aexit__). A brand new downstream session must still work.
    async with authed_client(gateway_url, ALLOW_ALL_KEY) as client_b:
        result_b = await client_b.call_tool("benchmark.noop", {})
        assert not result_b.is_error

    # Both distinct downstream sessions' calls reached the one shared upstream
    # session and its one real tool implementation.
    assert counters.noop == initial_noop + 2
