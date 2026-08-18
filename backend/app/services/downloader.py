"""Download orchestration — talks to the extractor wrapper interface only,
never yt-dlp. Exact-height format pin (RESOLUTION_UNAVAILABLE otherwise — no
silent substitution), banded monotonic progress (video 0–85, audio 85–95, mux
95–100). The wrapper reports the ACTUAL output path yt-dlp produced; finalize
renames it to the canonical <job_id>.mp4 only when they differ. On every
failed attempt each <job_id>.* intermediate yt-dlp may have left (.part,
.f*.mp4 fragments, .m4a, .ytdl) is removed.

Retry policy:
- FR-010: an expired signed CDN URL (HTTP 403, surfaced as an AppError flagged
  ``signed_url_expired``) earns exactly ONE re-extract + retry, then fails.
- FR-011: TRANSIENT_CODES are retried at most twice with jittered exponential
  backoff; permanent codes fail immediately.

MAX_DOWNLOAD_BYTES is enforced here from CUMULATIVE progress-callback bytes
(Content-Length is never trusted), in addition to the wrapper's own hook-level
abort — whichever fires first."""

from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path

from app.models.errors import AppError, ErrorCode, TRANSIENT_CODES
from app.services.jobs import Job

_BANDS: dict[str, tuple[int, int]] = {
    "video": (0, 85),
    "audio": (85, 95),
    "mux": (95, 100),
}

_MAX_TRANSIENT_RETRIES = 2
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_JITTER_SECONDS = 0.5

# Indirection so tests can observe/skip the backoff without real sleeping.
_sleep = asyncio.sleep


def banded_progress(phase: str, downloaded: int, total: int | None) -> int:
    lo, hi = _BANDS.get(phase, (0, 0))
    if not total or total <= 0:
        return lo
    fraction = min(1.0, max(0.0, downloaded / total))
    return lo + int((hi - lo) * fraction)


def _pin_format(info, requested_height: int):
    fmt = next((f for f in info.formats if f.height == requested_height), None)
    if fmt is None:
        raise AppError(
            ErrorCode.RESOLUTION_UNAVAILABLE,
            f"{requested_height}p is not offered for this video.",
        )
    return fmt


async def perform_download(job: Job, info, extractor, pools, settings) -> Path:
    """Download job.requested_height for `job`, returning the final .mp4 path.
    Raises AppError on any failure; the caller finalizes job state."""
    fmt = _pin_format(info, job.requested_height)

    downloads_dir: Path = settings.downloads_dir
    downloads_dir.mkdir(parents=True, exist_ok=True)
    output_stem = downloads_dir / job.id
    final_path = downloads_dir / f"{job.id}.mp4"

    reextracted = False
    transient_retries = 0
    while True:
        try:
            return await _attempt(
                job, fmt, extractor, pools, settings, output_stem, final_path
            )
        except AppError as err:
            if getattr(err, "signed_url_expired", False):
                if reextracted:
                    raise  # FR-010: the one re-extract+retry already happened
                reextracted = True
                info = await pools.run_extract(extractor.extract_info, job.url)
                fmt = _pin_format(info, job.requested_height)
                continue
            if err.code in TRANSIENT_CODES and transient_retries < _MAX_TRANSIENT_RETRIES:
                transient_retries += 1
                delay = _BACKOFF_BASE_SECONDS * 2 ** (transient_retries - 1)
                await _sleep(delay + random.uniform(0, _BACKOFF_JITTER_SECONDS))
                continue
            raise


async def _attempt(
    job: Job,
    fmt,
    extractor,
    pools,
    settings,
    output_stem: Path,
    final_path: Path,
) -> Path:
    """One download attempt: byte-cap-enforcing progress, finalize rename,
    full <job_id>.* cleanup on any failure so a retry starts clean."""
    byte_cap: int = settings.max_download_bytes
    downloads_dir = final_path.parent
    phase_bytes: dict[str, int] = {}  # per-attempt cumulative, per stream

    def progress_cb(phase: str, downloaded: int, total: int | None) -> None:
        # Runs on the download worker thread; bump_progress is single-writer safe.
        if phase in ("video", "audio"):
            phase_bytes[phase] = max(phase_bytes.get(phase, 0), downloaded)
            if sum(phase_bytes.values()) > byte_cap:
                # The 15-code taxonomy is contractual and has no dedicated code
                # for a MAX_DOWNLOAD_BYTES breach: a mid-stream byte-cap abort
                # maps to EXTRACTION_FAILED (permanent — never retried) with a
                # clear message. Driven solely by cumulative progress bytes;
                # Content-Length / total_bytes is never consulted.
                raise AppError(
                    ErrorCode.EXTRACTION_FAILED,
                    "The download exceeded the maximum allowed size and was "
                    "aborted.",
                )
        job.bump_progress(banded_progress(phase, downloaded, total))

    try:
        actual_path = await pools.run_download(
            extractor.download,
            job.url,
            fmt.format_id,
            output_stem,
            progress_cb,
            byte_cap,
        )
        actual_path = Path(actual_path) if actual_path else None
        if (
            actual_path is not None
            and actual_path != final_path
            and actual_path.exists()
        ):
            os.replace(actual_path, final_path)
        if not final_path.exists():
            raise AppError(
                ErrorCode.EXTRACTION_FAILED,
                "The download finished but produced no output file.",
            )
    except BaseException:
        for leftover in downloads_dir.glob(f"{job.id}.*"):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    return final_path
