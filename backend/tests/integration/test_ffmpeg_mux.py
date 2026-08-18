"""Integration: the downloader finalize path with a REAL FFmpeg binary.

Two tiny streams (lavfi testsrc video + sine audio) are generated locally and
muxed by real FFmpeg inside a wrapper-shaped extractor, exercising exactly what
yt-dlp's merge postprocessor does — zero network, per the constitutional
testing gate. Skips cleanly when FFmpeg is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import downloader
from app.services.jobs import Job
from tests.conftest import InlinePools, make_video

FFMPEG = shutil.which("ffmpeg")

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(FFMPEG is None, reason="requires a real FFmpeg binary on PATH"),
]


def _run_ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


@pytest.fixture(scope="module")
def streams(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """One second of 128x72 test video and one second of 440 Hz audio."""
    src = tmp_path_factory.mktemp("streams")
    video = src / "video.mp4"
    audio = src / "audio.m4a"
    _run_ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=1:size=128x72:rate=10",
        "-c:v", "mpeg4", str(video),
    )
    _run_ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:a", "aac", str(audio),
    )
    return video, audio


class MuxingExtractor:
    """Wrapper-interface extractor whose download() muxes the two local
    streams with real FFmpeg, writing to a yt-dlp-style intermediate name
    (<stem>.f720.mp4) so the downloader's finalize rename runs for real."""

    def __init__(self, video: Path, audio: Path) -> None:
        self._video = video
        self._audio = audio

    def extract_info(self, url: str):
        return make_video((720,))

    def download(self, url, format_id, output_stem, progress_cb, byte_cap) -> Path:
        video_bytes = self._video.stat().st_size
        audio_bytes = self._audio.stat().st_size
        progress_cb("video", video_bytes, video_bytes)
        progress_cb("audio", audio_bytes, audio_bytes)
        progress_cb("mux", 0, 1)
        outpath = Path(f"{output_stem}.f720.mp4")
        _run_ffmpeg(
            "-i", str(self._video), "-i", str(self._audio),
            "-c", "copy", str(outpath),
        )
        progress_cb("mux", 1, 1)
        return outpath

    def self_check(self) -> bool:
        return True


def _stream_types(path: Path) -> str:
    """ffmpeg -i prints the stream table on stderr (exit code 1 is normal
    when no output file is given)."""
    probe = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return probe.stderr


async def test_real_ffmpeg_mux_through_finalize_path(settings, streams):
    video, audio = streams
    extractor = MuxingExtractor(video, audio)
    info = extractor.extract_info("x")
    job = Job(
        client_hash="c" * 64,
        url="https://www.facebook.com/watch?v=1234567890",
        video_id="1234567890",
        requested_height=720,
        title="Mux Test",
    )

    final_path = await downloader.perform_download(
        job, info, extractor, InlinePools(), settings
    )

    assert final_path == settings.downloads_dir / f"{job.id}.mp4"
    assert final_path.is_file() and final_path.stat().st_size > 0
    # The intermediate .f720.mp4 was renamed away, not copied.
    leftovers = [
        p for p in settings.downloads_dir.iterdir() if p.name != final_path.name
    ]
    assert leftovers == []
    # Both streams made it into the muxed container.
    stream_table = _stream_types(final_path)
    assert "Video:" in stream_table
    assert "Audio:" in stream_table
    # Banded progress ran to the end of the mux band.
    assert job.progress == 100


async def test_mux_failure_cleans_intermediates(settings, streams):
    video, audio = streams

    class BrokenMuxExtractor(MuxingExtractor):
        def download(self, url, format_id, output_stem, progress_cb, byte_cap):
            Path(f"{output_stem}.f720.mp4").write_bytes(b"partial")
            # Real FFmpeg invoked on a nonexistent input: genuine failure.
            _run_ffmpeg("-i", str(video.parent / "missing.mp4"), str(output_stem) + ".mp4")
            raise AssertionError("unreachable")

    extractor = BrokenMuxExtractor(video, audio)
    info = extractor.extract_info("x")
    job = Job(
        client_hash="c" * 64,
        url="https://www.facebook.com/watch?v=1234567890",
        video_id="1234567890",
        requested_height=720,
        title="Mux Failure",
    )

    with pytest.raises(Exception):
        await downloader.perform_download(job, info, extractor, InlinePools(), settings)

    # Every <job_id>.* intermediate was swept on failure.
    assert list(settings.downloads_dir.glob(f"{job.id}.*")) == []
