"""US4 T041: cleanup sweep (FR-007) — TTL purge removes job + file together,
orphan .part/.mp4 files with no job record are swept, active-job files are
untouched, and the periodic loop honors CLEANUP_INTERVAL_SECONDS with a
boot-time initial sweep."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import get_settings
from app.services import cleanup
from app.services.jobs import Job, JobState, JobStore


@pytest.fixture
def settings(tmp_path):
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    return get_settings(
        downloads_dir=downloads_dir,
        ip_hash_salt="s",
        job_ttl_seconds=1800,
        cleanup_interval_seconds=60,
    )


def make_job(state: JobState, age_seconds: float = 0.0, **kw) -> Job:
    job = Job(
        client_hash="c", url="u", video_id="v", requested_height=720, title="t", **kw
    )
    job.state = state
    now = time.monotonic()
    job.created_at = now - age_seconds
    if state in (JobState.COMPLETED, JobState.FAILED):
        job.finished_at = now - age_seconds
    return job


async def test_ttl_purge_removes_job_and_file_together(settings):
    store = JobStore()
    file_path = settings.downloads_dir / "expired.mp4"
    file_path.write_bytes(b"x")
    job = make_job(JobState.COMPLETED, age_seconds=1801)  # past the 1800s TTL
    job.file_path = file_path
    await store.create(job)

    await cleanup.sweep(store, settings)

    assert await store.get(job.id) is None
    assert not file_path.exists()


async def test_fresh_terminal_job_and_file_survive_the_sweep(settings):
    store = JobStore()
    job = make_job(JobState.COMPLETED, age_seconds=10)
    file_path = settings.downloads_dir / f"{job.id}.mp4"
    file_path.write_bytes(b"x")
    job.file_path = file_path
    await store.create(job)

    await cleanup.sweep(store, settings)

    assert await store.get(job.id) is not None
    assert file_path.exists()


async def test_stale_queued_job_expires_by_created_at(settings):
    # data-model §3: queued/running jobs also TTL-expire (abandonment path).
    store = JobStore()
    job = make_job(JobState.QUEUED, age_seconds=1801)
    await store.create(job)

    await cleanup.sweep(store, settings)

    assert await store.get(job.id) is None


async def test_orphan_part_and_mp4_files_are_swept(settings):
    store = JobStore()
    orphan_part = settings.downloads_dir / ("a" * 32 + ".part")
    orphan_mp4 = settings.downloads_dir / ("b" * 32 + ".mp4")
    orphan_fragment = settings.downloads_dir / ("c" * 32 + ".f720.mp4")
    for f in (orphan_part, orphan_mp4, orphan_fragment):
        f.write_bytes(b"leftover")

    await cleanup.sweep(store, settings)

    assert not orphan_part.exists()
    assert not orphan_mp4.exists()
    assert not orphan_fragment.exists()


async def test_active_job_files_are_untouched(settings):
    store = JobStore()
    job = make_job(JobState.RUNNING, age_seconds=10)
    await store.create(job)
    part = settings.downloads_dir / f"{job.id}.part"
    fragment = settings.downloads_dir / f"{job.id}.f720.mp4"
    part.write_bytes(b"downloading")
    fragment.write_bytes(b"downloading")

    await cleanup.sweep(store, settings)

    assert await store.get(job.id) is not None
    assert part.exists()
    assert fragment.exists()


async def test_periodic_loop_sweeps_at_boot_then_honors_interval(
    settings, monkeypatch
):
    store = JobStore()
    orphan = settings.downloads_dir / ("d" * 32 + ".part")
    orphan.write_bytes(b"crash leftover")

    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)
        if len(recorded) >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(cleanup, "_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await cleanup.run_periodic(store, settings)

    # Boot-time initial sweep ran BEFORE the first sleep (crash leftovers gone),
    # and every wait between sweeps is CLEANUP_INTERVAL_SECONDS.
    assert not orphan.exists()
    assert recorded == [60, 60]
