"""US3 T036: queue behavior — queue_position is present only while queued and
never increases across polls; progress is monotonic across band transitions
under repeated polling (spec US3 scenarios 3-4)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.config import get_settings
from tests.conftest import poll_until

GOOD_URL = "https://www.facebook.com/watch?v=1234567890"


@pytest.fixture
def settings(tmp_path):
    # One download slot so submitted jobs actually queue.
    return get_settings(
        downloads_dir=tmp_path / "downloads",
        ip_hash_salt="test-salt",
        cleanup_interval_seconds=3600,
        max_concurrent_downloads=1,
        max_queued_jobs=10,
        max_jobs_per_client=10,
    )


async def submit(client) -> str:
    resp = await client.post(
        "/api/download", json={"url": GOOD_URL, "resolution": 720}
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["job_id"]


async def test_queue_position_only_while_queued_and_non_increasing(
    client, fake_extractor
):
    gate = threading.Event()
    fake_extractor.download_gate = gate

    first = await submit(client)
    second = await submit(client)
    third = await submit(client)

    # The first job holds the single slot (running); the rest are queued.
    status = await poll_until(client, first, {"running"})
    assert status["queue_position"] is None  # never present once running

    status2 = (await client.get(f"/api/status/{second}")).json()
    status3 = (await client.get(f"/api/status/{third}")).json()
    assert status2["state"] == "queued" and status2["queue_position"] == 1
    assert status3["state"] == "queued" and status3["queue_position"] == 2

    # Poll the last job to completion: its position must never increase.
    gate.set()
    positions: list[int] = []
    while True:
        status = (await client.get(f"/api/status/{third}")).json()
        if status["state"] != "queued":
            assert status["queue_position"] is None
            if status["state"] in ("completed", "failed"):
                break
        elif status["queue_position"] is not None:
            positions.append(status["queue_position"])
        await asyncio.sleep(0.01)
    assert status["state"] == "completed"
    assert positions == sorted(positions, reverse=True)

    for job_id in (first, second):
        final = (await client.get(f"/api/status/{job_id}")).json()
        assert final["state"] == "completed"
        assert final["queue_position"] is None


async def test_progress_monotonic_across_band_transitions(client, fake_extractor):
    fake_extractor.step_delay = 0.03
    fake_extractor.progress_script = [
        ("video", 25, 100),
        ("video", 60, 100),
        ("video", 100, 100),
        ("audio", 50, 100),
        ("audio", 100, 100),
        # A stale/regressing report must never move the bar backwards.
        ("video", 10, 100),
        ("mux", 1, 1),
    ]
    job_id = await submit(client)

    samples: list[int] = []
    while True:
        resp = await client.get(f"/api/status/{job_id}")
        assert resp.status_code == 200
        status = resp.json()
        samples.append(status["progress"])
        if status["state"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.005)

    assert status["state"] == "completed"
    assert samples == sorted(samples)  # monotonic under repeated polls
    assert samples[-1] == 100
