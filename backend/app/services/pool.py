"""Bounded worker pools (Constitution IV): separate extraction and download
executors, each with explicit max_workers and a matching asyncio.Semaphore so a
saturated download pool never blocks inspection (SC-005)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.config import Settings


class Pools:
    def __init__(self, settings: Settings) -> None:
        self.extract_executor = ThreadPoolExecutor(
            max_workers=settings.extract_concurrency,
            thread_name_prefix="extract",
        )
        self.download_executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_downloads,
            thread_name_prefix="download",
        )
        self.extract_sem = asyncio.Semaphore(settings.extract_concurrency)
        self.download_sem = asyncio.Semaphore(settings.max_concurrent_downloads)

    async def run_extract(self, fn, *args):
        loop = asyncio.get_running_loop()
        async with self.extract_sem:
            return await loop.run_in_executor(self.extract_executor, fn, *args)

    async def run_download(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.download_executor, fn, *args)

    def shutdown(self) -> None:
        self.extract_executor.shutdown(wait=False, cancel_futures=True)
        self.download_executor.shutdown(wait=False, cancel_futures=True)
