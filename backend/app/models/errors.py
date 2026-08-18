"""Error taxonomy (Constitution III/V): 15 stable codes, stable HTTP statuses,
uniform {code, message} body rendered by the global exception handler."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_URL = "UNSUPPORTED_URL"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    RESOLUTION_UNAVAILABLE = "RESOLUTION_UNAVAILABLE"
    NETWORK_ERROR = "NETWORK_ERROR"
    FFMPEG_MISSING = "FFMPEG_MISSING"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_READY = "JOB_NOT_READY"
    TOO_MANY_JOBS = "TOO_MANY_JOBS"
    SERVER_BUSY = "SERVER_BUSY"
    INSUFFICIENT_STORAGE = "INSUFFICIENT_STORAGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.INVALID_URL: 422,
    ErrorCode.UNSUPPORTED_URL: 422,
    ErrorCode.VIDEO_UNAVAILABLE: 404,
    ErrorCode.AUTH_REQUIRED: 403,
    ErrorCode.EXTRACTION_FAILED: 502,
    ErrorCode.RATE_LIMITED: 503,
    ErrorCode.RESOLUTION_UNAVAILABLE: 422,
    ErrorCode.NETWORK_ERROR: 502,
    ErrorCode.FFMPEG_MISSING: 500,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.JOB_NOT_READY: 409,
    ErrorCode.TOO_MANY_JOBS: 429,
    ErrorCode.SERVER_BUSY: 503,
    ErrorCode.INSUFFICIENT_STORAGE: 507,
    ErrorCode.INTERNAL_ERROR: 500,
}

TRANSIENT_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.NETWORK_ERROR, ErrorCode.RATE_LIMITED}
)


class AppError(Exception):
    """The only failure type raised across the app; everything else is a bug
    and is translated to INTERNAL_ERROR by the catch-all handler.

    `signed_url_expired` is an internal retry-policy hint (FR-010: an HTTP 403
    on a signed CDN URL earns exactly one re-extract), never serialized to the
    wire — the taxonomy stays at 15 codes.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        retry_after: int | None = None,
        signed_url_expired: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.signed_url_expired = signed_url_expired

    @property
    def status(self) -> int:
        return STATUS_MAP[self.code]
