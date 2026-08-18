"""Shared fixtures: app built via create_app() with a FakeExtractor injected in
place of the real yt-dlp wrapper, an httpx AsyncClient over ASGI, and a tmp
DOWNLOADS_DIR per test. Zero live network calls anywhere in the suite."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.config import get_settings
from app.main import create_app
from app.models.errors import AppError


@dataclass
class FakeVideo:
    """Duck-typed stand-in for services.extractor.ExtractedVideo — services
    only ever read these attributes through the wrapper interface."""

    video_id: str = "1234567890"
    title: str = "A Test Video"
    thumbnail_url: str = "https://scontent.example/thumb.jpg"
    duration_seconds: float = 180.0
    width: int = 1280
    height: int = 720
    formats: tuple = (
        # (height, label, format_id) triples via SimpleFormat below
    )


@dataclass(frozen=True)
class SimpleFormat:
    height: int
    label: str
    format_id: str


def make_video(heights: tuple[int, ...] = (720, 360), **kw: Any) -> FakeVideo:
    v = FakeVideo(**kw)
    v.formats = tuple(
        SimpleFormat(
            height=h,
            label=f"{h}p (HD)" if h >= 720 else f"{h}p",
            format_id=f"fmt-{h}",
        )
        for h in sorted(heights, reverse=True)
    )
    return v


class FakeExtractor:
    """Configurable stand-in for the extractor wrapper interface:
    extract_info / download / self_check, all sync (executor-run like the
    real one). Script results, AppError raises, and progress callbacks."""

    def __init__(self) -> None:
        self.video: FakeVideo = make_video()
        self.info_error: AppError | None = None
        self.download_error: AppError | None = None
        # (phase, downloaded_bytes, total_bytes) events fed to progress_cb
        self.progress_script: list[tuple[str, int, int | None]] = [
            ("video", 50, 100),
            ("video", 100, 100),
            ("audio", 100, 100),
            ("mux", 1, 1),
        ]
        self.file_bytes: bytes = b"\x00\x00\x00\x18ftypmp42fake-mp4-payload"
        # Suffix appended to the output stem for the file the fake "actually"
        # produces — override to simulate yt-dlp writing somewhere other than
        # the canonical <job_id>.mp4 (e.g. ".f720.mp4").
        self.output_suffix: str = ".mp4"
        # Extra <stem><suffix> files written before success/error, simulating
        # yt-dlp intermediates (.part, .f*.mp4 fragments, .m4a, .ytdl).
        self.intermediate_suffixes: list[str] = []
        self.extract_calls = 0
        self.download_calls = 0
        self.downloaded_paths: list[Path] = []
        self.extract_started = threading.Event()
        self.extract_gate: threading.Event | None = None  # block extraction when set
        self.download_gate: threading.Event | None = None  # block download when set
        self.step_delay: float = 0.0  # pause between progress events (pacing)
        self.self_check_ok: bool = True

    # -- wrapper interface -------------------------------------------------
    def extract_info(self, url: str):
        self.extract_calls += 1
        self.extract_started.set()
        if self.extract_gate is not None:
            self.extract_gate.wait(timeout=10)
        if self.info_error is not None:
            raise self.info_error
        return self.video

    def download(
        self,
        url: str,
        format_id: str,
        output_stem: Path,
        progress_cb: Callable[[str, int, int | None], None],
        byte_cap: int,
    ) -> Path:
        self.download_calls += 1
        if self.download_gate is not None:
            self.download_gate.wait(timeout=10)
        for suffix in self.intermediate_suffixes:
            Path(f"{output_stem}{suffix}").write_bytes(b"intermediate")
        if self.download_error is not None:
            raise self.download_error
        total = 0
        for phase, downloaded, total_bytes in self.progress_script:
            if self.step_delay:
                time.sleep(self.step_delay)
            if phase in ("video", "audio"):
                total = max(total, downloaded)
            progress_cb(phase, downloaded, total_bytes)
        outpath = Path(f"{output_stem}{self.output_suffix}")
        outpath.write_bytes(self.file_bytes)
        self.downloaded_paths.append(outpath)
        return outpath

    def self_check(self) -> bool:
        return self.self_check_ok


class InlinePools:
    """Pools stand-in for unit tests: runs wrapper calls inline on the event
    loop thread — no executors, fully deterministic."""

    async def run_extract(self, fn, *args):
        return fn(*args)

    async def run_download(self, fn, *args):
        return fn(*args)


@pytest.fixture(autouse=True)
def _ffmpeg_present(monkeypatch):
    """Suite determinism: never depend on the host machine having FFmpeg.
    Tests that need it absent re-patch _ffmpeg_available to False."""
    from app.api import routes

    monkeypatch.setattr(routes, "_ffmpeg_available", lambda: True, raising=False)


@pytest.fixture
def fake_extractor() -> FakeExtractor:
    return FakeExtractor()


@pytest.fixture
def thumb_transport() -> httpx.MockTransport:
    """Outbound HTTP is always mocked — no test ever leaves the process."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"\x89PNG fake image bytes", headers={"content-type": "image/png"}
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def settings(tmp_path: Path):
    return get_settings(
        downloads_dir=tmp_path / "downloads",
        ip_hash_salt="test-salt",
        cleanup_interval_seconds=3600,
    )


@pytest.fixture
def app(settings, fake_extractor, thumb_transport):
    application = create_app(
        settings=settings,
        extractor=fake_extractor,
        http_client=httpx.AsyncClient(transport=thumb_transport),
    )
    yield application
    application.state.pools.shutdown()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c
    await app.state.http.aclose()


async def poll_until(
    client: httpx.AsyncClient,
    job_id: str,
    states: set[str],
    timeout: float = 10.0,
) -> dict:
    """Poll /api/status until the job reaches one of `states`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        resp = await client.get(f"/api/status/{job_id}")
        if resp.status_code == 200 and resp.json()["state"] in states:
            return resp.json()
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"job {job_id} never reached {states}: {resp.text}")
        await asyncio.sleep(0.02)
