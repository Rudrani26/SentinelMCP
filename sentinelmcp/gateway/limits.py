"""Per-principal rate limiting and upstream-execution concurrency control.

Both are single-process, in-memory only: state lives in this process's
memory, is never shared across gateway processes, and is lost on restart.
There is no distributed rate-limiting or concurrency guarantee - see the
module-level docstrings below and docs/policy-semantics.md's neighbors for
the full documented limitations.

Both use a monotonic clock (injectable for deterministic unit tests) and an
`asyncio.Lock` around their shared state, per CLAUDE.md's engineering rules
("use monotonic time", "explicit concurrency safety").
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable


class TokenBucket:
    """A single principal's token bucket.

    Starts full (capacity tokens), so a principal may immediately burst up to
    `capacity` attempts, then is limited to `refill_rate` attempts/second
    thereafter. Refill is computed lazily from elapsed monotonic time at each
    `try_consume` call, not by a background task.
    """

    def __init__(self, capacity: float, refill_rate: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._clock = clock
        self._tokens = capacity
        self._last_refill = clock()
        self._lock = asyncio.Lock()

    async def try_consume(self, amount: float = 1.0) -> bool:
        """Attempt to consume `amount` tokens. Returns whether it succeeded."""
        async with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._last_refill)
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now
            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return False

    async def available_tokens(self) -> float:
        """Current token count, after applying refill for elapsed time. For tests/inspection."""
        async with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._last_refill)
            return min(self._capacity, self._tokens + elapsed * self._refill_rate)


class RateLimiter:
    """One `TokenBucket` per principal, created lazily on first use.

    Every authenticated, structurally valid `tools/call` attempt consumes a
    token - including one later denied by policy or rejected by schema
    validation - so this must be checked before any of those, not after.
    Authentication failures never reach this (they're rejected by
    `BearerAuthMiddleware` before any principal-scoped state exists).
    """

    def __init__(self, capacity: float, refill_rate: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._buckets_lock = asyncio.Lock()

    async def _bucket_for(self, principal: str) -> TokenBucket:
        async with self._buckets_lock:
            bucket = self._buckets.get(principal)
            if bucket is None:
                bucket = TokenBucket(self._capacity, self._refill_rate, clock=self._clock)
                self._buckets[principal] = bucket
            return bucket

    async def try_acquire(self, principal: str) -> bool:
        bucket = await self._bucket_for(principal)
        return await bucket.try_consume()


class ConcurrencyLimiter:
    """Caps concurrently *active upstream executions* per principal.

    Rejects immediately when the ceiling is already reached, rather than
    queuing callers to wait for capacity - the simplest bounded-acquisition
    behavior satisfying "do not allow unbounded waiting", and the easiest to
    reason about and test for correctness under concurrent load. Capacity is
    released in a `finally` block by `acquire()`'s context-manager form, so
    it is released after success, after an upstream exception, after a
    timeout (a wrapped `asyncio.TimeoutError`), and after cancellation.
    """

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self._max_concurrent = max_concurrent
        self._active: dict[str, int] = {}
        self._peak: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(self, principal: str) -> bool:
        async with self._lock:
            current = self._active.get(principal, 0)
            if current >= self._max_concurrent:
                return False
            current += 1
            self._active[principal] = current
            if current > self._peak.get(principal, 0):
                self._peak[principal] = current
            return True

    async def release(self, principal: str) -> None:
        async with self._lock:
            current = self._active.get(principal, 0)
            # Defensive floor: release() is only ever called after a matching
            # successful try_acquire(), so this should never go negative.
            self._active[principal] = max(0, current - 1)

    async def active_count(self, principal: str) -> int:
        """Current active-execution count, for tests/inspection."""
        async with self._lock:
            return self._active.get(principal, 0)

    async def peak_count(self, principal: str) -> int:
        """Highest concurrent-execution count ever observed, for tests."""
        async with self._lock:
            return self._peak.get(principal, 0)

    @contextlib.asynccontextmanager
    async def acquire(self, principal: str) -> AsyncIterator[bool]:
        """Yields whether capacity was acquired; releases it afterward if so,
        regardless of how the `async with` block exits."""
        acquired = await self.try_acquire(principal)
        try:
            yield acquired
        finally:
            if acquired:
                await self.release(principal)
