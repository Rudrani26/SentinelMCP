"""Phase 3 exit-criteria tests: upstream inputSchema validation and
SentinelMCP argument-constraint evaluation, over a real upstream server and a
real gateway.

Uses the shared session-scoped `gateway_url` fixture from conftest.py, and
the `constrained-diagnostics-agent` principal configured there.
"""

from __future__ import annotations

import pytest

from examples.upstream_server import counters
from tests.integration.conftest import CONSTRAINED_KEY
from tests.support import authed_client

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_permitted_constrained_call_succeeds_with_values_forwarded_unchanged(gateway_url: str):
    initial = counters.query_stats

    async with authed_client(gateway_url, CONSTRAINED_KEY) as client:
        result = await client.call_tool(
            "database.query_stats",
            {"database": "staging", "limit": 50, "include_query_text": False},
        )

    assert not result.is_error
    # The upstream tool reflects its received arguments back - proving the
    # exact values (and types) it saw are what was sent, not coerced.
    reflected = result.content[0].text
    assert '"database": "staging"' in reflected
    assert '"limit": 50' in reflected
    assert '"include_query_text": false' in reflected
    assert counters.query_stats == initial + 1


async def test_schema_invalid_call_is_rejected_and_never_reaches_upstream(gateway_url: str):
    """`limit` as a string fails the upstream's own inputSchema (integer),
    which SentinelMCP validates against before its own constraints."""
    initial = counters.query_stats

    async with authed_client(gateway_url, CONSTRAINED_KEY) as client:
        result = await client.call_tool(
            "database.query_stats",
            {"database": "staging", "limit": "fifty"},
        )

    assert result.is_error
    assert "Schema-invalid" in result.content[0].text
    assert counters.query_stats == initial


async def test_policy_invalid_call_is_rejected_and_never_reaches_upstream(gateway_url: str):
    """`database` is schema-valid (any string) but not in the policy's
    allowed set - distinguishable from the schema-invalid case above."""
    initial = counters.query_stats

    async with authed_client(gateway_url, CONSTRAINED_KEY) as client:
        result = await client.call_tool(
            "database.query_stats",
            {"database": "attacker-controlled-db", "limit": 50},
        )

    assert result.is_error
    assert "Argument policy violation" in result.content[0].text
    assert "Schema-invalid" not in result.content[0].text
    assert counters.query_stats == initial


async def test_numeric_out_of_range_call_is_rejected_and_never_reaches_upstream(gateway_url: str):
    """`limit` is schema-valid (upstream only declares it as an integer, with
    no schema-level range) but exceeds the policy's `max: 100` - isolates the
    numeric min/max constraint end-to-end, distinct from the `in`-set case
    covered by test_policy_invalid_call_is_rejected_and_never_reaches_upstream
    above."""
    initial = counters.query_stats

    async with authed_client(gateway_url, CONSTRAINED_KEY) as client:
        result = await client.call_tool(
            "database.query_stats",
            {"database": "staging", "limit": 101, "include_query_text": False},
        )

    assert result.is_error
    assert "Argument policy violation" in result.content[0].text
    assert "Schema-invalid" not in result.content[0].text
    assert counters.query_stats == initial


async def test_missing_required_field_is_rejected_and_never_reaches_upstream(gateway_url: str):
    """`include_query_text` is policy-required (not schema-required, since it
    has a Python default) - omitting it isolates the policy-level `required`
    check from schema validation."""
    initial = counters.query_stats

    async with authed_client(gateway_url, CONSTRAINED_KEY) as client:
        result = await client.call_tool("database.query_stats", {"database": "staging", "limit": 50})

    assert result.is_error
    assert "Argument policy violation" in result.content[0].text
    assert counters.query_stats == initial


async def test_unknown_argument_field_is_rejected_and_never_reaches_upstream(gateway_url: str):
    """A field the schema and constraints don't know about at all - a
    representative "nested/extra field" bypass attempt."""
    initial = counters.query_stats

    async with authed_client(gateway_url, CONSTRAINED_KEY) as client:
        result = await client.call_tool(
            "database.query_stats",
            {"database": "staging", "limit": 50, "admin_override": True},
        )

    assert result.is_error
    assert counters.query_stats == initial


async def test_type_confused_argument_is_rejected_and_never_reaches_upstream(gateway_url: str):
    """`include_query_text` expects the JSON boolean `false`; sending the
    string `"false"` instead must be rejected somewhere in the pipeline
    (here, the upstream's own schema catches it, since it declares a
    boolean type - the unit tests in test_argument_policy.py isolate
    SentinelMCP's own strict-equals behavior directly)."""
    initial = counters.query_stats

    async with authed_client(gateway_url, CONSTRAINED_KEY) as client:
        result = await client.call_tool(
            "database.query_stats",
            {"database": "staging", "limit": 50, "include_query_text": "false"},
        )

    assert result.is_error
    assert counters.query_stats == initial
