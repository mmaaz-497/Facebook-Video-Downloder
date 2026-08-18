"""US3 T035: admission control — per-client quota (TOO_MANY_JOBS 429), global
backpressure (SERVER_BUSY 503), both rejected BEFORE any job record exists
(FR-008: no trace), and client isolation via distinct salted hashes."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

import httpx

from app.config import get_settings
from app.main import create_app
from app.models.errors import ErrorCode
from tests.conftest import FakeExtractor, poll_until

GOOD_URL = "https://www.facebook.com/watch?v=1234567890"


def _image_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"img", headers={"content-type": "image/png"})


@asynccontextmanager
async def make_app(tmp_path, fake: FakeExtractor, **overrides):
    settings = get_settings(
        downloads_dir=tmp_path / "downloads",
        ip_hash_salt="test-salt",
        cleanup_interval_seconds=3600,
        **overrides,
    )
    app = create_app(
        settings=settings,
        extractor=fake,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_image_handler)),
    )
    try:
        yield app
    finally:
        await app.state.http.aclose()
        app.state.pools.shutdown()


@asynccontextmanager
async def client_for(app, ip: str):
    transport = httpx.ASGITransport(app=app, client=(ip, 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


async def submit(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/download", json={"url": GOOD_URL, "resolution": 720}
    )


async def test_fourth_job_per_client_rejected_with_no_trace(tmp_path):
    fake = FakeExtractor()
    gate = threading.Event()
    fake.download_gate = gate
    async with make_app(
        tmp_path,
        fake,
        max_jobs_per_client=3,
        max_concurrent_downloads=5,
        max_queued_jobs=10,
    ) as app:
        async with client_for(app, "203.0.113.7") as client:
            job_ids = []
            for _ in range(3):
                resp = await submit(client)
                assert resp.status_code == 202, resp.text
                job_ids.append(resp.json()["job_id"])

            resp = await submit(client)
            assert resp.status_code == 429
            body = resp.json()
            assert set(body) == {"code", "message"}
            assert body["code"] == ErrorCode.TOO_MANY_JOBS.value
            # FR-008: the rejected request left no trace in the store.
            assert await app.state.jobs.global_active() == 3

            gate.set()
            for job_id in job_ids:
                await poll_until(client, job_id, {"completed"})


async def test_distinct_clients_have_isolated_quotas(tmp_path):
    fake = FakeExtractor()
    gate = threading.Event()
    fake.download_gate = gate
    async with make_app(
        tmp_path,
        fake,
        max_jobs_per_client=1,
        max_concurrent_downloads=5,
        max_queued_jobs=10,
    ) as app:
        async with client_for(app, "203.0.113.7") as client_a, client_for(
            app, "198.51.100.9"
        ) as client_b:
            resp_a = await submit(client_a)
            assert resp_a.status_code == 202

            # Client A is at quota; client B is unaffected.
            resp = await submit(client_a)
            assert resp.status_code == 429
            assert resp.json()["code"] == ErrorCode.TOO_MANY_JOBS.value

            resp_b = await submit(client_b)
            assert resp_b.status_code == 202, resp_b.text

            gate.set()
            await poll_until(client_a, resp_a.json()["job_id"], {"completed"})
            await poll_until(client_b, resp_b.json()["job_id"], {"completed"})


async def test_global_capacity_rejects_with_server_busy_and_no_trace(tmp_path):
    fake = FakeExtractor()
    gate = threading.Event()
    fake.download_gate = gate
    async with make_app(
        tmp_path,
        fake,
        max_jobs_per_client=10,
        max_concurrent_downloads=1,
        max_queued_jobs=1,
    ) as app:
        async with client_for(app, "203.0.113.7") as client_a, client_for(
            app, "198.51.100.9"
        ) as client_b:
            job_ids = []
            for _ in range(2):  # capacity = 1 running + 1 queued
                resp = await submit(client_a)
                assert resp.status_code == 202, resp.text
                job_ids.append(resp.json()["job_id"])

            # A DIFFERENT client is rejected too: the cap is global.
            resp = await submit(client_b)
            assert resp.status_code == 503
            body = resp.json()
            assert set(body) == {"code", "message"}
            assert body["code"] == ErrorCode.SERVER_BUSY.value
            # No trace: the store still holds exactly the two admitted jobs.
            assert await app.state.jobs.global_active() == 2
            running, queued = await app.state.jobs.counts()
            assert running + queued == 2

            gate.set()
            for job_id in job_ids:
                await poll_until(client_a, job_id, {"completed"})
