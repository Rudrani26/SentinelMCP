"""Chaos test: the upstream connection disappears while the gateway is
awaiting it.

Uses a real, separate upstream OS process, killed (SIGKILL/`TerminateProcess`)
while a real `tools/call` is in flight - the closest realistic approximation
of "the upstream connection disappears" available without inventing a fake
exception, and the same technique used to discover the two findings this
test locks in (see the module-level docstring on `_upstream_error_result` in
sentinelmcp/gateway/bridge.py for the first).

Does not share the session-scoped `gateway_url` fixture from conftest.py -
this test deliberately kills its own upstream process, which must not affect
any other test.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sentinelmcp.gateway.limits import ConcurrencyLimiter
from sentinelmcp.gateway.server import build_gateway_app
from sentinelmcp.policy.models import PolicyConfig
from sentinelmcp.telemetry.audit import AuditLogger
from tests.support import authed_client, running_asgi_app

KEY_ENV = "SENTINELMCP_TEST_DISCONNECT_API_KEY"
KEY = "test-only-disconnect-key"

POLICY = PolicyConfig.model_validate(
    {
        "principals": {
            "agent": {
                "api_key_env": KEY_ENV,
                "tools": {"benchmark.fixed_latency": {"effect": "allow", "deny_unknown_arguments": False}},
            }
        }
    }
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_serving(url: str, attempts: int = 150, delay_seconds: float = 0.02) -> None:
    import httpx2

    async with httpx2.AsyncClient() as probe:
        for _ in range(attempts):
            try:
                await probe.get(url)
                return
            except httpx2.HTTPError:
                await asyncio.sleep(delay_seconds)
    raise RuntimeError(f"{url} never became reachable")


async def test_upstream_process_dying_mid_call_does_not_hang_and_maps_to_a_graceful_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Kills a real upstream OS process 0.5s into a `benchmark.fixed_latency`
    call that requested a 5-second delay. Proves:

    1. the call does not hang for anywhere near the full 5 seconds - it
       resolves promptly once the connection actually dies
    2. it maps to a graceful, protocol-correct `CallToolResult(is_error=True)`
       - not a raised exception that tears down the *caller's own* transport
         session (this was a real bug found and fixed via this exact
         experiment; see `_upstream_error_result` in bridge.py)
    3. exactly one audit record is emitted, with outcome "upstream_error"
    4. the concurrency slot is released afterward

    Then, in the same run (to avoid spinning up a second real subprocess
    pair), documents a known, disclosed limitation: because this gateway
    holds exactly one upstream `Client` connection for its whole process
    lifetime and never reconnects (an explicit v1 non-goal - see
    docs/architecture.md), a *fully severed* upstream connection (as opposed
    to one call's failure) leaves the underlying MCP session manager unable
    to serve *any* further request - not just ones needing the dead upstream
    - for the rest of the gateway process's life. This is proven not to be a
    hang (bounded, and still an error, not a crash) but it is proven NOT to
    recover, which CLAUDE.md's "if the architecture permits it" phrasing
    anticipates as an acceptable, honestly-disclosed outcome rather than a
    required guarantee.
    """
    monkeypatch.setenv(KEY_ENV, KEY)
    audit_log_path = tmp_path / "audit.jsonl"

    port = _free_port()
    upstream_proc = subprocess.Popen(
        [sys.executable, "-m", "examples.upstream_server", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    upstream_url = f"http://127.0.0.1:{port}/mcp"

    try:
        await _wait_until_serving(upstream_url)

        concurrency_limiter = ConcurrencyLimiter(max_concurrent=5)
        gateway = build_gateway_app(
            upstream_url,
            POLICY,
            concurrency_limiter=concurrency_limiter,
            audit_logger=AuditLogger(audit_log_path),
        )

        async with running_asgi_app(gateway) as gateway_url:

            async def kill_upstream_soon() -> None:
                await asyncio.sleep(0.5)
                upstream_proc.kill()
                upstream_proc.wait(timeout=10)

            killer = asyncio.create_task(kill_upstream_soon())

            start = time.monotonic()
            async with authed_client(gateway_url, KEY) as client:
                result = await asyncio.wait_for(
                    client.call_tool("benchmark.fixed_latency", {"duration_seconds": 5}), timeout=8
                )
            elapsed = time.monotonic() - start
            await killer

            # 1. did not hang: resolved well before the tool's own requested
            # 5-second delay would have elapsed on a healthy upstream.
            assert elapsed < 4.5

            # 2. graceful, protocol-correct error - not a raised exception.
            assert result.is_error
            assert "Upstream error" in result.content[0].text

            # 3. exactly one audit record, correctly categorized.
            records = [json.loads(line) for line in audit_log_path.read_text().splitlines()]
            assert len(records) == 1
            assert records[0]["outcome"] == "upstream_error"
            assert records[0]["upstream_attempted"] is True

            # 4. concurrency capacity released.
            assert await concurrency_limiter.active_count("agent") == 0

            # Documented limitation, proven rather than assumed: a fully
            # severed upstream connection degrades the whole session
            # manager - a fresh downstream session's call also fails, but
            # (the property this test actually locks in) it does not hang
            # either. No reconnect is attempted or claimed.
            second_start = time.monotonic()
            async with authed_client(gateway_url, KEY) as second_client:
                with pytest.raises(Exception):  # noqa: PT011, BLE001 - deliberately broad: see docstring
                    await asyncio.wait_for(
                        second_client.call_tool("benchmark.fixed_latency", {"duration_seconds": 0.1}), timeout=10
                    )
            second_elapsed = time.monotonic() - second_start
            assert second_elapsed < 10
    finally:
        if upstream_proc.poll() is None:
            upstream_proc.kill()
