"""Thumbnail proxy (FR-006): the browser never contacts Facebook. Serves the
CDN thumbnail of a cached ExtractedVideo — content-type must be image/*, body
streamed with a hard 5 MB cap (Content-Length never trusted)."""

from __future__ import annotations

import httpx

from app.models.errors import AppError, ErrorCode
from app.services.info_cache import InfoCache

MAX_THUMB_BYTES = 5 * 1024 * 1024


async def fetch_thumbnail(
    video_id: str, cache: InfoCache, http: httpx.AsyncClient
) -> tuple[bytes, str]:
    """Returns (image_bytes, media_type). 404 VIDEO_UNAVAILABLE when the video
    id is not in the info cache (inspect first)."""
    info = cache.peek(video_id)
    if info is None or not getattr(info, "thumbnail_url", ""):
        raise AppError(
            ErrorCode.VIDEO_UNAVAILABLE,
            "No cached info for this video — inspect the URL first.",
        )

    try:
        async with http.stream("GET", info.thumbnail_url) as response:
            if response.status_code != 200:
                raise AppError(
                    ErrorCode.NETWORK_ERROR, "The thumbnail could not be fetched."
                )
            media_type = response.headers.get("content-type", "")
            if not media_type.startswith("image/"):
                raise AppError(
                    ErrorCode.NETWORK_ERROR, "The thumbnail was not an image."
                )
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > MAX_THUMB_BYTES:
                    raise AppError(
                        ErrorCode.NETWORK_ERROR, "The thumbnail is too large."
                    )
                chunks.append(chunk)
    except AppError:
        raise
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.NETWORK_ERROR, "The thumbnail could not be fetched."
        ) from exc

    return b"".join(chunks), media_type
