"""URL classification (NFR-001): every accepted single-video form, every
rejected shape, and the SSRF cases — all pure, zero network."""

from __future__ import annotations

import pytest

from app.models.errors import AppError, ErrorCode
from app.services.url import classify_url

ACCEPTED_DIRECT = [
    ("https://www.facebook.com/watch?v=123456", "123456"),
    ("https://www.facebook.com/watch/?v=123456", "123456"),
    ("https://facebook.com/watch?v=123456", "123456"),
    ("https://m.facebook.com/watch?v=123456", "123456"),
    ("https://web.facebook.com/watch?v=123456", "123456"),
    ("https://mbasic.facebook.com/watch?v=123456", "123456"),
    ("http://www.facebook.com/watch?v=42", "42"),
    ("https://www.facebook.com/SomePage/videos/987654/", "987654"),
    ("https://www.facebook.com/some.user/videos/987654", "987654"),
    ("https://www.facebook.com/reel/555", "555"),
    ("https://www.facebook.com/reel/555/?fs=e&s=cl", "555"),
    ("https://m.facebook.com/reel/555", "555"),
]

ACCEPTED_SHORT = [
    "https://www.facebook.com/share/v/AbCd123/",
    "https://www.facebook.com/share/r/XyZ987/",
    "https://facebook.com/share/v/AbCd123",
    "https://fb.watch/aBcDe1/",
    "https://fb.watch/aBcDe1",
]

REJECTED_UNSUPPORTED = [
    "https://www.facebook.com/groups/123456/",
    "https://www.facebook.com/groups/123/posts/456/",
    "https://www.facebook.com/marketplace/item/123/",
    "https://www.facebook.com/stories/123456/",
    "https://www.facebook.com/SomePage/live/",
    "https://www.facebook.com/live/producer/123",
    "https://www.facebook.com/watch/?playlist=123",
    "https://www.facebook.com/playlist/123",
    "https://www.facebook.com/zuck",
    "https://www.facebook.com/zuck/",
    "https://www.facebook.com/watch",          # Watch feed, no video id
    "https://www.facebook.com/",
    "https://fb.watch/",
    "https://vimeo.com/123456",
    "https://youtube.com/watch?v=123",
    "https://facebook.com.evil.tld/watch?v=123",   # exact-suffix, not substring
    "https://notfacebook.com/watch?v=123",
    "https://evilfacebook.com/watch?v=123",
]

REJECTED_INVALID = [
    "not a url",
    "ftp://www.facebook.com/watch?v=123",
    "javascript:alert(1)",
    "https://user:pass@facebook.com/watch?v=123",   # credentials in URL
    "https://157.240.1.35/watch?v=123",             # IPv4 literal
    "https://[2a03:2880:f101::1]/watch?v=123",      # IPv6 literal
    "",
]


@pytest.mark.parametrize("url,video_id", ACCEPTED_DIRECT)
def test_accepts_direct_video_forms(url: str, video_id: str):
    ref = classify_url(url)
    assert ref.kind == "video"
    assert ref.video_id == video_id


@pytest.mark.parametrize("url", ACCEPTED_SHORT)
def test_accepts_short_forms_as_redirect_refs(url: str):
    ref = classify_url(url)
    assert ref.kind == "short"


@pytest.mark.parametrize("url", REJECTED_UNSUPPORTED)
def test_rejects_unsupported_shapes(url: str):
    with pytest.raises(AppError) as exc:
        classify_url(url)
    assert exc.value.code is ErrorCode.UNSUPPORTED_URL


@pytest.mark.parametrize("url", REJECTED_INVALID)
def test_rejects_invalid_urls(url: str):
    with pytest.raises(AppError) as exc:
        classify_url(url)
    assert exc.value.code is ErrorCode.INVALID_URL
