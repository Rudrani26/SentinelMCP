"""Shared test helpers: run a real Streamable HTTP ASGI app on a loopback,
OS-assigned port for the duration of a test.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator

import httpx2
import pytest
import uvicorn
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp


async def _wait_until_serving(mcp_url: str, *, attempts: int = 50, delay_seconds: float = 0.02) -> None:
    """Block until `mcp_url` (the Streamable HTTP endpoint itself) actually
    answers an HTTP request.

    `uvicorn.Server.started` flips True once its own startup lifecycle
    completes, but occasionally a connection made immediately afterward still
    fails (observed as a dropped/incomplete SSE response on the very first
    real request) - the listen socket is accepting, but the Streamable HTTP
    session manager's own internal task group isn't fully warmed up yet. A
    real request/response round trip against the *actual* MCP route is a
    stronger readiness signal than a bare TCP or unrelated-route probe: a GET
    to "/" would succeed without ever touching the session manager at all.
    """
    async with httpx2.AsyncClient() as probe:
        last_exc: Exception | None = None
        for _ in range(attempts):
            try:
                await probe.get(mcp_url)
                return
            except httpx2.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(delay_seconds)
        raise RuntimeError(f"{mcp_url} never became reachable") from last_exc


@contextlib.asynccontextmanager
async def running_asgi_app(app: ASGIApp, *, mcp_path: str = "/mcp") -> AsyncIterator[str]:
    """Serve `app` over real loopback HTTP on an ephemeral port; yield its MCP endpoint URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        mcp_url = f"http://127.0.0.1:{port}{mcp_path}"
        await _wait_until_serving(mcp_url)
        yield mcp_url
    finally:
        server.should_exit = True
        await serve_task


@contextlib.asynccontextmanager
async def client_with_authorization_header(url: str, header_value: str | None) -> AsyncIterator[Client]:
    """A real `mcp.Client(mode="legacy")` connection sending `header_value` verbatim
    as its Authorization header (or no header at all if `header_value is None`).

    `Client`'s own URL-string convenience path has no way to set request headers,
    so this connects via an explicit `streamable_http_client(..., http_client=...)`
    transport instead - still official SDK surface, just the lower layer.
    """
    headers = {"Authorization": header_value} if header_value is not None else {}
    async with httpx2.AsyncClient(headers=headers) as http_client:
        async with Client(streamable_http_client(url, http_client=http_client), mode="legacy") as client:
            yield client


def authed_client(url: str, api_key: str | None) -> AsyncIterator[Client]:
    """A well-formed `Authorization: Bearer <api_key>` connection.

    `api_key=None` connects with no Authorization header at all (for testing
    missing-credential rejection).
    """
    header_value = f"Bearer {api_key}" if api_key is not None else None
    return client_with_authorization_header(url, header_value)


@contextlib.contextmanager
def expect_mcp_error() -> Iterator[None]:
    """Assert an `MCPError` occurs anywhere inside the wrapped block.

    A failure raised while *connecting* (inside `Client.__aenter__`) surfaces
    as a `BaseExceptionGroup` from the SDK's internal anyio task groups, not a
    bare `MCPError` - `pytest.raises(MCPError)` would not match it. This
    covers both shapes.
    """
    with pytest.raises((MCPError, BaseExceptionGroup)) as exc_info:
        yield
    if isinstance(exc_info.value, BaseExceptionGroup):
        assert exc_info.group_contains(MCPError)
