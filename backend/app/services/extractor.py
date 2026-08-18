"""THE ONLY MODULE THAT IMPORTS yt_dlp (constitutional Technology Constraint,
enforced by tests/unit/test_import_boundary.py).

Every function on YtDlpExtractor is BLOCKING and must run on the bounded
executors in services/pool.py — never on the event loop. No option that
authenticates to Facebook (cookiefile, cookiesfrombrowser, headers) may ever
appear here (Constitution I).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp

from app.models.errors import AppError, ErrorCode

ProgressCb = Callable[[str, int, int | None], None]


@dataclass(frozen=True)
class FormatOption:
    height: int
    label: str
    format_id: str  # engine-internal selector, never exposed on the wire


@dataclass(frozen=True)
class ExtractedVideo:
    video_id: str
    title: str
    thumbnail_url: str  # CDN URL: proxy-only, never sent to the browser or logged
    duration_seconds: float
    width: int
    height: int
    formats: tuple[FormatOption, ...]


class _ByteCapExceeded(Exception):
    pass


def _label(height: int) -> str:
    return f"{height}p (HD)" if height >= 720 else f"{height}p"


def _map_exception(exc: Exception, during_download: bool = False) -> AppError:
    """yt-dlp exception → typed AppError. Message text is ours, never the
    extractor's internals."""
    text = str(exc).lower()
    if during_download and ("403" in text or "forbidden" in text):
        # During a download a 403 means the signed CDN URL expired — transient,
        # flagged so the downloader applies FR-010's one-shot re-extract+retry.
        # (During extraction a 403 is handled by the marker checks below.)
        return AppError(
            ErrorCode.NETWORK_ERROR,
            "The video's download link expired.",
            signed_url_expired=True,
        )
    if any(
        marker in text
        for marker in ("login", "log in", "logged in", "cookies", "private", "account")
    ):
        return AppError(
            ErrorCode.AUTH_REQUIRED,
            "This video requires a Facebook login. Only public videos are "
            "supported, and this service will never log in to Facebook.",
        )
    if any(
        marker in text
        for marker in ("unavailable", "not exist", "deleted", "removed", "not found", "404")
    ):
        return AppError(
            ErrorCode.VIDEO_UNAVAILABLE,
            "This video is unavailable — it may be deleted or region-blocked.",
        )
    if "429" in text or "rate" in text or "too many requests" in text:
        return AppError(
            ErrorCode.RATE_LIMITED,
            "Facebook is throttling requests. Try again shortly.",
            retry_after=60,
        )
    if any(
        marker in text
        for marker in ("timed out", "timeout", "connection", "network", "unable to download webpage")
    ):
        return AppError(
            ErrorCode.NETWORK_ERROR, "A network error occurred talking to Facebook."
        )
    return AppError(
        ErrorCode.EXTRACTION_FAILED,
        "The extractor could not produce a downloadable stream for this video.",
    )


