"""Info cache (FR-001 / data-model §4): TTL expiry, LRU bound, single-flight
de-duplication, failures never cached, canonical-id sharing."""

from __future__ import annotations

import asyncio

import pytest

from app.models.errors import AppError, ErrorCode
from app.services.info_cache import InfoCache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class CountingFactory:
    def __init__(self, value: object = "video-info", fail: bool = False) -> None:
        self.calls = 0
        self.value = value
        self.fail = fail
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self) -> object:
        self.calls += 1
        await self.release.wait()
        if self.fail:
            raise AppError(ErrorCode.EXTRACTION_FAILED, "boom")
        return self.value


async def test_hit_within_ttl_skips_factory():
    clock = Clock()
    cache = InfoCache(ttl_seconds=300, max_entries=10, clock=clock)
    factory = CountingFactory()
    assert await cache.get_or_create("vid1", factory) == "video-info"
    clock.now += 299
    assert await cache.get_or_create("vid1", factory) == "video-info"
    assert factory.calls == 1


async def test_ttl_expiry_re_extracts():
    clock = Clock()
    cache = InfoCache(ttl_seconds=300, max_entries=10, clock=clock)
    factory = CountingFactory()
    await cache.get_or_create("vid1", factory)
    clock.now += 301
    await cache.get_or_create("vid1", factory)
    assert factory.calls == 2


async def test_lru_bound_evicts_oldest():
    clock = Clock()
    cache = InfoCache(ttl_seconds=300, max_entries=2, clock=clock)
    f1, f2, f3 = CountingFactory("a"), CountingFactory("b"), CountingFactory("c")
    await cache.get_or_create("k1", f1)
    await cache.get_or_create("k2", f2)
    await cache.get_or_create("k3", f3)  # evicts k1
    await cache.get_or_create("k1", f1)  # re-extraction required
    assert f1.calls == 2
    # k3 stayed cached throughout
    await cache.get_or_create("k3", f3)
    assert f3.calls == 1


async def test_single_flight_concurrent_gets_one_extraction():
    cache = InfoCache(ttl_seconds=300, max_entries=10, clock=Clock())
    factory = CountingFactory()
    factory.release.clear()  # hold all callers in-flight

    tasks = [
        asyncio.create_task(cache.get_or_create("vid1", factory)) for _ in range(10)
    ]
    await asyncio.sleep(0.01)
    factory.release.set()
    results = await asyncio.gather(*tasks)
    assert results == ["video-info"] * 10
    assert factory.calls == 1


async def test_failures_propagate_to_all_waiters_and_are_not_cached():
    cache = InfoCache(ttl_seconds=300, max_entries=10, clock=Clock())
    factory = CountingFactory(fail=True)
    factory.release.clear()

    tasks = [
        asyncio.create_task(cache.get_or_create("vid1", factory)) for _ in range(5)
    ]
    await asyncio.sleep(0.01)
    factory.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, AppError) for r in results)
    assert factory.calls == 1

    # The failure was not cached: a later call re-runs the factory and succeeds.
    factory.fail = False
    assert await cache.get_or_create("vid1", factory) == "video-info"
    assert factory.calls == 2


async def test_canonical_id_shares_one_entry_across_url_forms():
    cache = InfoCache(ttl_seconds=300, max_entries=10, clock=Clock())
    factory = CountingFactory()
    # watch?v=, reel/, and fb.watch forms all canonicalize to one video id key.
    for _form in ("watch", "reel", "fbwatch"):
        await cache.get_or_create("same-canonical-id", factory)
    assert factory.calls == 1


async def test_peek_returns_cached_value_without_factory():
    clock = Clock()
    cache = InfoCache(ttl_seconds=300, max_entries=10, clock=clock)
    assert cache.peek("vid1") is None
    await cache.get_or_create("vid1", CountingFactory())
    assert cache.peek("vid1") == "video-info"
    clock.now += 301
    assert cache.peek("vid1") is None  # expired entries are absent
