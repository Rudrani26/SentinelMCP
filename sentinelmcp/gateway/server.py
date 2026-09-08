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
from sentinelmcp.gateway.limits import ConcurrencyLimiter, RateLimiter
from sentinelmcp.policy.models import PolicyConfig, load_policy
from sentinelmcp.telemetry.audit import DEFAULT_REDACTION_MODE, AuditLogger, RedactionMode

# Defaults for a local demo; see sentinelmcp/gateway/limits.py for semantics.
# Deliberately generous - v1's point is to prove the enforcement mechanism
# works and is race-free, not to model a specific production traffic shape.
DEFAULT_RATE_LIMIT_CAPACITY = 20.0
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 5.0
DEFAULT_MAX_CONCURRENT_PER_PRINCIPAL = 10
DEFAULT_AUDIT_LOG_PATH = "sentinelmcp-audit.jsonl"


def build_gateway_server(
    upstream_url: str,
    policy: PolicyConfig,
    *,
    rate_limiter: RateLimiter,
    concurrency_limiter: ConcurrencyLimiter,
    audit_logger: AuditLogger,
    upstream_call_timeout_seconds: float | None = None,
) -> Server[Client]:
    """Construct the gateway's low-level Server, bridged to `upstream_url` and
    enforcing `policy`, `rate_limiter`, and `concurrency_limiter`.

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
        on_call_tool=make_call_tool_handler(
            policy,
            rate_limiter,
            concurrency_limiter,
            audit_logger,
            upstream_call_timeout_seconds=upstream_call_timeout_seconds,
        ),
    )


def build_gateway_app(
    upstream_url: str,
    policy: PolicyConfig,
    *,
    host: str = "127.0.0.1",
    rate_limiter: RateLimiter | None = None,
    concurrency_limiter: ConcurrencyLimiter | None = None,
    audit_logger: AuditLogger | None = None,
    audit_log_path: str = DEFAULT_AUDIT_LOG_PATH,
    audit_redaction_mode: RedactionMode = DEFAULT_REDACTION_MODE,
    upstream_call_timeout_seconds: float | None = None,
) -> ASGIApp:
    """The full gateway ASGI app: Bearer authentication wrapping the MCP server.

    `rate_limiter`/`concurrency_limiter`/`audit_logger` default to fresh
    instances with this module's defaults; pass explicit ones (e.g. with an
    injectable clock, tighter limits, or a temp-file audit path) for tests.
    """
    principal_key_env = {name: p.api_key_env for name, p in policy.principals.items()}
    resolver = IdentityResolver(principal_key_env)

    if rate_limiter is None:
        rate_limiter = RateLimiter(DEFAULT_RATE_LIMIT_CAPACITY, DEFAULT_RATE_LIMIT_REFILL_PER_SECOND)
    if concurrency_limiter is None:
        concurrency_limiter = ConcurrencyLimiter(DEFAULT_MAX_CONCURRENT_PER_PRINCIPAL)
    if audit_logger is None:
        audit_logger = AuditLogger(audit_log_path, redaction_mode=audit_redaction_mode)

    server = build_gateway_server(
        upstream_url,
        policy,
        rate_limiter=rate_limiter,
        concurrency_limiter=concurrency_limiter,
        audit_logger=audit_logger,
        upstream_call_timeout_seconds=upstream_call_timeout_seconds,
    )
    mcp_app = server.streamable_http_app(host=host)
    return BearerAuthMiddleware(mcp_app, resolver)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SentinelMCP gateway.")
    parser.add_argument("--upstream-url", required=True, help="e.g. http://127.0.0.1:8765/mcp")
    parser.add_argument("--policy", required=True, help="Path to a policy YAML file.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--rate-limit-capacity", type=float, default=DEFAULT_RATE_LIMIT_CAPACITY)
    parser.add_argument("--rate-limit-refill-per-second", type=float, default=DEFAULT_RATE_LIMIT_REFILL_PER_SECOND)
    parser.add_argument("--max-concurrent-per-principal", type=int, default=DEFAULT_MAX_CONCURRENT_PER_PRINCIPAL)
    parser.add_argument("--upstream-call-timeout-seconds", type=float, default=None)
    parser.add_argument("--audit-log-path", default=DEFAULT_AUDIT_LOG_PATH)
    parser.add_argument(
        "--audit-redaction-mode", choices=["none", "keys_only", "redacted", "full"], default=DEFAULT_REDACTION_MODE
    )
    args = parser.parse_args()

    policy = load_policy(args.policy)
    app = build_gateway_app(
        args.upstream_url,
        policy,
        host=args.host,
        rate_limiter=RateLimiter(args.rate_limit_capacity, args.rate_limit_refill_per_second),
        concurrency_limiter=ConcurrencyLimiter(args.max_concurrent_per_principal),
        audit_logger=AuditLogger(args.audit_log_path, redaction_mode=args.audit_redaction_mode),
        upstream_call_timeout_seconds=args.upstream_call_timeout_seconds,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
