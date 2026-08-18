"""Filename sanitization (FR-004): titles only ever appear in the
Content-Disposition header — stripped of path separators/control chars, with
RFC 5987 filename* for non-ASCII. On-disk names are always <job_id>.mp4
(asserted end-to-end in tests/contract/test_us1_flow.py)."""

from __future__ import annotations

import pytest

from app.api.routes import content_disposition, sanitize_filename

FORBIDDEN = set('/\\:*?"<>|')


@pytest.mark.parametrize(
    "title",
    [
        'evil/../../etc\\passwd:file*name?"x"<y>|z',
        "C:\\Windows\\system32",
        "a/b/c",
    ],
)
def test_path_separators_and_reserved_chars_removed(title: str):
    safe = sanitize_filename(title)
    assert safe
    assert not (set(safe) & FORBIDDEN)
    assert ".." not in safe


def test_control_chars_stripped():
    safe = sanitize_filename("bad\x00\x01\x1f\r\n\ttitle")
    assert safe == "badtitle" or "bad" in safe and "title" in safe
    assert not any(ord(c) < 32 for c in safe)


def test_empty_or_fully_stripped_title_falls_back():
    assert sanitize_filename("") == "video"
    assert sanitize_filename('///:::***') == "video"


def test_ascii_title_plain_disposition():
    header = content_disposition("My Holiday Clip")
    assert header.startswith("attachment;")
    assert 'filename="My Holiday Clip.mp4"' in header


def test_non_ascii_title_gets_rfc5987_star_form():
    header = content_disposition("Café vidéo ☕")
    assert "filename*=UTF-8''" in header
    assert "%C3%A9" in header          # é percent-encoded
    assert 'filename="' in header      # ASCII fallback also present
    # Header value itself must be pure ASCII
    header.encode("ascii")


def test_disposition_always_mp4():
    for title in ("clip", "clip.mp4", "clip.exe", "日本語"):
        header = content_disposition(title)
        assert ".mp4" in header
