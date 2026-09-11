"""Unit tests proving the bridge emits exactly one terminal audit record per
`tools/call` attempt, with the outcome matching what actually happened, and
that the response carries the same correlation ID as its audit record.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types

from sentinelmcp.gateway.bridge import make_call_tool_handler
from sentinelmcp.gateway.identity import authenticated_as
from sentinelmcp.gateway.limits import ConcurrencyLimiter, RateLimiter
from sentinelmcp.policy.models import PolicyConfig
from sentinelmcp.telemetry.audit import AuditLogger

POLICY = PolicyConfig.model_validate(
    {
        "principals": {
            "agent": {
                "api_key_env": "SOME_ENV_VAR",
                "tools": {
                    "allowed.tool": {"effect": "allow", "deny_unknown_arguments": False},
                    "denied.tool": {"effect": "deny"},
                },
            }
        }
    }
)

_OPEN_SCHEMA = {"type": "object"}


def _fake_ctx(upstream: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(lifespan_context=upstream)


def _upstream_with_tool(name: str, input_schema: dict | None = _OPEN_SCHEMA) -> AsyncMock:
    upstream = AsyncMock()
    upstream.list_tools.return_value = types.ListToolsResult(tools=[types.Tool(name=name, input_schema=input_schema)])
    return upstream


def _read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_successful_call_emits_one_upstream_success_record_with_matching_correlation_id(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = _upstream_with_tool("allowed.tool")
    upstream.call_tool.return_value = types.CallToolResult(content=[], is_error=False)
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), ConcurrencyLimiter(1000), audit_logger)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": 1})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["outcome"] == "upstream_success"
    assert records[0]["principal"] == "agent"
    assert records[0]["tool_name"] == "allowed.tool"
    assert records[0]["upstream_attempted"] is True
    assert records[0]["rate_limit_result"] == "passed"
    assert records[0]["schema_result"] == "passed"
    assert records[0]["policy_result"] == "passed"
    assert records[0]["concurrency_result"] == "passed"
    assert result.meta["correlation_id"] == records[0]["correlation_id"]


async def test_policy_denied_call_emits_one_policy_denied_record(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = AsyncMock()
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), ConcurrencyLimiter(1000), audit_logger)
    params = types.CallToolRequestParams(name="denied.tool", arguments={})

    with authenticated_as("agent"):
        await handler(_fake_ctx(upstream), params)

    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["outcome"] == "policy_denied"
    assert records[0]["upstream_attempted"] is False


async def test_schema_rejected_call_emits_one_schema_rejected_record(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = _upstream_with_tool(
        "allowed.tool", input_schema={"type": "object", "properties": {"x": {"type": "integer"}}}
    )
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), ConcurrencyLimiter(1000), audit_logger)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"x": "not-an-integer"})

    with authenticated_as("agent"):
        await handler(_fake_ctx(upstream), params)

    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["outcome"] == "schema_rejected"
    assert records[0]["upstream_attempted"] is False


async def test_rate_limited_call_emits_one_rate_limited_record(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = _upstream_with_tool("allowed.tool")
    upstream.call_tool.return_value = types.CallToolResult(content=[], is_error=False)
    handler = make_call_tool_handler(POLICY, RateLimiter(1, 0.0001), ConcurrencyLimiter(1000), audit_logger)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})

    with authenticated_as("agent"):
        await handler(_fake_ctx(upstream), params)  # consumes the only token
        await handler(_fake_ctx(upstream), params)  # rate limited

    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 2
    assert records[0]["outcome"] == "upstream_success"
    assert records[1]["outcome"] == "rate_limited"
    assert records[1]["rate_limit_result"] == "failed"


async def test_concurrency_rejected_call_emits_one_concurrency_rejected_record(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = _upstream_with_tool("allowed.tool")
    concurrency_limiter = ConcurrencyLimiter(max_concurrent=1)
    await concurrency_limiter.try_acquire("agent")  # occupy the only slot
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), concurrency_limiter, audit_logger)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})

    with authenticated_as("agent"):
        await handler(_fake_ctx(upstream), params)

    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["outcome"] == "concurrency_rejected"
    assert records[0]["upstream_attempted"] is False


async def test_upstream_exception_emits_one_upstream_error_record_and_returns_gracefully(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = _upstream_with_tool("allowed.tool")
    upstream.call_tool.side_effect = RuntimeError("boom")
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), ConcurrencyLimiter(1000), audit_logger)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})

    with authenticated_as("agent"):
        result = await handler(_fake_ctx(upstream), params)

    assert result.is_error
    assert "Upstream error" in result.content[0].text

    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["outcome"] == "upstream_error"
    assert records[0]["upstream_attempted"] is True


async def test_upstream_timeout_emits_one_upstream_timeout_record(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = _upstream_with_tool("allowed.tool")

    async def slow_call_tool(*_a, **_kw):
        await asyncio.sleep(10)

    upstream.call_tool.side_effect = slow_call_tool
    handler = make_call_tool_handler(
        POLICY,
        RateLimiter(1000.0, 1000.0),
        ConcurrencyLimiter(1000),
        audit_logger,
        upstream_call_timeout_seconds=0.05,
    )
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})

    with authenticated_as("agent"):
        await handler(_fake_ctx(upstream), params)

    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["outcome"] == "upstream_timeout"
    assert records[0]["upstream_attempted"] is True


async def test_cancellation_during_upstream_call_emits_one_cancelled_record(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = _upstream_with_tool("allowed.tool")
    upstream_call_started = asyncio.Event()

    async def slow_call_tool(*_a, **_kw):
        upstream_call_started.set()
        await asyncio.sleep(10)

    upstream.call_tool.side_effect = slow_call_tool
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), ConcurrencyLimiter(1000), audit_logger)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})

    with authenticated_as("agent"):
        task = asyncio.create_task(handler(_fake_ctx(upstream), params))
        await upstream_call_started.wait()  # deterministic: waits exactly until the upstream call begins
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["outcome"] == "cancelled"
    assert records[0]["upstream_attempted"] is True


async def test_secrets_never_appear_in_the_audit_log_under_the_default_redaction_mode(tmp_path: Path):
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    upstream = _upstream_with_tool("allowed.tool")
    upstream.call_tool.return_value = types.CallToolResult(content=[], is_error=False)
    handler = make_call_tool_handler(POLICY, RateLimiter(1000.0, 1000.0), ConcurrencyLimiter(1000), audit_logger)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={"api_key": "sk-super-secret-value"})

    with authenticated_as("agent"):
        await handler(_fake_ctx(upstream), params)

    raw_log_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in raw_log_text


async def test_every_failure_category_produces_a_pairwise_distinct_audit_outcome(tmp_path: Path):
    """Consolidates invariant #17 ("upstream failures are distinguishable
    from authentication, schema, policy, rate-limit, and concurrency
    failures") into one place: trigger every failure category this bridge
    can produce and assert their recorded `outcome` values are all
    different from each other and from a successful call's."""
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")

    # rate_limited
    rl_upstream = _upstream_with_tool("allowed.tool")
    rl_upstream.call_tool.return_value = types.CallToolResult(content=[], is_error=False)
    rl_handler = make_call_tool_handler(POLICY, RateLimiter(1, 0.0001), ConcurrencyLimiter(1000), audit_logger)
    params = types.CallToolRequestParams(name="allowed.tool", arguments={})
    with authenticated_as("agent"):
        await rl_handler(_fake_ctx(rl_upstream), params)  # consumes the token: upstream_success
        await rl_handler(_fake_ctx(rl_upstream), params)  # rate_limited

    # policy_denied
    generous_handler = make_call_tool_handler(
        POLICY, RateLimiter(1000.0, 1000.0), ConcurrencyLimiter(1000), audit_logger
    )
    with authenticated_as("agent"):
        await generous_handler(_fake_ctx(AsyncMock()), types.CallToolRequestParams(name="denied.tool", arguments={}))

    # schema_rejected
    schema_upstream = _upstream_with_tool(
        "allowed.tool", input_schema={"type": "object", "properties": {"x": {"type": "integer"}}}
    )
    with authenticated_as("agent"):
        await generous_handler(
            _fake_ctx(schema_upstream),
            types.CallToolRequestParams(name="allowed.tool", arguments={"x": "not-an-integer"}),
        )

    # concurrency_rejected
    full_concurrency_limiter = ConcurrencyLimiter(max_concurrent=1)
    await full_concurrency_limiter.try_acquire("agent")
    cr_handler = make_call_tool_handler(
        POLICY, RateLimiter(1000.0, 1000.0), full_concurrency_limiter, audit_logger
    )
    with authenticated_as("agent"):
        await cr_handler(_fake_ctx(_upstream_with_tool("allowed.tool")), params)

    # upstream_error
    error_upstream = _upstream_with_tool("allowed.tool")
    error_upstream.call_tool.side_effect = RuntimeError("boom")
    with authenticated_as("agent"):
        await generous_handler(_fake_ctx(error_upstream), params)

    # upstream_timeout
    timeout_upstream = _upstream_with_tool("allowed.tool")

    async def slow_call_tool(*_a, **_kw):
        await asyncio.sleep(10)

    timeout_upstream.call_tool.side_effect = slow_call_tool
    timeout_handler = make_call_tool_handler(
        POLICY,
        RateLimiter(1000.0, 1000.0),
        ConcurrencyLimiter(1000),
        audit_logger,
        upstream_call_timeout_seconds=0.05,
    )
    with authenticated_as("agent"):
        await timeout_handler(_fake_ctx(timeout_upstream), params)

    # cancelled
    cancel_upstream = _upstream_with_tool("allowed.tool")
    upstream_call_started = asyncio.Event()

    async def slow_call_tool_with_signal(*_a, **_kw):
        upstream_call_started.set()
        await asyncio.sleep(10)

    cancel_upstream.call_tool.side_effect = slow_call_tool_with_signal
    with authenticated_as("agent"):
        task = asyncio.create_task(generous_handler(_fake_ctx(cancel_upstream), params))
        await upstream_call_started.wait()  # deterministic: waits exactly until the upstream call begins
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    records = _read_records(tmp_path / "audit.jsonl")
    outcomes = [r["outcome"] for r in records]
    expected = {
        "upstream_success",
        "rate_limited",
        "policy_denied",
        "schema_rejected",
        "concurrency_rejected",
        "upstream_error",
        "upstream_timeout",
        "cancelled",
    }
    assert expected.issubset(set(outcomes))
    # Every failure category recorded here is a genuinely distinct value -
    # none of them collide with each other or with success.
    assert len(set(outcomes)) == len(outcomes)
