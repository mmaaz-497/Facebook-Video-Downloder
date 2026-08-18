"""Ephemerality guarantees (FR-007): a periodic sweep that TTL-purges expired
jobs together with their files, and removes orphan .part/.mp4 leftovers in
DOWNLOADS_DIR that no job record owns (crash leftovers included, via the
boot-time initial sweep). Started/stopped by the app lifespan."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.services.jobs import JobStore

logger = logging.getLogger("app.cleanup")

# Indirection so tests can observe the interval without real sleeping.
_sleep = asyncio.sleep


async def sweep(store: JobStore, settings) -> None:
    """One pass: purge TTL-expired jobs (record + file atomically via
    JobStore.purge), then delete files whose <job_id> stem no job owns."""
    now = time.monotonic()
    for job_id in await store.expired_ids(settings.job_ttl_seconds, now):
        await store.purge(job_id)

    downloads_dir: Path = settings.downloads_dir
    if not downloads_dir.is_dir():
        return
    known = await store.known_ids()
    for path in downloads_dir.iterdir():
        if not path.is_file():
            continue
        # On-disk names are always <job_id>.<ext...> (uuid4 hex stems).
        stem = path.name.split(".", 1)[0]
        if stem not in known:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # in use / permission hiccup — next sweep retries


async def run_periodic(store: JobStore, settings) -> None:
    """Sweep immediately at boot (crash leftovers), then every
    CLEANUP_INTERVAL_SECONDS until cancelled by the lifespan."""
    while True:
        try:
            await sweep(store, settings)
        except Exception:
            # The sweep must never die: it is the last line of the
            # no-data-at-rest guarantee (SC-004).
            logger.exception("cleanup sweep failed")
        await _sleep(settings.cleanup_interval_seconds)
