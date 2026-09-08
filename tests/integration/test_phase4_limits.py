"""Phase 4 exit-criteria tests: per-principal token-bucket rate limiting and
upstream-execution concurrency ceiling, over a real gateway and upstream.

Uses its own dedicated gateway (not the shared `gateway_url` fixture), since
these tests need deliberately tight limits that would otherwise interfere
with every other integration test's use of the shared one. Two principals -
one for rate-limit tests, one for concurrency tests - so exhausting one
limiter's per-principal state can't contaminate the other test's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from examples.upstream_server import build_server, concurrency_tracker, counters
from sentinelmcp.gateway.limits import ConcurrencyLimiter, RateLimiter
from sentinelmcp.gateway.server import build_gateway_app
from sentinelmcp.policy.models import PolicyConfig
from tests.support import authed_client, running_asgi_app

pytestmark = pytest.mark.asyncio(loop_scope="session")

RATE_LIMIT_KEY_ENV = "SENTINELMCP_TEST_RATE_LIMIT_API_KEY"
RATE_LIMIT_KEY = "test-only-rate-limit-key"
RATE_LIMIT_CAPACITY = 10

CONCURRENCY_KEY_ENV = "SENTINELMCP_TEST_CONCURRENCY_API_KEY"
CONCURRENCY_KEY = "test-only-concurrency-key"
MAX_CONCURRENT = 2

_ALLOWED_TOOLS = {
    "benchmark.noop": {"effect": "allow"},
    "benchmark.fixed_latency": {"effect": "allow", "deny_unknown_arguments": False},
}

LIMITS_POLICY = PolicyConfig.model_validate(
    {
        "principals": {
            "rate-limit-test-agent": {"api_key_env": RATE_LIMIT_KEY_ENV, "tools": _ALLOWED_TOOLS},
            "concurrency-test-agent": {"api_key_env": CONCURRENCY_KEY_ENV, "tools": _ALLOWED_TOOLS},
        }
    }
)


@pytest.fixture(scope="module", autouse=True)
def _api_key_env() -> AsyncIterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(RATE_LIMIT_KEY_ENV, RATE_LIMIT_KEY)
        mp.setenv(CONCURRENCY_KEY_ENV, CONCURRENCY_KEY)
        yield


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def limited_gateway(
    _api_key_env: None, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[tuple[str, ConcurrencyLimiter]]:
    """A dedicated gateway with tight rate/concurrency limits, plus direct
    access to the `ConcurrencyLimiter` object for peak-observation."""
    audit_log_path = tmp_path_factory.mktemp("sentinelmcp-audit-limits") / "audit.jsonl"
    upstream = build_server().streamable_http_app(host="127.0.0.1")
    async with running_asgi_app(upstream) as upstream_url:
        # refill_rate is near-zero so a test's own real (sub-second) runtime
        # cannot meaningfully refill the bucket mid-test - deterministic
        # enough without an injectable clock at this level.
        rate_limiter = RateLimiter(capacity=RATE_LIMIT_CAPACITY, refill_rate=0.0001)
        concurrency_limiter = ConcurrencyLimiter(max_concurrent=MAX_CONCURRENT)
        gateway = build_gateway_app(
            upstream_url,
            LIMITS_POLICY,
            rate_limiter=rate_limiter,
            concurrency_limiter=concurrency_limiter,
            audit_log_path=str(audit_log_path),
        )
        async with running_asgi_app(gateway) as url:
            yield url, concurrency_limiter


async def test_burst_then_rate_limited_and_upstream_reflects_only_allowed_calls(
    limited_gateway: tuple[str, ConcurrencyLimiter],
):
    url, _ = limited_gateway
    initial = counters.noop

    async with authed_client(url, RATE_LIMIT_KEY) as client:
        results = [await client.call_tool("benchmark.noop", {}) for _ in range(RATE_LIMIT_CAPACITY)]
        assert all(not r.is_error for r in results)

        limited_result = await client.call_tool("benchmark.noop", {})

    assert limited_result.is_error
    assert "Rate limit" in limited_result.content[0].text
    # The rate-limited attempt never reached the upstream tool.
    assert counters.noop == initial + RATE_LIMIT_CAPACITY


async def test_concurrency_ceiling_enforced_under_real_concurrent_load(
    limited_gateway: tuple[str, ConcurrencyLimiter],
):
    """More concurrent real requests than the ceiling allows: some succeed,
    the rest are rejected, and max observed concurrency - measured
    independently by the gateway's own limiter *and* by the upstream tool
    itself - never exceeds the configured ceiling."""
    url, concurrency_limiter = limited_gateway
    attempt_count = MAX_CONCURRENT * 4

    async def fire_one(client) -> object:
        return await client.call_tool("benchmark.fixed_latency", {"duration_seconds": 0.3})

    async with authed_client(url, CONCURRENCY_KEY) as client:
        results = await asyncio.gather(*(fire_one(client) for _ in range(attempt_count)))

    succeeded = [r for r in results if not r.is_error]
    rejected = [r for r in results if r.is_error]
    assert len(succeeded) + len(rejected) == attempt_count
    assert len(succeeded) > 0
    assert all("Concurrency limit" in r.content[0].text for r in rejected)

    assert await concurrency_limiter.peak_count("concurrency-test-agent") <= MAX_CONCURRENT
    assert concurrency_tracker.peak <= MAX_CONCURRENT
    # Every slot was released again after the burst finished.
    assert await concurrency_limiter.active_count("concurrency-test-agent") == 0
