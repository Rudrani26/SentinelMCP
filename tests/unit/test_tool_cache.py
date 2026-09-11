"""Unit tests for `UpstreamToolCache` (sentinelmcp/gateway/bridge.py).

Isolated from the bridge handlers themselves - `test_bridge_handlers.py`
separately proves the handlers actually route through this cache instead of
calling `upstream.list_tools()` directly on every call.
"""

from __future__ import annotations

import asyncio

from mcp import types

from sentinelmcp.gateway.bridge import UpstreamToolCache


class _CountingUpstream:
    """A minimal stand-in for `Client` whose `list_tools()` records how many
    times it was actually called, and can return a different tool set after
    `set_tools` is called - for proving invalidation is respected."""

    def __init__(self, tools: list[types.Tool]) -> None:
        self._tools = tools
        self.call_count = 0

    def set_tools(self, tools: list[types.Tool]) -> None:
        self._tools = tools

    async def list_tools(self) -> types.ListToolsResult:
        self.call_count += 1
        return types.ListToolsResult(tools=self._tools)


def _tool(name: str) -> types.Tool:
    return types.Tool(name=name, input_schema={"type": "object"})


async def test_cache_initializes_correctly_on_first_get():
    upstream = _CountingUpstream([_tool("a"), _tool("b")])
    cache = UpstreamToolCache()

    tools = await cache.get(upstream)

    assert [t.name for t in tools] == ["a", "b"]
    assert upstream.call_count == 1


async def test_repeated_calls_do_not_unnecessarily_refetch():
    upstream = _CountingUpstream([_tool("a")])
    cache = UpstreamToolCache()

    for _ in range(10):
        await cache.get(upstream)

    assert upstream.call_count == 1


async def test_concurrent_first_callers_produce_exactly_one_real_fetch():
    """A hostile-ish cold-start race: many callers arrive before the cache
    is populated. The lock is held across the fetch itself, so only one of
    them actually performs it - proving concurrency cannot produce two
    competing fetches or a torn/inconsistent cache state."""
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    call_count = 0

    class _SlowFirstFetchUpstream:
        async def list_tools(self) -> types.ListToolsResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                fetch_started.set()
                await release_fetch.wait()
            return types.ListToolsResult(tools=[_tool("a")])

    upstream = _SlowFirstFetchUpstream()
    cache = UpstreamToolCache()

    first_getter = asyncio.create_task(cache.get(upstream))
    await fetch_started.wait()  # the first caller is now blocked mid-fetch, holding the lock

    other_getters = [asyncio.create_task(cache.get(upstream)) for _ in range(20)]
    await asyncio.sleep(0)  # let them all queue up on the lock

    release_fetch.set()
    results = await asyncio.gather(first_getter, *other_getters)

    assert call_count == 1
    assert all([t.name for t in tools] == ["a"] for tools in results)


async def test_invalidate_forces_a_refetch_on_the_next_get():
    upstream = _CountingUpstream([_tool("a")])
    cache = UpstreamToolCache()

    await cache.get(upstream)
    assert upstream.call_count == 1

    await cache.invalidate()
    await cache.get(upstream)

    assert upstream.call_count == 2


async def test_changed_upstream_tools_are_respected_after_invalidation():
    """Proves the designed invalidation semantics end to end: a cache is not
    just "cleared," a subsequent get() actually reflects new upstream data."""
    upstream = _CountingUpstream([_tool("old_tool")])
    cache = UpstreamToolCache()

    before = await cache.get(upstream)
    assert [t.name for t in before] == ["old_tool"]

    upstream.set_tools([_tool("new_tool")])
    stale = await cache.get(upstream)
    assert [t.name for t in stale] == ["old_tool"]  # not yet invalidated: still stale, by design

    await cache.invalidate()
    fresh = await cache.get(upstream)
    assert [t.name for t in fresh] == ["new_tool"]


async def test_invalidate_before_any_get_is_a_safe_no_op():
    upstream = _CountingUpstream([_tool("a")])
    cache = UpstreamToolCache()

    await cache.invalidate()
    tools = await cache.get(upstream)

    assert [t.name for t in tools] == ["a"]
    assert upstream.call_count == 1
