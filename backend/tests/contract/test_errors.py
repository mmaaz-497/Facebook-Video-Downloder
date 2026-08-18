"""US2 T028: error taxonomy contract tests — every reachable ErrorCode on its
endpoints renders the documented HTTP status and the uniform {code, message}
body (contracts/api.yaml, spec Error Taxonomy)."""

from __future__ import annotations

import threading

import httpx

from app.models.errors import AppError, ErrorCode
from tests.conftest import poll_until

GOOD_URL = "https://www.facebook.com/watch?v=1234567890"


def assert_error(resp: httpx.Response, code: ErrorCode, status: int) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    # Uniform failure shape: exactly {code, message}, nothing else, everywhere.
    assert set(body) == {"code", "message"}, body
    assert body["code"] == code.value
    assert isinstance(body["message"], str) and body["message"]
    return body


async def test_invalid_url_on_info(client):
    resp = await client.get("/api/info", params={"url": "ftp://facebook.com/watch?v=1"})
    assert_error(resp, ErrorCode.INVALID_URL, 422)


async def test_invalid_url_on_download(client, app):
    resp = await client.post(
        "/api/download",
        json={"url": "https://user:pass@facebook.com/watch?v=1", "resolution": 720},
    )
    assert_error(resp, ErrorCode.INVALID_URL, 422)
    assert await app.state.jobs.global_active() == 0


async def test_unsupported_url_on_info(client):
    resp = await client.get(
        "/api/info", params={"url": "https://www.facebook.com/groups/123456"}
    )
    assert_error(resp, ErrorCode.UNSUPPORTED_URL, 422)


async def test_video_unavailable_on_info(client, fake_extractor):
    fake_extractor.info_error = AppError(
        ErrorCode.VIDEO_UNAVAILABLE, "This video is unavailable."
    )
    resp = await client.get("/api/info", params={"url": GOOD_URL})
    assert_error(resp, ErrorCode.VIDEO_UNAVAILABLE, 404)


async def test_auth_required_on_info(client, fake_extractor):
    fake_extractor.info_error = AppError(
        ErrorCode.AUTH_REQUIRED,
        "This video requires a Facebook login. Only public videos are "
        "supported, and this service will never log in to Facebook.",
    )
    resp = await client.get("/api/info", params={"url": GOOD_URL})
    body = assert_error(resp, ErrorCode.AUTH_REQUIRED, 403)
    message = body["message"].lower()
    assert "only public videos are supported" in message
    assert "never log in" in message


def test_auth_required_wrapper_message_states_policy():
    """The REAL wrapper's AUTH_REQUIRED mapping must state the verbatim
    policy: only public videos are supported and the app will never log in
    (spec US2 scenario 1). Tested at the mapping function so the message is
    ours, not the fake's."""
    from app.services.extractor import _map_exception

    err = _map_exception(Exception("You must log in to continue."))
    assert err.code is ErrorCode.AUTH_REQUIRED
    message = err.message.lower()
    assert "only public videos are supported" in message
    assert "never log in" in message


async def test_extraction_failed_on_info(client, fake_extractor):
    fake_extractor.info_error = AppError(
        ErrorCode.EXTRACTION_FAILED, "No downloadable stream."
    )
    resp = await client.get("/api/info", params={"url": GOOD_URL})
    assert_error(resp, ErrorCode.EXTRACTION_FAILED, 502)


async def test_rate_limited_includes_retry_after_header(client, fake_extractor):
    fake_extractor.info_error = AppError(
        ErrorCode.RATE_LIMITED, "Facebook is throttling requests.", retry_after=60
    )
    resp = await client.get("/api/info", params={"url": GOOD_URL})
    assert_error(resp, ErrorCode.RATE_LIMITED, 503)
    assert resp.headers.get("retry-after") == "60"


async def test_resolution_unavailable_creates_no_job(client, app):
    # Fake offers {720, 360}; 1080 must be rejected with no trace of a job.
    resp = await client.post(
        "/api/download", json={"url": GOOD_URL, "resolution": 1080}
    )
    assert_error(resp, ErrorCode.RESOLUTION_UNAVAILABLE, 422)
    assert await app.state.jobs.global_active() == 0
    running, queued = await app.state.jobs.counts()
    assert (running, queued) == (0, 0)


async def test_network_error_on_info(client, fake_extractor):
    fake_extractor.info_error = AppError(
        ErrorCode.NETWORK_ERROR, "A network error occurred talking to Facebook."
    )
    resp = await client.get("/api/info", params={"url": GOOD_URL})
    assert_error(resp, ErrorCode.NETWORK_ERROR, 502)


async def test_job_not_found_on_status_and_file(client):
    for path in ("/api/status/" + "0" * 32, "/api/file/" + "0" * 32):
        resp = await client.get(path)
        assert_error(resp, ErrorCode.JOB_NOT_FOUND, 404)


async def test_job_not_ready_on_file(client, fake_extractor):
    gate = threading.Event()
    fake_extractor.download_gate = gate
    resp = await client.post("/api/download", json={"url": GOOD_URL, "resolution": 720})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    try:
        resp = await client.get(f"/api/file/{job_id}")
        assert_error(resp, ErrorCode.JOB_NOT_READY, 409)
    finally:
        gate.set()
    await poll_until(client, job_id, {"completed", "failed"})


async def test_internal_error_hides_traceback(app, fake_extractor):
    fake_extractor.info_error = RuntimeError("boom: secret internal detail")
    transport = httpx.ASGITransport(
        app=app, client=("203.0.113.7", 12345), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.get("/api/info", params={"url": GOOD_URL})
    await app.state.http.aclose()
    assert_error(resp, ErrorCode.INTERNAL_ERROR, 500)
    # The body never echoes internals: no traceback, no exception text.
    assert "Traceback" not in resp.text
    assert "boom" not in resp.text
    assert "secret internal detail" not in resp.text


async def test_failed_job_status_carries_typed_error_body(client, fake_extractor):
    # Permanent code: fails immediately (no retry/backoff), keeping the test fast.
    fake_extractor.download_error = AppError(
        ErrorCode.VIDEO_UNAVAILABLE, "This video is unavailable."
    )
    resp = await client.post("/api/download", json={"url": GOOD_URL, "resolution": 720})
    assert resp.status_code == 202
    status = await poll_until(client, resp.json()["job_id"], {"failed"})
    assert set(status["error"]) == {"code", "message"}
    assert status["error"]["code"] == ErrorCode.VIDEO_UNAVAILABLE.value
