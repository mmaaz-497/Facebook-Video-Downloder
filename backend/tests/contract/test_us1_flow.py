"""US1 happy path (spec scenarios 1–5), end-to-end against the FakeExtractor:
inspect → exact resolutions → download 202 → poll to completed → collect with
sanitized Content-Disposition → purge verified on disk and via JOB_NOT_FOUND."""

from __future__ import annotations

import re

from tests.conftest import make_video, poll_until

WATCH_URL = "https://www.facebook.com/watch?v=1234567890"


async def test_inspect_reports_exact_extractor_resolutions(client, fake_extractor):
    fake_extractor.video = make_video(heights=(720, 360))
    resp = await client.get("/api/info", params={"url": WATCH_URL})
    assert resp.status_code == 200
    body = resp.json()
    assert body["video_id"] == "1234567890"
    assert body["title"] == "A Test Video"
    assert body["duration_seconds"] == 180.0
    assert body["width"] == 1280 and body["height"] == 720
    # Proxy URL only — never a Facebook CDN URL (FR-006)
    assert body["thumbnail_url"] == "/api/thumb?video_id=1234567890"
    assert "scontent" not in body["thumbnail_url"]
    # Exactly the extractor-reported set, highest first — never a fixed menu
    assert body["resolutions"] == [
        {"height": 720, "label": "720p (HD)"},
        {"height": 360, "label": "360p"},
    ]


async def test_inspect_single_resolution_still_selectable(client, fake_extractor):
    fake_extractor.video = make_video(heights=(480,))
    resp = await client.get("/api/info", params={"url": WATCH_URL})
    assert resp.status_code == 200
    assert resp.json()["resolutions"] == [{"height": 480, "label": "480p"}]


async def test_inspect_reel_reports_portrait_dimensions(client, fake_extractor):
    fake_extractor.video = make_video(heights=(720,), width=720, height=1280)
    resp = await client.get(
        "/api/info", params={"url": "https://www.facebook.com/reel/1234567890"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert (body["width"], body["height"]) == (720, 1280)


async def test_full_flow_download_deliver_purge(client, app, fake_extractor):
    fake_extractor.video = make_video(heights=(720, 360))

    # 1. inspect (also warms the cache)
    resp = await client.get("/api/info", params={"url": WATCH_URL})
    assert resp.status_code == 200

    # 2. start download at an offered height
    resp = await client.post(
        "/api/download", json={"url": WATCH_URL, "resolution": 720}
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", job_id)

    # 3. poll to completion; progress must be monotonic 0-100
    status = await poll_until(client, job_id, {"completed", "failed"})
    assert status["state"] == "completed"
    assert status["progress"] == 100
    assert status["error"] is None

    # 4. collect the file
    resp = await client.get(f"/api/file/{job_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("video/mp4")
    disposition = resp.headers["content-disposition"]
    assert 'filename="A Test Video.mp4"' in disposition
    assert resp.content == fake_extractor.file_bytes

    # On-disk name was always the server-generated job id, never the title
    assert fake_extractor.downloaded_paths, "extractor was never asked to download"
    on_disk = fake_extractor.downloaded_paths[0].name
    assert on_disk.startswith(job_id)
    assert "Test" not in on_disk

    # 5. purge after delivery: no files on disk, record gone (JOB_NOT_FOUND)
    downloads_dir = app.state.settings.downloads_dir
    leftovers = list(downloads_dir.glob("*"))
    assert leftovers == [], f"files not purged: {leftovers}"

    resp = await client.get(f"/api/status/{job_id}")
    assert resp.status_code == 404
    assert resp.json() == {
        "code": "JOB_NOT_FOUND",
        "message": resp.json()["message"],
    }

    resp = await client.get(f"/api/file/{job_id}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "JOB_NOT_FOUND"


async def test_completes_when_actual_output_differs_from_canonical(
    client, app, fake_extractor
):
    """Regression: yt-dlp names its own output (extension normalization, DASH
    merge) — finalize must rename the wrapper-reported actual path to
    <job_id>.mp4 instead of assuming a .part file exists."""
    fake_extractor.output_suffix = ".f720.mp4"

    resp = await client.post(
        "/api/download", json={"url": WATCH_URL, "resolution": 720}
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status = await poll_until(client, job_id, {"completed", "failed"})
    assert status["state"] == "completed", status

    resp = await client.get(f"/api/file/{job_id}")
    assert resp.status_code == 200
    assert resp.content == fake_extractor.file_bytes

    # Delivery purge left nothing — neither canonical nor the divergent name
    downloads_dir = app.state.settings.downloads_dir
    assert list(downloads_dir.glob("*")) == []


async def test_failure_removes_all_yt_dlp_intermediates(
    client, app, fake_extractor
):
    """On failure, cleanup must glob <job_id>.* — yt-dlp can leave .part,
    .f*.mp4 fragment files, .m4a, and .ytdl, not just the canonical names."""
    from app.models.errors import AppError, ErrorCode

    fake_extractor.intermediate_suffixes = [
        ".f720.mp4.part",
        ".f720.mp4",
        ".m4a",
        ".mp4.ytdl",
    ]
    fake_extractor.download_error = AppError(
        ErrorCode.NETWORK_ERROR, "A network error occurred talking to Facebook."
    )

    resp = await client.post(
        "/api/download", json={"url": WATCH_URL, "resolution": 720}
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status = await poll_until(client, job_id, {"completed", "failed"})
    assert status["state"] == "failed"

    downloads_dir = app.state.settings.downloads_dir
    leftovers = list(downloads_dir.glob(f"{job_id}*"))
    assert leftovers == [], f"intermediates not cleaned: {leftovers}"


async def test_file_before_completion_is_409_not_ready(client, fake_extractor):
    import threading

    fake_extractor.download_gate = threading.Event()  # hold the download open
    resp = await client.post(
        "/api/download", json={"url": WATCH_URL, "resolution": 720}
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    resp = await client.get(f"/api/file/{job_id}")
    assert resp.status_code == 409
    assert resp.json()["code"] == "JOB_NOT_READY"

    fake_extractor.download_gate.set()  # let the job finish so teardown is clean
    await poll_until(client, job_id, {"completed", "failed"})


async def test_unknown_job_id_is_404(client):
    resp = await client.get(f"/api/status/{'0' * 32}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "JOB_NOT_FOUND"

    resp = await client.get(f"/api/file/{'0' * 32}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "JOB_NOT_FOUND"


async def test_thumbnail_served_through_proxy(client, fake_extractor):
    await client.get("/api/info", params={"url": WATCH_URL})  # warm cache
    resp = await client.get("/api/thumb", params={"video_id": "1234567890"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")

    resp = await client.get("/api/thumb", params={"video_id": "not-cached"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "VIDEO_UNAVAILABLE"
