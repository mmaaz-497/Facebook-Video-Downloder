"""Wire models (Pydantic v2) — the published contract (contracts/api.yaml)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResolutionOption(BaseModel):
    height: int = Field(ge=1)
    label: str


class VideoInfoResponse(BaseModel):
    video_id: str
    title: str
    # Relative proxy URL (/api/thumb?video_id=...) — never a Facebook CDN URL.
    thumbnail_url: str
    duration_seconds: float = Field(ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    resolutions: list[ResolutionOption] = Field(min_length=1)


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    resolution: int = Field(ge=1)


class DownloadAccepted(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ErrorBody(BaseModel):
    code: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    state: Literal["queued", "running", "completed", "failed"]
    progress: int = Field(ge=0, le=100)
    queue_position: int | None = None
    error: ErrorBody | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    ffmpeg: bool
    extractor: bool
    running: int = Field(ge=0)
    queued: int = Field(ge=0)
    accepting_new: bool


class ErrorResponse(BaseModel):
    """The only failure shape on every endpoint."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
