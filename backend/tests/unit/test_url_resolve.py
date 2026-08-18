"""Short-link redirect resolution (NFR-001): mocked httpx transport + injected
DNS resolver — ≤5 hops, per-hop host re-validation, private/loopback/link-local/
reserved resolved IPs rejected mid-chain. Zero live network."""

from __future__ import annotations

import httpx
import pytest

from app.models.errors import AppError, ErrorCode
from app.services.url import resolve_ref

PUBLIC_IP = "157.240.1.35"


def make_client(redirects: dict[str, str]) -> tuple[httpx.AsyncClient, list[str]]:
    """MockTransport client: any URL found in `redirects` 302s to its target."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url in redirects:
            return httpx.Response(302, headers={"location": redirects[url]})
        return httpx.Response(200)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requested


async def resolve_public(host: str) -> list[str]:
    return [PUBLIC_IP]


async def test_short_link_resolves_to_video_ref():
    client, _ = make_client(
        {"https://fb.watch/abc12/": "https://www.facebook.com/watch?v=777"}
    )
    ref = await resolve_ref("https://fb.watch/abc12/", client, resolver=resolve_public)
    assert ref.kind == "video"
    assert ref.video_id == "777"


async def test_multi_hop_within_cap_resolves():
    client, _ = make_client(
        {
            "https://fb.watch/a/": "https://www.facebook.com/share/v/tok1/",
            "https://www.facebook.com/share/v/tok1/": "https://www.facebook.com/reel/999",
        }
    )
    ref = await resolve_ref("https://fb.watch/a/", client, resolver=resolve_public)
    assert ref.video_id == "999"


async def test_more_than_five_hops_rejected():
    chain = {
        f"https://fb.watch/hop{i}/": f"https://fb.watch/hop{i + 1}/" for i in range(7)
    }
    client, _ = make_client(chain)
    with pytest.raises(AppError) as exc:
        await resolve_ref("https://fb.watch/hop0/", client, resolver=resolve_public)
    assert exc.value.code is ErrorCode.UNSUPPORTED_URL


async def test_host_revalidated_every_hop():
    client, requested = make_client(
        {"https://fb.watch/evil1/": "https://evil.example.com/watch?v=1"}
    )
    with pytest.raises(AppError) as exc:
        await resolve_ref("https://fb.watch/evil1/", client, resolver=resolve_public)
    assert exc.value.code is ErrorCode.UNSUPPORTED_URL
    # The disallowed host must never be contacted.
    assert not any("evil.example.com" in u for u in requested)


@pytest.mark.parametrize(
    "bad_ip",
    [
        "127.0.0.1",       # loopback
        "10.1.2.3",        # private
        "192.168.1.10",    # private
        "172.16.5.5",      # private
        "169.254.9.9",     # link-local
        "240.0.0.1",       # reserved
        "::1",             # v6 loopback
        "fe80::1",         # v6 link-local
    ],
)
async def test_privately_resolving_hop_rejected_mid_chain(bad_ip: str):
    """First hop is fine; the redirect target's host resolves to a forbidden
    range and must be rejected before it is ever contacted."""
    client, requested = make_client(
        {"https://fb.watch/abc/": "https://m.facebook.com/watch?v=5"}
    )

    async def resolver(host: str) -> list[str]:
        return [PUBLIC_IP] if host == "fb.watch" else [bad_ip]

    with pytest.raises(AppError) as exc:
        await resolve_ref("https://fb.watch/abc/", client, resolver=resolver)
    assert exc.value.code is ErrorCode.INVALID_URL
    assert not any("m.facebook.com" in u for u in requested)


async def test_direct_video_url_needs_no_hops():
    client, requested = make_client({})
    ref = await resolve_ref(
        "https://www.facebook.com/watch?v=123", client, resolver=resolve_public
    )
    assert ref.video_id == "123"
    assert requested == []
