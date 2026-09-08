"""Phase 3 malformed-input tests: the gateway must survive malformed
protocol input without crashing or hanging, using the official SDK's own
request handling rather than anything SentinelMCP reimplements.

These send raw HTTP directly (bypassing the MCP SDK client entirely) so the
gateway sees exactly the malformed bytes/JSON described, then prove the
gateway is still alive and correctly serving a real request afterward.

Uses the shared session-scoped `gateway_url` fixture from conftest.py.
"""

from __future__ import annotations

import httpx2
import pytest

from tests.integration.conftest import MALFORMED_INPUT_KEY
from tests.support import authed_client

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _post_raw(gateway_url: str, content: bytes, *, content_type: str = "application/json") -> httpx2.Response:
    headers = {
        "Authorization": f"Bearer {MALFORMED_INPUT_KEY}",
        "Content-Type": content_type,
        "Accept": "application/json, text/event-stream",
    }
    async with httpx2.AsyncClient() as http_client:
        return await http_client.post(gateway_url, content=content, headers=headers)


async def _assert_gateway_still_works(gateway_url: str) -> None:
    """After sending something malformed, prove the gateway is still alive
    and correctly serving real requests - not crashed, not stuck."""
    async with authed_client(gateway_url, MALFORMED_INPUT_KEY) as client:
        result = await client.call_tool("benchmark.noop", {})
        assert not result.is_error


@pytest.mark.parametrize(
    "body",
    [
        b"this is not json at all",
        b'{"unterminated": ',
        b"",
    ],
)
async def test_invalid_json_does_not_crash_the_gateway(gateway_url: str, body: bytes):
    response = await _post_raw(gateway_url, body)
    assert response.status_code < 500
    await _assert_gateway_still_works(gateway_url)


async def test_valid_json_that_is_not_a_jsonrpc_envelope_does_not_crash_the_gateway(gateway_url: str):
    response = await _post_raw(gateway_url, b'{"hello": "world"}')
    assert response.status_code < 500
    await _assert_gateway_still_works(gateway_url)


async def test_unsupported_method_does_not_crash_the_gateway(gateway_url: str):
    response = await _post_raw(
        gateway_url,
        b'{"jsonrpc": "2.0", "id": 1, "method": "totally/unsupported", "params": {}}',
    )
    assert response.status_code < 500
    await _assert_gateway_still_works(gateway_url)


async def test_call_tool_missing_params_does_not_crash_the_gateway(gateway_url: str):
    response = await _post_raw(gateway_url, b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call"}')
    assert response.status_code < 500
    await _assert_gateway_still_works(gateway_url)


async def test_call_tool_wrong_param_types_does_not_crash_the_gateway(gateway_url: str):
    """`params` is a JSON array/number/string instead of an object - and
    `name` (when present) is the wrong JSON type."""
    response = await _post_raw(
        gateway_url,
        b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["not", "an", "object"]}',
    )
    assert response.status_code < 500
    await _assert_gateway_still_works(gateway_url)

    response = await _post_raw(
        gateway_url,
        b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 12345, "arguments": {}}}',
    )
    assert response.status_code < 500
    await _assert_gateway_still_works(gateway_url)


async def test_unexpected_nested_structure_does_not_crash_the_gateway(gateway_url: str):
    response = await _post_raw(
        gateway_url,
        b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", '
        b'"params": {"name": "benchmark.noop", "arguments": {"a": {"b": {"c": {"d": [1, 2, [3, [4, [5]]]]}}}}}}}',
    )
    assert response.status_code < 500
    await _assert_gateway_still_works(gateway_url)
