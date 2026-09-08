"""The SentinelMCP gateway: a downstream-facing MCP server, authenticated and
authorized, bridged to one upstream MCP server.

SentinelMCP is not a byte-forwarding proxy: it terminates its own downstream
MCP session (via the SDK's low-level `Server` + Streamable HTTP session
manager, wrapped in `BearerAuthMiddleware`) and separately holds one upstream
MCP client session for the lifetime of the gateway process (via `Server`'s
`lifespan`). The two are distinct protocol sessions, bridged only through
`sentinelmcp.gateway.bridge`.

Only `tools/list` and `tools/call` handlers are registered, so the gateway
advertises (and the SDK negotiates) only the `tools` capability - no
resources, prompts, logging, or completions.
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from mcp import Client
from mcp.server.lowlevel.server import Server
from starlette.types import ASGIApp

from sentinelmcp.gateway.bridge import make_call_tool_handler, make_list_tools_handler
from sentinelmcp.gateway.identity import BearerAuthMiddleware, IdentityResolver
from sentinelmcp.policy.models import PolicyConfig, load_policy


def build_gateway_server(upstream_url: str, policy: PolicyConfig) -> Server[Client]:
    """Construct the gateway's low-level Server, bridged to `upstream_url` and
    enforcing `policy`.

    The upstream `Client` connection is opened once, when the gateway's
    lifespan starts (i.e. once per gateway process), and closed when it
    stops. It is shared across every downstream session; each downstream
    session's own initialize/terminate lifecycle is fully independent of it.
    """

    @asynccontextmanager
    async def lifespan(_server: Server[Client]) -> AsyncIterator[Client]:
        async with Client(upstream_url, mode="legacy") as upstream_client:
            yield upstream_client

    return Server(
        "sentinelmcp-gateway",
        version="0.0.1",
        lifespan=lifespan,
        on_list_tools=make_list_tools_handler(policy),
        on_call_tool=make_call_tool_handler(policy),
    )


def build_gateway_app(upstream_url: str, policy: PolicyConfig, *, host: str = "127.0.0.1") -> ASGIApp:
    """The full gateway ASGI app: Bearer authentication wrapping the MCP server."""
    principal_key_env = {name: p.api_key_env for name, p in policy.principals.items()}
    resolver = IdentityResolver(principal_key_env)

    server = build_gateway_server(upstream_url, policy)
    mcp_app = server.streamable_http_app(host=host)
    return BearerAuthMiddleware(mcp_app, resolver)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SentinelMCP gateway.")
    parser.add_argument("--upstream-url", required=True, help="e.g. http://127.0.0.1:8765/mcp")
    parser.add_argument("--policy", required=True, help="Path to a policy YAML file.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    policy = load_policy(args.policy)
    app = build_gateway_app(args.upstream_url, policy, host=args.host)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
