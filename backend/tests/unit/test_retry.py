"""US2 T029: retry policy for the download orchestration (FR-010/FR-011).

- Expired signed CDN URL (HTTP 403, surfaced as an AppError flagged
  signed_url_expired): exactly ONE re-extract, then one retry, then fail.
- Transient codes (NETWORK_ERROR, RATE_LIMITED): retried at most 2 times with
  jittered backoff.
- Permanent codes: fail immediately, zero retries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.models.errors import AppError, ErrorCode
from app.services import downloader
from app.services.jobs import Job
from tests.conftest import InlinePools, make_video


class ScriptedExtractor:
    """Wrapper-interface stand-in whose download() consumes a script of
    outcomes: an Exception instance (raised) or "ok" (writes the output)."""

    def __init__(self, outcomes, videos=None):
        self.outcomes = list(outcomes)
        # extract_info returns videos in order (re-extract may see new formats)
        self.videos = list(videos) if videos else [make_video()]
        self.extract_calls = 0
        self.download_calls = 0

    def extract_info(self, url: str):
        self.extract_calls += 1
        video = self.videos.pop(0) if len(self.videos) > 1 else self.videos[0]
        return video

    def download(self, url, format_id, output_stem, progress_cb, byte_cap) -> Path:
        self.download_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        progress_cb("video", 100, 100)
        out = Path(f"{output_stem}.mp4")
        out.write_bytes(b"fake-mp4")
        return out


@pytest.fixture
def settings(tmp_path):
    return get_settings(downloads_dir=tmp_path / "downloads", ip_hash_salt="s")


@pytest.fixture
def job():
    return Job(
        client_hash="c", url="https://www.facebook.com/watch?v=1",
        video_id="1", requested_height=720, title="t",
    )


@pytest.fixture
def sleeps(monkeypatch):
    """Capture backoff delays; no real sleeping."""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(downloader, "_sleep", fake_sleep)
    return recorded


def expired_403() -> AppError:
    return AppError(
        ErrorCode.NETWORK_ERROR,
        "The video's download link expired.",
        signed_url_expired=True,
    )


async def test_fr010_one_reextract_then_retry_succeeds(settings, job, sleeps):
    ex = ScriptedExtractor([expired_403(), "ok"])
    path = await downloader.perform_download(
        job, make_video(), ex, InlinePools(), settings
    )
    assert path.exists()
    assert ex.extract_calls == 1  # exactly one re-extract
    assert ex.download_calls == 2  # original attempt + one retry


async def test_fr010_second_403_fails_without_further_retry(settings, job, sleeps):
    ex = ScriptedExtractor([expired_403(), expired_403()])
    with pytest.raises(AppError) as excinfo:
        await downloader.perform_download(
            job, make_video(), ex, InlinePools(), settings
        )
    assert excinfo.value.code is ErrorCode.NETWORK_ERROR
    assert ex.extract_calls == 1  # re-extracted exactly once, never again
    assert ex.download_calls == 2  # no third attempt for the 403 path


async def test_fr010_reextract_losing_the_height_fails_typed(settings, job, sleeps):
    # After re-extract the requested 720p no longer exists -> typed failure,
    # never a silent substitution.
    ex = ScriptedExtractor(
        [expired_403(), "ok"],
        videos=[make_video(heights=(360,)), make_video(heights=(360,))],
    )
    with pytest.raises(AppError) as excinfo:
        await downloader.perform_download(
            job, make_video(), ex, InlinePools(), settings
        )
    assert excinfo.value.code is ErrorCode.RESOLUTION_UNAVAILABLE
    assert ex.download_calls == 1


async def test_fr011_transient_retried_at_most_twice_then_success(
    settings, job, sleeps
):
    ex = ScriptedExtractor(
        [
            AppError(ErrorCode.NETWORK_ERROR, "net down"),
            AppError(ErrorCode.NETWORK_ERROR, "net down"),
            "ok",
        ]
    )
    path = await downloader.perform_download(
        job, make_video(), ex, InlinePools(), settings
    )
    assert path.exists()
    assert ex.download_calls == 3  # initial + 2 retries
    assert ex.extract_calls == 0  # plain transient failures never re-extract
    assert len(sleeps) == 2
    # Jittered exponential backoff: base 1s then 2s, plus up to 0.5s jitter.
    assert 1.0 <= sleeps[0] <= 1.5
    assert 2.0 <= sleeps[1] <= 2.5


async def test_fr011_transient_budget_exhausted_fails(settings, job, sleeps):
    ex = ScriptedExtractor([AppError(ErrorCode.NETWORK_ERROR, "net down")] * 4)
    with pytest.raises(AppError) as excinfo:
        await downloader.perform_download(
            job, make_video(), ex, InlinePools(), settings
        )
    assert excinfo.value.code is ErrorCode.NETWORK_ERROR
    assert ex.download_calls == 3  # never more than 2 retries


async def test_fr011_rate_limited_is_transient(settings, job, sleeps):
    ex = ScriptedExtractor(
        [AppError(ErrorCode.RATE_LIMITED, "throttled", retry_after=1), "ok"]
    )
    path = await downloader.perform_download(
        job, make_video(), ex, InlinePools(), settings
    )
    assert path.exists()
    assert ex.download_calls == 2


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.VIDEO_UNAVAILABLE,
        ErrorCode.AUTH_REQUIRED,
        ErrorCode.EXTRACTION_FAILED,
        ErrorCode.UNSUPPORTED_URL,
    ],
)
async def test_fr011_permanent_codes_fail_immediately(settings, job, sleeps, code):
    ex = ScriptedExtractor([AppError(code, "permanent failure")])
    with pytest.raises(AppError) as excinfo:
        await downloader.perform_download(
            job, make_video(), ex, InlinePools(), settings
        )
    assert excinfo.value.code is code
    assert ex.download_calls == 1  # zero retries
    assert ex.extract_calls == 0
    assert sleeps == []
