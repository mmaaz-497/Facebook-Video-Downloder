"""In-memory info cache (FR-001 / data-model §4): TTL + LRU keyed by canonical
video id, with single-flight de-duplication so N concurrent inspections of one
video cause exactly one extraction. Failures propagate to all waiters and are
never cached."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable


class InfoCache:
    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}

    def peek(self, key: str) -> Any | None:
        """Cached value or None. Expired entries are treated as absent."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    async def get_or_create(
        self, key: str, factory: Callable[[], Awaitable[Any]]
    ) -> Any:
        cached = self.peek(key)
        if cached is not None:
            return cached

        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await factory()
        except BaseException as exc:
            # Failures are not cached: remove the future so the next caller
            # re-runs the factory.
            self._inflight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
                # Waiters retrieve it; avoid "exception never retrieved" when none do.
                future.exception()
            raise
        self._inflight.pop(key, None)
        self._store(key, value)
        if not future.done():
            future.set_result(value)
        return value

    def _store(self, key: str, value: Any) -> None:
        self._entries[key] = (value, self._clock() + self._ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
