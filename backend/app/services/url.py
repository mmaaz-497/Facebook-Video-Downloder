"""URL classification + short-link redirect resolution (NFR-001, SSRF).

`classify_url` is pure: scheme allowlist, credentials/IP-literal rejection,
exact-suffix host allowlist, single-video shape recognition. `resolve_ref`
follows short-link redirects manually (≤5 hops), re-validating the host every
hop and rejecting hops whose hosts resolve to private/loopback/link-local/
reserved ranges before any connection is made.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from app.models.errors import AppError, ErrorCode

ALLOWED_HOSTS = frozenset(
    {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "web.facebook.com",
        "mbasic.facebook.com",
        "fb.watch",
    }
)

# First path segments that are never a single public video
_BLOCKED_SEGMENTS = frozenset(
    {
        "groups",
        "marketplace",
        "stories",
        "story.php",
        "live",
        "playlist",
        "events",
        "gaming",
        "profile.php",
    }
)

MAX_REDIRECT_HOPS = 5

Resolver = Callable[[str], Awaitable[list[str]]]


@dataclass(frozen=True)
class UrlRef:
    kind: Literal["video", "short"]
    url: str
    video_id: str | None = None


def _invalid(message: str) -> AppError:
    return AppError(ErrorCode.INVALID_URL, message)


def _unsupported(message: str) -> AppError:
    return AppError(
        ErrorCode.UNSUPPORTED_URL,
        message + " Only single public Facebook video URLs are supported.",
    )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def classify_url(url: str) -> UrlRef:
    """Validate and classify a URL. Raises AppError(INVALID_URL) for malformed/
    unsafe URLs and AppError(UNSUPPORTED_URL) for non-single-video shapes.
    Runs before any network work."""
    if not url or not url.strip():
        raise _invalid("Empty URL.")
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise _invalid("Malformed URL.") from None

    if parts.scheme not in ("http", "https"):
        raise _invalid("Only http(s) URLs are accepted.")
    if parts.username is not None or parts.password is not None:
        raise _invalid("URLs with embedded credentials are not accepted.")

    host = (parts.hostname or "").lower()
    if not host:
        raise _invalid("URL has no host.")
    if _is_ip_literal(host):
        raise _invalid("IP-literal hosts are not accepted.")
    # Exact-suffix allowlist: exact host match only (facebook.com.evil.tld and
    # evilfacebook.com never match).
    if host not in ALLOWED_HOSTS:
        raise _unsupported("Host is not Facebook or fb.watch.")

    segments = [s for s in parts.path.split("/") if s]
    query = parse_qs(parts.query)

    if "playlist" in query:
        raise _unsupported("Playlists cannot be downloaded.")

    if host == "fb.watch":
        if len(segments) == 1:
            return UrlRef(kind="short", url=url)
        raise _unsupported("Unrecognized fb.watch link.")

    if segments and segments[0].lower() in _BLOCKED_SEGMENTS:
        raise _unsupported("This Facebook URL is not a single video.")

    # facebook.com/watch?v=<id> and /watch/?v=<id>
    if segments == ["watch"] or not segments and parts.path.startswith("/watch"):
        vid = (query.get("v") or [None])[0]
        if vid:
            return UrlRef(kind="video", url=url, video_id=vid)
        raise _unsupported("The Watch feed is not a single video.")

    # facebook.com/reel/<id>
    if len(segments) >= 2 and segments[0].lower() == "reel" and segments[1]:
        return UrlRef(kind="video", url=url, video_id=segments[1])

    # facebook.com/share/v/<token>/ and /share/r/<token>/
    if (
        len(segments) >= 3
        and segments[0].lower() == "share"
        and segments[1].lower() in ("v", "r")
        and segments[2]
    ):
        return UrlRef(kind="short", url=url)

    # facebook.com/<page-or-user>/videos/<id>/
    if len(segments) >= 3 and segments[1].lower() == "videos" and segments[2]:
        return UrlRef(kind="video", url=url, video_id=segments[2])

    raise _unsupported("Unrecognized Facebook URL shape.")


def _forbidden_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


async def _check_resolved_ips(host: str, resolver: Resolver) -> None:
    try:
        ips = await resolver(host)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.NETWORK_ERROR, "Could not resolve the link's host."
        ) from exc
    if not ips or any(_forbidden_ip(ip) for ip in ips):
        raise _invalid("The link resolves to a forbidden network range.")


async def resolve_ref(
    url: str,
    http: httpx.AsyncClient,
    resolver: Resolver | None = None,
) -> UrlRef:
    """Resolve a URL (typically a short link) to a direct single-video ref.
    Manual redirect loop, ≤5 hops; every hop re-runs classify_url and the
    resolved-IP check before it is contacted (SC-008)."""
    resolver = resolver or _default_resolver
    ref = classify_url(url)
    if ref.kind == "video":
        return ref

    current = ref.url
    for _hop in range(MAX_REDIRECT_HOPS):
        host = urlsplit(current).hostname or ""
        await _check_resolved_ips(host, resolver)
        try:
            response = await http.get(current, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.NETWORK_ERROR, "Could not resolve the share link."
            ) from exc

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise AppError(
                    ErrorCode.NETWORK_ERROR, "Redirect without a target."
                )
            current = urljoin(current, location)
            ref = classify_url(current)  # re-validate EVERY hop
            if ref.kind == "video":
                # The final hop's host must also resolve to a safe range
                await _check_resolved_ips(urlsplit(current).hostname or "", resolver)
                return ref
            continue

        raise AppError(
            ErrorCode.VIDEO_UNAVAILABLE,
            "The share link did not lead to a video.",
        )

    raise _unsupported("Too many redirects while resolving the link.")
