"""Phase 0 exit-criteria test: a real MCP SDK client against a real MCP SDK
server, over real (loopback) Streamable HTTP.

No SentinelMCP code exists yet; this proves the upstream fixture server and
the SDK's client/server pairing behave correctly before anything is built on
top of them.
"""

from __future__ import annotations

from mcp import Client

from examples.upstream_server import build_server, counters
from tests.support import running_asgi_app

EXPECTED_TOOL_NAMES = {
    "database.query_stats",
    "database.execute_write",
    "benchmark.noop",
    "benchmark.fixed_latency",
}


async def test_direct_streamable_http_round_trip():
    initial_noop = counters.noop
    initial_query_stats = counters.query_stats

    server = build_server()
    app = server.streamable_http_app(host="127.0.0.1")
    async with running_asgi_app(app) as url:
        async with Client(url, mode="legacy") as client:
            # Session initialization succeeded (mode="legacy" == initialize handshake).
            assert client.protocol_version
            assert client.server_info is not None
            assert client.server_info.name == "sentinelmcp-upstream-demo"

            # tools/list returns real schemas for all four demo tools.
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert tool_names == EXPECTED_TOOL_NAMES

            query_stats_tool = next(t for t in tools.tools if t.name == "database.query_stats")
            assert query_stats_tool.input_schema is not None
            assert "database" in query_stats_tool.input_schema["properties"]
            assert "limit" in query_stats_tool.input_schema["properties"]

            # A safe tools/call succeeds end-to-end.
            noop_result = await client.call_tool("benchmark.noop", {})
            assert not noop_result.is_error

            query_result = await client.call_tool(
                "database.query_stats",
                {"database": "staging", "limit": 10, "include_query_text": False},
            )
            assert not query_result.is_error

    # The upstream tool's own invocation counters observed exactly one call each,
    # proving the calls actually reached the real tool implementation.
    assert counters.noop == initial_noop + 1
    assert counters.query_stats == initial_query_stats + 1
