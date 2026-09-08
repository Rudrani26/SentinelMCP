"""Phase 2 exit-criteria tests: Bearer API-key authentication and
default-deny, exact-name tool authorization.

Two principals are configured (in conftest.py's shared policy):
`diagnostics-agent` (three allowed tools plus an explicit deny) and
`readonly-agent` (one allowed tool), so discovery filtering and call-time
authorization can be shown to differ per principal.

Uses the shared session-scoped `gateway_url` fixture from conftest.py.
"""

from __future__ import annotations

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from examples.upstream_server import counters
from tests.integration.conftest import DIAG_KEY, READONLY_KEY
from tests.support import authed_client, client_with_authorization_header, expect_mcp_error

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_valid_credentials_resolve_to_correct_principal_and_filter_discovery(gateway_url: str):
    async with authed_client(gateway_url, DIAG_KEY) as diagnostics_client:
        tools = await diagnostics_client.list_tools()
        assert {t.name for t in tools.tools} == {
            "database.query_stats",
            "benchmark.noop",
            "benchmark.fixed_latency",
        }

    async with authed_client(gateway_url, READONLY_KEY) as readonly_client:
        tools = await readonly_client.list_tools()
        assert {t.name for t in tools.tools} == {"benchmark.noop"}


async def test_missing_credentials_rejected(gateway_url: str):
    with expect_mcp_error():
        async with authed_client(gateway_url, None) as client:
            await client.list_tools()


@pytest.mark.parametrize(
    "header_value",
    [
        "Bearer",  # no token at all (also covers the "empty token" case - "Bearer "
        # with a trailing space is not testable through a real HTTP client:
        # httpx2 itself refuses to send a header value with trailing
        # whitespace, raising LocalProtocolError before any request is sent)
        "Basic dGVzdDp0ZXN0",  # wrong scheme entirely
        f"bearer {DIAG_KEY}",  # lowercase scheme - a real key, wrong shape
    ],
)
async def test_malformed_credentials_rejected(gateway_url: str, header_value: str):
    with expect_mcp_error():
        async with client_with_authorization_header(gateway_url, header_value) as client:
            await client.list_tools()


async def test_unknown_credentials_rejected(gateway_url: str):
    """A well-formed Bearer token that matches no configured principal."""
    with expect_mcp_error():
        async with authed_client(gateway_url, "not-a-configured-key") as client:
            await client.list_tools()


async def test_caller_cannot_spoof_principal_via_arbitrary_header(gateway_url: str):
    """An arbitrary identity-looking header alongside a *valid* but different
    principal's key must have zero effect: the principal is resolved from the
    Authorization value alone."""
    headers = {
        "Authorization": f"Bearer {READONLY_KEY}",
        "X-Sentinel-Principal": "diagnostics-agent",
    }
    async with httpx2.AsyncClient(headers=headers) as http_client:
        async with Client(streamable_http_client(gateway_url, http_client=http_client), mode="legacy") as client:
            tools = await client.list_tools()
            # Still readonly-agent's tool list, not diagnostics-agent's - the
            # spoofing header was never consulted.
            assert {t.name for t in tools.tools} == {"benchmark.noop"}


async def test_hidden_tool_direct_invocation_is_denied_and_never_reaches_upstream(gateway_url: str):
    initial_query_stats = counters.query_stats

    async with authed_client(gateway_url, READONLY_KEY) as client:
        # readonly-agent has no rule for database.query_stats at all (it isn't
        # even visible in this principal's tools/list) - call it directly anyway.
        result = await client.call_tool("database.query_stats", {"database": "staging", "limit": 1})
        assert result.is_error

    # The denied call never reached the real upstream tool implementation.
    assert counters.query_stats == initial_query_stats


async def test_explicit_deny_behaves_like_default_deny_and_never_reaches_upstream(gateway_url: str):
    initial_execute_write = counters.execute_write

    async with authed_client(gateway_url, DIAG_KEY) as client:
        # diagnostics-agent has an *explicit* deny rule for this tool.
        result = await client.call_tool(
            "database.execute_write", {"database": "staging", "statement": "DELETE FROM x"}
        )
        assert result.is_error

    assert counters.execute_write == initial_execute_write


async def test_authorized_call_still_succeeds_end_to_end(gateway_url: str):
    """Sanity check that Phase 2's auth/authz layer doesn't break the
    legitimately-authorized path proven in Phase 1."""
    initial_noop = counters.noop

    async with authed_client(gateway_url, READONLY_KEY) as client:
        result = await client.call_tool("benchmark.noop", {})
        assert not result.is_error

    assert counters.noop == initial_noop + 1
