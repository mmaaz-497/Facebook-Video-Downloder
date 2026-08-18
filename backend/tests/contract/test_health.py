"""US4 T040: /api/health contract — schema per contracts/api.yaml, degraded
status when FFmpeg is absent (and download submissions then fail
FFMPEG_MISSING), accepting_new false at capacity with accurate counts."""

from __future__ import annotations

import threading

import pytest

from app.config import get_settings
from app.models.errors import ErrorCode
from tests.conftest import poll_until

GOOD_URL = "https://www.facebook.com/watch?v=1234567890"

HEALTH_KEYS = {"status", "ffmpeg", "extractor", "running", "queued", "accepting_new"}


@pytest.fixture
def settings(tmp_path):
    # Tiny capacity (1 running + 1 queued) so a health check can observe it full.
    return get_settings(
        downloads_dir=tmp_path / "downloads",
        ip_hash_salt="test-salt",
        cleanup_interval_seconds=3600,
        max_concurrent_downloads=1,
        max_queued_jobs=1,
        max_jobs_per_client=10,
    )


async def test_health_schema_all_ok(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == HEALTH_KEYS
    assert body["status"] == "ok"
    assert body["ffmpeg"] is True
    assert body["extractor"] is True
    assert body["running"] == 0
    assert body["queued"] == 0
    assert body["accepting_new"] is True


async def test_ffmpeg_missing_degrades_health_and_blocks_downloads(
    client, app, monkeypatch
):
    from app.api import routes

    monkeypatch.setattr(routes, "_ffmpeg_available", lambda: False)

    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ffmpeg"] is False
    assert body["status"] == "degraded"

    # Spec US4 scenario 1: submissions fail with FFMPEG_MISSING, no job created.
    resp = await client.post(
        "/api/download", json={"url": GOOD_URL, "resolution": 720}
    )
    assert resp.status_code == 500
    error = resp.json()
    assert set(error) == {"code", "message"}
    assert error["code"] == ErrorCode.FFMPEG_MISSING.value
    assert await app.state.jobs.global_active() == 0


async def test_extractor_self_check_failure_degrades_health(client, fake_extractor):
    fake_extractor.self_check_ok = False
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["extractor"] is False
    assert body["status"] == "degraded"


async def test_accepting_new_false_at_capacity_with_accurate_counts(
    client, fake_extractor
):
    gate = threading.Event()
    fake_extractor.download_gate = gate

    job_ids = []
    for _ in range(2):  # capacity = 1 running + 1 queued
        resp = await client.post(
            "/api/download", json={"url": GOOD_URL, "resolution": 720}
        )
        assert resp.status_code == 202, resp.text
        job_ids.append(resp.json()["job_id"])
    await poll_until(client, job_ids[0], {"running"})

    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] == 1
    assert body["queued"] == 1
    assert body["accepting_new"] is False
    # Load alone never marks the service unhealthy.
    assert body["status"] == "ok"

    gate.set()
    for job_id in job_ids:
        await poll_until(client, job_id, {"completed"})

    resp = await client.get("/api/health")
    body = resp.json()
    assert body["accepting_new"] is True
    assert (body["running"], body["queued"]) == (0, 0)