class YtDlpExtractor:
    """Blocking wrapper around yt-dlp. Executor-only."""

    _BASE_OPTS = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }

    def extract_info(self, url: str) -> ExtractedVideo:
        """BLOCKING — run on the extraction executor only."""
        opts = dict(self._BASE_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # yt-dlp raises many types; all map to taxonomy
            raise _map_exception(exc) from None

        if not info:
            raise AppError(
                ErrorCode.EXTRACTION_FAILED, "The extractor returned no data."
            )

        heights: set[int] = set()
        for fmt in info.get("formats") or []:
            height = fmt.get("height")
            if height and fmt.get("vcodec") not in (None, "none"):
                heights.add(int(height))
        if not heights:
            raise AppError(
                ErrorCode.EXTRACTION_FAILED,
                "The extractor reported no downloadable video streams.",
            )

        formats = tuple(
            FormatOption(
                height=h,
                label=_label(h),
                format_id=f"bv*[height={h}]+ba/b[height={h}]/bv*[height={h}]",
            )
            for h in sorted(heights, reverse=True)
        )
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        if width <= 0 or height <= 0:
            best = max(
                (f for f in info.get("formats") or [] if f.get("height")),
                key=lambda f: f.get("height") or 0,
                default={},
            )
            width = int(best.get("width") or 16)
            height = int(best.get("height") or 9)

        return ExtractedVideo(
            video_id=str(info.get("id") or ""),
            title=str(info.get("title") or "Facebook video"),
            thumbnail_url=str(info.get("thumbnail") or ""),
            duration_seconds=float(info.get("duration") or 0.0),
            width=width,
            height=height,
            formats=formats,
        )

    def download(
        self,
        url: str,
        format_id: str,
        output_stem: Path,
        progress_cb: ProgressCb,
        byte_cap: int,
    ) -> Path:
        """BLOCKING — run on the download executor only.

        Downloads to `output_stem`.%(ext)s (yt-dlp controls the extension and
        its own intermediates) and returns the ACTUAL final output path as
        reported by yt-dlp — never a guessed path. Reports (phase,
        downloaded_bytes, total_bytes) through progress_cb, where phase is
        "video" for the first stream, "audio" for the second, and "mux" during
        postprocessing. Enforces byte_cap from cumulative progress-hook bytes
        (Content-Length is never trusted) by aborting.
        """
        seen_files: list[str] = []
        pp_outputs: list[str] = []
        finished_bytes = 0
        state_lock = threading.Lock()

        def hook(d: dict) -> None:
            nonlocal finished_bytes
            status = d.get("status")
            filename = d.get("filename") or ""
            with state_lock:
                if filename and filename not in seen_files:
                    seen_files.append(filename)
                phase = "video" if len(seen_files) <= 1 else "audio"
            downloaded = int(d.get("downloaded_bytes") or 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if finished_bytes + downloaded > byte_cap:
                raise _ByteCapExceeded()
            if status == "downloading":
                progress_cb(phase, downloaded, int(total) if total else None)
            elif status == "finished":
                with state_lock:
                    finished_bytes += downloaded
                progress_cb(phase, downloaded, downloaded or None)

        def pp_hook(d: dict) -> None:
            if d.get("status") == "started":
                progress_cb("mux", 0, 1)
            elif d.get("status") == "finished":
                # The merge/move postprocessors report the file they produced.
                filepath = (d.get("info_dict") or {}).get("filepath")
                if filepath:
                    with state_lock:
                        pp_outputs.append(str(filepath))
                progress_cb("mux", 1, 1)

        opts = dict(self._BASE_OPTS)
        opts.update(
            {
                "format": format_id,
                "outtmpl": {"default": f"{output_stem}.%(ext)s"},
                "merge_output_format": "mp4",
                "progress_hooks": [hook],
                "postprocessor_hooks": [pp_hook],
                "max_filesize": byte_cap,
                "concurrent_fragment_downloads": 4,
                "retries": 3,
                "fragment_retries": 3,
            }
        )
        info = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except _ByteCapExceeded:
            # No dedicated taxonomy code exists for a MAX_DOWNLOAD_BYTES breach
            # (the 15-code taxonomy is contractual): a mid-stream byte-cap
            # abort maps to EXTRACTION_FAILED with a clear message.
            raise AppError(
                ErrorCode.EXTRACTION_FAILED,
                "The download exceeded the maximum allowed size and was aborted.",
            ) from None
        except Exception as exc:
            if isinstance(exc.__cause__, _ByteCapExceeded):
                # Same contractual mapping as above: byte-cap breach (surfaced
                # wrapped by yt-dlp) → EXTRACTION_FAILED, never a 16th code.
                raise AppError(
                    ErrorCode.EXTRACTION_FAILED,
                    "The download exceeded the maximum allowed size and was aborted.",
                ) from None
            raise _map_exception(exc, during_download=True) from None

        # Resolve the real final path, most authoritative source first:
        # requested_downloads.filepath (post-merge/move) → postprocessor
        # outputs → per-stream hook filenames (single-stream case).
        candidates: list[str] = []
        for rd in (info or {}).get("requested_downloads") or []:
            filepath = rd.get("filepath")
            if filepath:
                candidates.append(str(filepath))
        candidates.extend(reversed(pp_outputs))
        candidates.extend(reversed(seen_files))
        for candidate in candidates:
            path = Path(candidate)
            if path.exists():
                return path
        raise AppError(
            ErrorCode.EXTRACTION_FAILED,
            "The download finished but produced no output file.",
        )

    def self_check(self) -> bool:
        """Wrapper self-check: the Facebook extractor is importable and
        operational. Never a live probe of Facebook."""
        try:
            from yt_dlp.extractor.facebook import FacebookIE

            return FacebookIE is not None
        except Exception:
            return False
