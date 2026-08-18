"""US2 T030: MAX_DOWNLOAD_BYTES is enforced from CUMULATIVE progress-reported
bytes mid-stream — the Content-Length header (total_bytes) is never trusted.
On breach the job fails and every partial file is deleted.

The taxonomy has no dedicated code for a byte-cap breach; it maps to
EXTRACTION_FAILED by design (the 15-code taxonomy is contractual).
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.models.errors import AppError, ErrorCode
from app.services import downloader
from app.services.jobs import Job
from tests.conftest import FakeExtractor, InlinePools, make_video

CAP = 1000


@pytest.fixture
def settings(tmp_path):
    return get_settings(
        downloads_dir=tmp_path / "downloads",
        ip_hash_salt="s",
        max_download_bytes=CAP,
    )


@pytest.fixture
def job():
    return Job(
        client_hash="c", url="https://www.facebook.com/watch?v=1",
        video_id="1", requested_height=720, title="t",
    )


async def run_expecting_cap_abort(fake: FakeExtractor, job: Job, settings) -> AppError:
    with pytest.raises(AppError) as excinfo:
        await downloader.perform_download(
            job, make_video(), fake, InlinePools(), settings
        )
    return excinfo.value


async def test_cap_breach_mid_stream_fails_and_deletes_partials(settings, job):
    fake = FakeExtractor()
    fake.intermediate_suffixes = [".part"]
    # total_bytes deliberately absent (None): only cumulative downloaded bytes
    # may drive the abort — Content-Length is never consulted.
    fake.progress_script = [("video", 600, None), ("video", 1200, None)]
    err = await run_expecting_cap_abort(fake, job, settings)
    assert err.code is ErrorCode.EXTRACTION_FAILED
    assert "size" in err.message.lower() or "large" in err.message.lower()
    # Every <job_id>.* partial is gone.
    assert list(settings.downloads_dir.glob(f"{job.id}*")) == []


async def test_cap_ignores_lying_content_length(settings, job):
    # The claimed total says "tiny download" while actual bytes blow the cap:
    # the abort must still fire (header never trusted).
    fake = FakeExtractor()
    fake.intermediate_suffixes = [".part"]
    fake.progress_script = [("video", 500, 10), ("video", 1500, 10)]
    err = await run_expecting_cap_abort(fake, job, settings)
    assert err.code is ErrorCode.EXTRACTION_FAILED
    assert list(settings.downloads_dir.glob(f"{job.id}*")) == []


async def test_cap_is_cumulative_across_video_and_audio_streams(settings, job):
    # 600 (video) + 500 (audio) = 1100 > 1000: breach happens on the audio
    # stream even though neither stream alone exceeds the cap.
    fake = FakeExtractor()
    fake.intermediate_suffixes = [".f720.mp4"]
    fake.progress_script = [
        ("video", 600, 600),
        ("audio", 500, None),
    ]
    err = await run_expecting_cap_abort(fake, job, settings)
    assert err.code is ErrorCode.EXTRACTION_FAILED
    assert list(settings.downloads_dir.glob(f"{job.id}*")) == []


async def test_under_cap_download_succeeds(settings, job):
    fake = FakeExtractor()
    fake.progress_script = [("video", 400, 400), ("audio", 300, 300), ("mux", 1, 1)]
    path = await downloader.perform_download(
        job, make_video(), fake, InlinePools(), settings
    )
    assert path.exists()
    assert job.progress > 0


async def test_cap_breach_is_permanent_never_retried(settings, job, monkeypatch):
    # EXTRACTION_FAILED is a permanent code: exactly one download attempt.
    fake = FakeExtractor()
    fake.progress_script = [("video", 2000, None)]
    await run_expecting_cap_abort(fake, job, settings)
    assert fake.download_calls == 1
