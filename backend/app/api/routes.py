"""Thin API routes: validate → delegate to services. Every failure raised here
is an AppError rendered by the global handler as the uniform ErrorResponse."""

from __future__ import annotations

import re
import shutil
import unicodedata
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.models.errors import AppError, ErrorCode
from app.models.schemas import (
    DownloadAccepted,
    DownloadRequest,
    ErrorBody,
    HealthResponse,
    JobStatusResponse,
    ResolutionOption,
    VideoInfoResponse,
)
from app.services import runner, thumb
from app.services.jobs import Job, JobState, client_hash
from app.services.url import classify_url, resolve_ref

router = APIRouter(prefix="/api")

# ---------------------------------------------------------------------------
# Filename sanitization (FR-004): the title only ever appears in the
# Content-Disposition header; on-disk names are always <job_id>.mp4.

_FORBIDDEN_CHARS = re.compile(r'[\/\\:*?"<>|]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_filename(title: str) -> str:
    safe = _CONTROL_CHARS.sub("", title)
    safe = _FORBIDDEN_CHARS.sub("", safe)
    safe = safe.replace("..", "").strip().strip(".")
    safe = re.sub(r"\s+", " ", safe)
    return safe or "video"


def content_disposition(title: str) -> str:
    safe = sanitize_filename(title)
    ascii_name = (
        unicodedata.normalize("NFKD", safe).encode("ascii", "ignore").decode("ascii")
    )
    ascii_name = ascii_name.strip() or "video"
    header = f'attachment; filename="{ascii_name}.mp4"'
    if safe != ascii_name:
        header += f"; filename*=UTF-8''{quote(safe, safe='')}.mp4"
    return header


# ---------------------------------------------------------------------------


def _ffmpeg_available() -> bool:
    """Single source of truth for FFmpeg presence: /api/health reports it and
    download submissions are refused without it (muxing would fail anyway)."""
    return shutil.which("ffmpeg") is not None


def _extractor(state):
    if state.extractor is None:
        # Lazy: keeps yt-dlp out of every code path until first real use.
        from app.services.extractor import YtDlpExtractor

        state.extractor = YtDlpExtractor()
    return state.extractor


async def _resolve_and_extract(state, url: str):
    """classify → (resolve short links) → cache/extract. Returns
    (canonical_video_id, direct_url, ExtractedVideo)."""
    ref = classify_url(url)
    if ref.kind == "short":
        ref = await resolve_ref(ref.url, state.http)
    video_id = ref.video_id
    extractor = _extractor(state)

    async def factory():
        return await state.pools.run_extract(extractor.extract_info, ref.url)

    info = await state.info_cache.get_or_create(video_id, factory)
    return video_id, ref.url, info


def _dedupe_resolutions(info) -> list[ResolutionOption]:
    seen: dict[int, str] = {}
    for fmt in info.formats:
        seen.setdefault(fmt.height, fmt.label)
    return [
        ResolutionOption(height=h, label=seen[h])
        for h in sorted(seen, reverse=True)
    ]


@router.get("/info", response_model=VideoInfoResponse)
async def get_info(url: str, request: Request) -> VideoInfoResponse:
    state = request.app.state
    video_id, _direct_url, info = await _resolve_and_extract(state, url)
    return VideoInfoResponse(
        video_id=video_id,
        title=info.title,
        thumbnail_url=f"/api/thumb?video_id={video_id}",
        duration_seconds=info.duration_seconds,
        width=info.width,
        height=info.height,
        resolutions=_dedupe_resolutions(info),
    )


@router.post("/download", response_model=DownloadAccepted, status_code=202)
async def start_download(body: DownloadRequest, request: Request) -> DownloadAccepted:
    state = request.app.state
    if not _ffmpeg_available():
        raise AppError(
            ErrorCode.FFMPEG_MISSING,
            "FFmpeg is not installed on the server, so downloads are disabled.",
        )
    peer_ip = request.client.host if request.client else "unknown"
    chash = client_hash(peer_ip, state.settings.ip_hash_salt)
    # Admission BEFORE extraction: rejection stays fast (< 1 s, SC-007) and
    # leaves no trace (FR-008 — no job record is ever created for it).
    await runner.check_admission(chash, state.jobs, state.settings)

    video_id, direct_url, info = await _resolve_and_extract(state, body.url)

    if body.resolution not in {f.height for f in info.formats}:
        # No job record is created for a rejected request.
        raise AppError(
            ErrorCode.RESOLUTION_UNAVAILABLE,
            f"{body.resolution}p is not offered for this video. "
            "Pick one of the listed resolutions.",
        )

    # Re-check: capacity may have filled while extraction awaited.
    await runner.check_admission(chash, state.jobs, state.settings)
    job = Job(
        client_hash=chash,
        url=direct_url,
        video_id=video_id,
        requested_height=body.resolution,
        title=info.title,
    )
    await state.jobs.create(job)
    runner.spawn_job(job.id, info, state)
    return DownloadAccepted(job_id=job.id)


@router.get("/status/{job_id}", response_model=JobStatusResponse)
async def get_status(job_id: str, request: Request) -> JobStatusResponse:
    state = request.app.state
    job = await state.jobs.get(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND, "Unknown or already-purged job.")
    return JobStatusResponse(
        job_id=job.id,
        state=job.state.value,
        progress=job.progress,
        queue_position=await state.jobs.queue_position(job),
        error=(
            ErrorBody(code=job.error.code.value, message=job.error.message)
            if job.error is not None
            else None
        ),
    )


@router.get("/file/{job_id}")
async def collect_file(job_id: str, request: Request) -> FileResponse:
    state = request.app.state
    job = await state.jobs.get(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND, "Unknown or already-purged job.")
    if job.state is not JobState.COMPLETED or job.file_path is None:
        raise AppError(
            ErrorCode.JOB_NOT_READY, "The job has not completed yet."
        )

    async def purge_after_delivery() -> None:
        # Runs after the last response byte is sent (research decision 6).
        await state.jobs.purge(job.id)

    return FileResponse(
        job.file_path,
        media_type="video/mp4",
        headers={"Content-Disposition": content_disposition(job.title)},
        background=BackgroundTask(purge_after_delivery),
    )


@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    """FR-005: liveness, FFmpeg presence, extractor self-check (never a live
    Facebook probe), running/queued counts, accepting_new."""
    state = request.app.state
    ffmpeg_ok = _ffmpeg_available()
    try:
        extractor_ok = bool(
            await state.pools.run_extract(_extractor(state).self_check)
        )
    except Exception:
        extractor_ok = False
    running, queued = await state.jobs.counts()
    capacity = (
        state.settings.max_concurrent_downloads + state.settings.max_queued_jobs
    )
    return HealthResponse(
        status="ok" if (ffmpeg_ok and extractor_ok) else "degraded",
        ffmpeg=ffmpeg_ok,
        extractor=extractor_ok,
        running=running,
        queued=queued,
        accepting_new=(running + queued) < capacity,
    )


@router.get("/thumb")
async def get_thumb(video_id: str, request: Request) -> Response:
    state = request.app.state
    content, media_type = await thumb.fetch_thumbnail(
        video_id, state.info_cache, state.http
    )
    return Response(content=content, media_type=media_type)
