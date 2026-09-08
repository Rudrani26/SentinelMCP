"""Phase 0 upstream MCP server.

A minimal, harmless upstream MCP server exposing four demo tools over
Streamable HTTP, used to prove that a real MCP SDK client can complete a
protocol-correct round trip against a real MCP SDK server. This server has
no knowledge of SentinelMCP; it plays the role of "the protected upstream"
in later phases.

Run directly:

    python -m examples.upstream_server --port 8765

Import for tests:

    from examples.upstream_server import build_server, counters
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from mcp.server.mcpserver import MCPServer


@dataclass
class InvocationCounters:
    """Observable per-tool invocation counts, for tests to assert against."""

    query_stats: int = 0
    execute_write: int = 0
    noop: int = 0
    fixed_latency: int = 0


counters = InvocationCounters()


def build_server() -> MCPServer:
    """Construct the upstream MCPServer with its four demo tools registered."""
    server = MCPServer("sentinelmcp-upstream-demo", version="0.0.1")

    @server.tool(name="database.query_stats")
    def query_stats(database: str, limit: int, include_query_text: bool = False) -> dict:
        """Return fake query statistics for a database (harmless demo tool)."""
        counters.query_stats += 1
        return {
            "database": database,
            "limit": limit,
            "include_query_text": include_query_text,
            "rows": [],
        }

    @server.tool(name="database.execute_write")
    def execute_write(database: str, statement: str) -> dict:
        """Simulate executing a write statement (harmless demo tool; no real side effect)."""
        counters.execute_write += 1
        return {"database": database, "statement": statement, "rows_affected": 0}

    @server.tool(name="benchmark.noop")
    def noop() -> dict:
        """Return immediately; used to approximate transport/session overhead."""
        counters.noop += 1
        return {"ok": True}

    @server.tool(name="benchmark.fixed_latency")
    async def fixed_latency(duration_seconds: float) -> dict:
        """Wait for a known duration, then return."""
        counters.fixed_latency += 1
        await asyncio.sleep(duration_seconds)
        return {"ok": True, "duration_seconds": duration_seconds}

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Phase 0 upstream MCP demo server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = build_server()
    server.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
