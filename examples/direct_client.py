"""Phase 0 direct MCP client.

Connects a real MCP SDK client directly to the upstream demo server (no
SentinelMCP involved) over Streamable HTTP, to prove:

- session initialization
- tools/list with real input schemas
- a successful tools/call

`mode="legacy"` pins the connection to the classic initialize-handshake
protocol era (session-based), which is the era SentinelMCP's architecture is
built around. See docs/architecture.md (added in a later phase) for why this
is pinned rather than left on the SDK's default 'auto' negotiation.

Run directly (with examples/upstream_server.py already running):

    python -m examples.direct_client --port 8765
"""

from __future__ import annotations

import argparse
import asyncio

from mcp import Client


async def run(url: str) -> None:
    async with Client(url, mode="legacy") as client:
        print(f"negotiated protocol version: {client.protocol_version}")
        print(f"server info: {client.server_info}")

        tools = await client.list_tools()
        print(f"discovered {len(tools.tools)} tools:")
        for tool in tools.tools:
            print(f"  - {tool.name}: {tool.input_schema}")

        result = await client.call_tool("benchmark.noop", {})
        print(f"benchmark.noop -> {result.content}")

        result = await client.call_tool(
            "database.query_stats",
            {"database": "staging", "limit": 10, "include_query_text": False},
        )
        print(f"database.query_stats -> {result.content}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Phase 0 direct MCP client demo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/mcp"
    asyncio.run(run(url))


if __name__ == "__main__":
    main()
