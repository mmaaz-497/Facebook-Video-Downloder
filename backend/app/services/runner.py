"""Job runner: one tracked asyncio task per job — download slot → free-disk
check → download → finalize state. Every failure lands in the job record as a
typed AppError; nothing escapes silently (Constitution III/IV)."""

from __future__ import annotations

import asyncio
import logging
import shutil

from app.models.errors import AppError, ErrorCode
from app.services import downloader
from app.services.jobs import JobState, JobStore

logger = logging.getLogger("app.runner")


async def check_admission(chash: str, store: JobStore, settings) -> None:
    """Admission control (FR-008): called by POST /api/download BEFORE any job
    record is created, so a rejected request leaves no trace. Per-client quota
    is checked first — the client-specific answer wins over the global one."""
    if await store.active_count(chash) >= settings.max_jobs_per_client:
        raise AppError(
            ErrorCode.TOO_MANY_JOBS,
            "You already have the maximum number of active downloads. "
            "Wait for one to finish before starting another.",
        )
    capacity = settings.max_concurrent_downloads + settings.max_queued_jobs
    if await store.global_active() >= capacity:
        raise AppError(
            ErrorCode.SERVER_BUSY,
            "The server is at full capacity right now. Try again in a moment.",
        )


def _check_free_disk(settings) -> None:
    free_bytes = shutil.disk_usage(settings.downloads_dir).free
    if free_bytes < settings.min_free_disk_mb * 1024 * 1024:
        raise AppError(
            ErrorCode.INSUFFICIENT_STORAGE,
            "The server does not have enough free disk space right now.",
        )


async def run_job(job_id: str, info, state) -> None:
    """`state` is the FastAPI app.state (settings, pools, jobs, extractor)."""
    store: JobStore = state.jobs
    job = await store.get(job_id)
    if job is None:  # purged before it ever started
        return
    try:
        async with state.pools.download_sem:
            await store.transition(job_id, JobState.RUNNING)
            _check_free_disk(state.settings)
            final_path = await downloader.perform_download(
                job, info, state.extractor, state.pools, state.settings
            )
            job.bump_progress(100)
            await store.transition(job_id, JobState.COMPLETED, file_path=final_path)
    except AppError as err:
        await _fail(store, job_id, err)
    except Exception:
        logger.exception("job %s crashed", job_id)
        await _fail(
            store,
            job_id,
            AppError(ErrorCode.INTERNAL_ERROR, "The download failed unexpectedly."),
        )


async def _fail(store: JobStore, job_id: str, err: AppError) -> None:
    try:
        await store.transition(job_id, JobState.FAILED, error=err)
    except AppError:
        # Job purged mid-flight or already terminal — nothing left to record.
        logger.warning("could not record failure for job %s", job_id)


def spawn_job(job_id: str, info, state) -> asyncio.Task:
    """Create and track the runner task; log any error the task machinery
    itself raises (the job body never lets one escape)."""
    task = asyncio.create_task(run_job(job_id, info, state), name=f"job-{job_id}")
    state.tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        state.tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("job task %s died: %r", job_id, t.exception())

    task.add_done_callback(_done)
    return task
