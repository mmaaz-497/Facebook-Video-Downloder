"""App factory + lifespan. Builds Settings/Pools/stores into app.state at
factory time (no import-time side effects); lifespan starts/stops the periodic
cleanup sweep and handles teardown."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, get_settings
from app.models.errors import AppError, ErrorCode
from app.services.info_cache import InfoCache
from app.services.jobs import JobStore, client_hash
from app.services.pool import Pools

logger = logging.getLogger("app")

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


class HashedIpFilter(logging.Filter):
    """Replaces raw IPv4 addresses in log output with their salted hash
    (NFR-004: salted-hash client IPs in logs, never raw)."""

    def __init__(self, salt: str) -> None:
        super().__init__()
        self._salt = salt

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = _IPV4_RE.sub(
            lambda m: "ip:" + client_hash(m.group(0), self._salt)[:12], msg
        )
        if redacted != msg:
            record.msg = redacted
            record.args = None
        return True


def _add_ip_filter(target: logging.Logger | logging.Handler, salt: str) -> None:
    if not any(isinstance(f, HashedIpFilter) for f in target.filters):
        target.addFilter(HashedIpFilter(salt))


def setup_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level)
    root = logging.getLogger()
    # Logger-level filters run only on the logger a record is emitted on, so a
    # filter on root never sees records propagated from app.* child loggers —
    # handlers see every propagated record, so the redaction must live there.
    # uvicorn's access logger (raw client addresses) does not propagate to
    # root at all and needs the filter directly.
    _add_ip_filter(root, settings.ip_hash_salt)
    for handler in root.handlers:
        _add_ip_filter(handler, settings.ip_hash_salt)
    _add_ip_filter(logging.getLogger("uvicorn.access"), settings.ip_hash_salt)
    # httpx/httpcore log full request URLs ("HTTP Request: GET <url>") at
    # INFO/DEBUG — for the thumbnail proxy that is a signed CDN URL, which must
    # never be logged at ANY level (Constitution VI). Capped at WARNING
    # unconditionally, independent of LOG_LEVEL.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from app.services import cleanup

    cleanup_task = asyncio.create_task(
        cleanup.run_periodic(app.state.jobs, app.state.settings),
        name="cleanup-sweep",
    )
    yield
    cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup_task
    await app.state.http.aclose()
    app.state.pools.shutdown()


def create_app(
    settings: Settings | None = None,
    extractor: object | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings)

    app = FastAPI(title="fb-video-downloader", lifespan=_lifespan)
    app.state.settings = settings
    app.state.pools = Pools(settings)
    app.state.http = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(10.0), follow_redirects=False
    )
    app.state.extractor = extractor  # None → real wrapper, resolved lazily
    app.state.jobs = JobStore()
    app.state.info_cache = InfoCache(
        ttl_seconds=settings.info_cache_ttl_seconds,
        max_entries=settings.info_cache_max_entries,
    )
    app.state.tasks = set()
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        headers = {}
        if exc.code is ErrorCode.RATE_LIMITED and exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            status_code=exc.status,
            content={"code": exc.code.value, "message": exc.message},
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def framework_http_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Paths outside the published contract (unmatched routes, wrong
        # methods, missing static files) must still render the uniform
        # {code, message} shape (Constitution V) — never {"detail": ...}.
        # These codes sit outside the 15-code endpoint taxonomy because they
        # describe requests to non-endpoints.
        code, message = {
            404: ("NOT_FOUND", "The requested path does not exist."),
            405: ("METHOD_NOT_ALLOWED", "This method is not allowed here."),
        }.get(
            exc.status_code,
            (ErrorCode.INTERNAL_ERROR.value, "The request could not be handled."),
        )
        return JSONResponse(
            status_code=exc.status_code, content={"code": code, "message": message}
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Traceback stays server-side only; the body never echoes internals.
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": "An internal error occurred.",
            },
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    from app.api.routes import router

    app.include_router(router)

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


app = create_app
