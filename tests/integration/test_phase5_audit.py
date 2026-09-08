"""Phase 5 exit-criteria tests: audit records over a real gateway and
upstream - correlation IDs connecting responses to records, concurrent
writes remaining valid, default redaction, and each outcome distinguishable.

Uses its own dedicated gateway (not the shared `gateway_url` fixture) since
these tests need direct access to the audit log file to inspect it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from examples.upstream_server import build_server
from sentinelmcp.gateway.limits import ConcurrencyLimiter, RateLimiter
from sentinelmcp.gateway.server import build_gateway_app
from sentinelmcp.policy.models import PolicyConfig
from tests.support import authed_client, running_asgi_app

pytestmark = pytest.mark.asyncio(loop_scope="session")

AUDIT_KEY_ENV = "SENTINELMCP_TEST_AUDIT_API_KEY"
AUDIT_KEY = "test-only-audit-key"

AUDIT_POLICY = PolicyConfig.model_validate(
    {
        "principals": {
            "audit-test-agent": {
                "api_key_env": AUDIT_KEY_ENV,
                "tools": {
                    "benchmark.noop": {"effect": "allow"},
                    "database.query_stats": {
                        "effect": "allow",
                        "required": ["database", "limit"],
                        "arguments": {"database": {"in": ["staging", "production"]}, "limit": {"min": 1, "max": 100}},
                    },
                    "database.execute_write": {"effect": "deny"},
                },
            }
        }
    }
)


@pytest.fixture(scope="module", autouse=True)
def _api_key_env() -> AsyncIterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(AUDIT_KEY_ENV, AUDIT_KEY)
        yield


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def audited_gateway(
    _api_key_env: None, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[tuple[str, Path]]:
    audit_log_path = tmp_path_factory.mktemp("sentinelmcp-audit-phase5") / "audit.jsonl"
    upstream = build_server().streamable_http_app(host="127.0.0.1")
    async with running_asgi_app(upstream) as upstream_url:
        # Generous: this module's tests share one gateway and cumulatively
        # make more calls than the gateway's normal default rate limit -
        # rate limiting isn't what these tests are about (see
        # test_phase4_limits.py for that).
        gateway = build_gateway_app(
            upstream_url,
            AUDIT_POLICY,
            rate_limiter=RateLimiter(capacity=1000.0, refill_rate=1000.0),
            concurrency_limiter=ConcurrencyLimiter(max_concurrent=1000),
            audit_log_path=str(audit_log_path),
        )
        async with running_asgi_app(gateway) as url:
            yield url, audit_log_path


def _read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _latest_record(path: Path) -> dict:
    return _read_records(path)[-1]


async def test_upstream_success_is_audited_and_correlation_id_matches_response(
    audited_gateway: tuple[str, Path],
):
    url, audit_log_path = audited_gateway

    async with authed_client(url, AUDIT_KEY) as client:
        result = await client.call_tool("benchmark.noop", {})

    record = _latest_record(audit_log_path)
    assert record["outcome"] == "upstream_success"
    assert record["principal"] == "audit-test-agent"
    assert record["tool_name"] == "benchmark.noop"
    assert record["session_id"] is not None
    assert record["total_latency_ms"] >= 0
    # The response and its audit record are tied together without exposing
    # any credential to do so.
    assert result.meta["correlation_id"] == record["correlation_id"]


async def test_policy_denied_is_audited_distinguishably(audited_gateway: tuple[str, Path]):
    url, audit_log_path = audited_gateway

    async with authed_client(url, AUDIT_KEY) as client:
        await client.call_tool("database.execute_write", {"database": "staging", "statement": "DELETE FROM x"})

    record = _latest_record(audit_log_path)
    assert record["outcome"] == "policy_denied"
    assert record["upstream_attempted"] is False


async def test_schema_rejected_is_audited_distinguishably(audited_gateway: tuple[str, Path]):
    url, audit_log_path = audited_gateway

    async with authed_client(url, AUDIT_KEY) as client:
        await client.call_tool("database.query_stats", {"database": "staging", "limit": "not-a-number"})

    record = _latest_record(audit_log_path)
    assert record["outcome"] == "schema_rejected"
    assert record["upstream_attempted"] is False


async def test_default_audit_output_contains_no_secrets(audited_gateway: tuple[str, Path]):
    url, audit_log_path = audited_gateway

    async with authed_client(url, AUDIT_KEY) as client:
        # An extra, policy-unknown field named like a secret - denied, but
        # the raw value must never reach the log even so.
        await client.call_tool(
            "database.query_stats",
            {"database": "staging", "limit": 5, "api_key": "sk-should-never-appear-in-audit-log"},
        )

    record = _latest_record(audit_log_path)
    raw_text = audit_log_path.read_text(encoding="utf-8")
    assert "sk-should-never-appear-in-audit-log" not in raw_text
    assert record["arguments"]["api_key"] == "***REDACTED***"


async def test_concurrent_calls_produce_valid_non_interleaved_audit_records(audited_gateway: tuple[str, Path]):
    url, audit_log_path = audited_gateway
    before = len(_read_records(audit_log_path))
    attempt_count = 20

    async def fire_one(client) -> None:
        await client.call_tool("benchmark.noop", {})

    async with authed_client(url, AUDIT_KEY) as client:
        await asyncio.gather(*(fire_one(client) for _ in range(attempt_count)))

    records = _read_records(audit_log_path)  # raises if any line is corrupted/interleaved JSON
    new_records = records[before:]
    assert len(new_records) == attempt_count
    assert all(r["outcome"] == "upstream_success" for r in new_records)
    assert len({r["correlation_id"] for r in new_records}) == attempt_count
