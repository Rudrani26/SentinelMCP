"""Unit tests for TokenBucket/RateLimiter and ConcurrencyLimiter.

Token-bucket timing tests use an injectable fake clock - never a real
`asyncio.sleep` - so they're deterministic and fast, per CLAUDE.md's
"use deterministic or injectable clocks in unit tests" rule.
"""

from __future__ import annotations

import asyncio

import pytest

from sentinelmcp.gateway.limits import ConcurrencyLimiter, RateLimiter, TokenBucket


class FakeClock:
    """A monotonic-clock stand-in advanced explicitly by tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# --- TokenBucket mathematics -------------------------------------------------


async def test_bucket_starts_full_and_allows_a_burst_up_to_capacity():
    clock = FakeClock()
    bucket = TokenBucket(capacity=5, refill_rate=1, clock=clock)

    for _ in range(5):
        assert await bucket.try_consume() is True
    assert await bucket.try_consume() is False


async def test_bucket_refills_at_the_configured_rate_over_elapsed_time():
    clock = FakeClock()
    bucket = TokenBucket(capacity=5, refill_rate=2, clock=clock)  # 2 tokens/sec

    for _ in range(5):
        assert await bucket.try_consume() is True
    assert await bucket.try_consume() is False

    clock.advance(1.0)  # +2 tokens
    assert await bucket.try_consume() is True
    assert await bucket.try_consume() is True
    assert await bucket.try_consume() is False


async def test_bucket_refill_never_exceeds_capacity():
    clock = FakeClock()
    bucket = TokenBucket(capacity=3, refill_rate=10, clock=clock)

    clock.advance(1000.0)  # would refill far past capacity if unclamped
    assert await bucket.available_tokens() == 3
    for _ in range(3):
        assert await bucket.try_consume() is True
    assert await bucket.try_consume() is False


async def test_bucket_fractional_refill_is_tracked_precisely():
    clock = FakeClock()
    bucket = TokenBucket(capacity=1, refill_rate=1, clock=clock)  # 1 token/sec

    assert await bucket.try_consume() is True
    assert await bucket.try_consume() is False

    clock.advance(0.5)  # only half a token back
    assert await bucket.try_consume() is False

    clock.advance(0.5)  # now a full token
    assert await bucket.try_consume() is True


async def test_bucket_rejects_non_positive_configuration():
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_rate=1)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1, refill_rate=0)


async def test_bucket_consumption_is_race_free_under_concurrent_callers():
    """Many concurrent try_consume() calls against a small bucket must never
    let total successes exceed capacity - the asyncio.Lock around
    refill/deduction must actually serialize them."""
    clock = FakeClock()
    bucket = TokenBucket(capacity=10, refill_rate=0.0001, clock=clock)

    results = await asyncio.gather(*(bucket.try_consume() for _ in range(100)))
    assert sum(results) == 10


# --- RateLimiter: per-principal isolation -----------------------------------


async def test_rate_limiter_gives_each_principal_an_independent_bucket():
    clock = FakeClock()
    limiter = RateLimiter(capacity=1, refill_rate=0.0001, clock=clock)

    assert await limiter.try_acquire("agent-a") is True
    assert await limiter.try_acquire("agent-a") is False
    # A different principal is unaffected by agent-a's exhausted bucket.
    assert await limiter.try_acquire("agent-b") is True


# --- ConcurrencyLimiter -------------------------------------------------------


async def test_concurrency_limiter_rejects_beyond_the_ceiling():
    limiter = ConcurrencyLimiter(max_concurrent=2)
    assert await limiter.try_acquire("agent") is True
    assert await limiter.try_acquire("agent") is True
    assert await limiter.try_acquire("agent") is False


async def test_concurrency_limiter_isolates_principals():
    limiter = ConcurrencyLimiter(max_concurrent=1)
    assert await limiter.try_acquire("agent-a") is True
    assert await limiter.try_acquire("agent-a") is False
    assert await limiter.try_acquire("agent-b") is True


async def test_release_frees_capacity_for_a_subsequent_acquire():
    limiter = ConcurrencyLimiter(max_concurrent=1)
    assert await limiter.try_acquire("agent") is True
    assert await limiter.try_acquire("agent") is False
    await limiter.release("agent")
    assert await limiter.try_acquire("agent") is True


async def test_acquire_context_manager_releases_on_normal_exit():
    limiter = ConcurrencyLimiter(max_concurrent=1)
    async with limiter.acquire("agent") as acquired:
        assert acquired is True
        assert await limiter.active_count("agent") == 1
    assert await limiter.active_count("agent") == 0


async def test_acquire_context_manager_releases_on_exception():
    limiter = ConcurrencyLimiter(max_concurrent=1)
    with pytest.raises(RuntimeError):
        async with limiter.acquire("agent") as acquired:
            assert acquired is True
            raise RuntimeError("boom")
    assert await limiter.active_count("agent") == 0


async def test_acquire_context_manager_releases_on_cancellation():
    limiter = ConcurrencyLimiter(max_concurrent=1)
    acquired_event = asyncio.Event()

    async def hold_forever() -> None:
        async with limiter.acquire("agent"):
            acquired_event.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(hold_forever())
    await acquired_event.wait()  # deterministic: waits exactly until capacity is held
    assert await limiter.active_count("agent") == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await limiter.active_count("agent") == 0


async def test_acquire_context_manager_does_not_release_when_never_acquired():
    """When capacity was already full, `acquired` is False and exiting the
    `async with` block must not decrement someone else's held slot."""
    limiter = ConcurrencyLimiter(max_concurrent=1)
    await limiter.try_acquire("agent")  # occupy the only slot

    async with limiter.acquire("agent") as acquired:
        assert acquired is False

    # The pre-existing holder's slot is untouched.
    assert await limiter.active_count("agent") == 1


async def test_hostile_concurrent_load_never_exceeds_the_ceiling():
    """100+ simultaneous attempts against a small ceiling: max observed
    concurrency must never exceed the configured limit."""
    limiter = ConcurrencyLimiter(max_concurrent=5)
    observed_concurrency: list[int] = []

    async def attempt() -> None:
        async with limiter.acquire("agent") as acquired:
            if not acquired:
                return
            observed_concurrency.append(await limiter.active_count("agent"))
            await asyncio.sleep(0.01)

    await asyncio.gather(*(attempt() for _ in range(150)))

    assert observed_concurrency  # at least some attempts got in
    assert max(observed_concurrency) <= 5
    assert await limiter.peak_count("agent") <= 5
    # All capacity was released again afterward.
    assert await limiter.active_count("agent") == 0


async def test_concurrency_limiter_rejects_non_positive_configuration():
    with pytest.raises(ValueError):
        ConcurrencyLimiter(max_concurrent=0)
