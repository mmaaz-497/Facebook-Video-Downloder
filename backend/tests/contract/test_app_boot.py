"""Regression tests against the REAL create_app() — no injected fakes — so
route registration, mount order, and error shapes are exercised exactly as a
uvicorn boot would see them. Still zero live network calls: /api/health's
extractor self-check only imports the wrapper, it never probes Facebook."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.main import create_app


@pytest.fixture
async def real_client(tmp_path):
    app = create_app(
        settings=get_settings(
            downloads_dir=tmp_path / "downloads", ip_hash_salt="boot-test"
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client
    await app.state.http.aclose()
    app.state.pools.shutdown()


async def test_health_reachable_on_real_app(real_client):
    resp = await real_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "status",
        "ffmpeg",
        "extractor",
        "running",
        "queued",
        "accepting_new",
    }
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["ffmpeg"], bool)
    assert body["extractor"] is True  # real wrapper self-check, offline
    assert body["running"] == 0
    assert body["queued"] == 0
    assert body["accepting_new"] is True


async def test_unmatched_path_returns_uniform_error_shape(real_client):
    for path in ("/api/nonexistent", "/api/health/extra", "/no-such-page.txt"):
        resp = await real_client.get(path)
        assert resp.status_code == 404, path
        body = resp.json()
        assert set(body) == {"code", "message"}, path  # never {"detail": ...}
        assert body["code"] == "NOT_FOUND", path


async def test_wrong_method_returns_uniform_error_shape(real_client):
    # A method mismatch is a partial route match; the catch-all static mount
    # at "/" then handles the path, so this surfaces as 404 NOT_FOUND. Either
    # way the body must be the uniform {code, message} shape — never
    # {"detail": ...}. (The published contract documents no 405 responses.)
    resp = await real_client.get("/api/download")  # POST-only endpoint
    assert resp.status_code in (404, 405)
    body = resp.json()
    assert set(body) == {"code", "message"}
    assert body["code"] in ("NOT_FOUND", "METHOD_NOT_ALLOWED")


async def test_frontend_served_regardless_of_cwd(real_client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # simulate uvicorn launched from anywhere
    resp = await real_client.get("/")
    assert resp.status_code == 200
    assert "Facebook Video Downloader" in resp.text
