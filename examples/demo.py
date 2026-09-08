"""SentinelMCP demo client.

Run against the gateway started by `docker compose up -d` (or any locally
running gateway using the bundled example policy,
sentinelmcp/policy/policies/example.yaml). Demonstrates, against a real
gateway over real Streamable HTTP:

- a permitted, constrained call succeeding
- a hidden tool denied on direct invocation, indistinguishable from unknown
- a schema-invalid call rejected (upstream inputSchema)
- an argument-policy-invalid call rejected (SentinelMCP constraint)
- missing/unknown credentials rejected

Run:

    python -m examples.demo
    python -m examples.demo --host localhost --port 8080
"""

from __future__ import annotations

import argparse
import asyncio

import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

DIAGNOSTICS_KEY = "demo-only-not-a-real-secret-1"
READONLY_KEY = "demo-only-not-a-real-secret-2"


def _authed_client(url: str, api_key: str | None) -> Client:
    """Connects with `Authorization: Bearer <api_key>` (or no header if None).

    `Client`'s own URL-string convenience path has no way to set request
    headers, so this goes through the lower-level `streamable_http_client`
    transport directly - still official SDK surface, not a workaround.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key is not None else {}
    http_client = httpx2.AsyncClient(headers=headers)
    return Client(streamable_http_client(url, http_client=http_client), mode="legacy")


async def _show(label: str, coro) -> None:
    print(f"\n--- {label} ---")
    try:
        result = await coro
        print(f"is_error={result.is_error}")
        for block in result.content:
            print(getattr(block, "text", block))
    except MCPError as exc:
        print(f"MCPError: {exc}")
    except Exception as exc:  # noqa: BLE001 - a demo script surfaces failures plainly
        print(f"{type(exc).__name__}: {exc}")


async def run(url: str) -> None:
    print(f"gateway: {url}")

    print("\n=== diagnostics-agent: authorized, constrained call ===")
    async with _authed_client(url, DIAGNOSTICS_KEY) as client:
        tools = await client.list_tools()
        print(f"tools visible to diagnostics-agent: {[t.name for t in tools.tools]}")

        await _show(
            "permitted call succeeds",
            client.call_tool("database.query_stats", {"database": "staging", "limit": 10, "include_query_text": False}),
        )
        await _show(
            "schema-invalid: limit as a string",
            client.call_tool("database.query_stats", {"database": "staging", "limit": "ten"}),
        )
        await _show(
            "policy-invalid: database not in the allowed set",
            client.call_tool("database.query_stats", {"database": "not-a-real-db", "limit": 10}),
        )
        await _show(
            "hidden tool, denied identically to an unknown one",
            client.call_tool("database.execute_write", {"database": "staging", "statement": "DELETE FROM x"}),
        )

    print("\n=== readonly-agent: a narrower policy, same gateway ===")
    async with _authed_client(url, READONLY_KEY) as client:
        tools = await client.list_tools()
        print(f"tools visible to readonly-agent: {[t.name for t in tools.tools]}")
        await _show("readonly-agent may call benchmark.noop", client.call_tool("benchmark.noop", {}))

    print("\n=== credential failures ===")
    try:
        async with _authed_client(url, None) as client:
            await client.list_tools()
        print("UNEXPECTED: missing credentials were accepted")
    except Exception:
        print("missing credentials: rejected, as expected")

    try:
        async with _authed_client(url, "not-a-configured-key") as client:
            await client.list_tools()
        print("UNEXPECTED: unknown credentials were accepted")
    except Exception:
        print("unknown credentials: rejected, as expected")


def main() -> None:
    parser = argparse.ArgumentParser(description="SentinelMCP demo client.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/mcp"
    asyncio.run(run(url))


if __name__ == "__main__":
    main()
