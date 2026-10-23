"""Tests for the per-indexer async HTTP client.

All network I/O is intercepted with ``respx``; nothing here touches the network.
Fixtures live in this file because ``tests/conftest.py`` is owned by a later task.
"""

from __future__ import annotations

import httpx
import pytest

from stateless_hydra.config import IndexerConfig
from stateless_hydra.indexer_client import (
    IndexerClient,
    IndexerError,
    origin_of,
    resolve_proxy_url,
)

UA = "test-agent/1.0"
API_KEY = "s3cret"
BASE = "https://api.example.invalid"
ORIGIN = origin_of(BASE)


def _indexer(**overrides) -> IndexerConfig:
    base = {
        "name": "ix",
        "host": BASE,
        "api_key_ref": "ix",
    }
    base.update(overrides)
    return IndexerConfig(**base)


@pytest.fixture
async def make_client():
    """Factory that builds :class:`IndexerClient`s and closes them afterwards."""
    clients: list[IndexerClient] = []

    def _make(
        indexer: IndexerConfig | None = None,
        **kwargs,
    ) -> IndexerClient:
        client = IndexerClient(indexer or _indexer(), API_KEY, UA, **kwargs)
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.close()


# --- resolve_proxy_url -------------------------------------------------------


def test_resolve_proxy_per_indexer_override_wins():
    indexer = _indexer(proxy_url="http://per-indexer:8080")

    assert resolve_proxy_url(indexer, "http://global:3128") == "http://per-indexer:8080"


def test_resolve_proxy_falls_back_to_global():
    indexer = _indexer()

    assert resolve_proxy_url(indexer, "http://global:3128") == "http://global:3128"


def test_resolve_proxy_none_when_both_unset():
    assert resolve_proxy_url(_indexer(), None) is None


# --- build_url ---------------------------------------------------------------


async def test_build_url_includes_host_path_and_encoded_params(make_client):
    client = make_client()

    url = client.build_url({"t": "search", "q": "hello world & more"})

    assert url.startswith(f"{BASE}/api?")
    assert "t=search" in url
    assert "q=hello+world+%26+more" in url
    assert url.endswith(f"apikey={API_KEY}")


async def test_build_url_always_appends_apikey_replacing_caller_value(make_client):
    client = make_client()

    url = client.build_url({"t": "search", "apikey": "caller-key"})

    assert url.count("apikey=") == 1
    assert "caller-key" not in url
    assert f"apikey={API_KEY}" in url


# --- construction ------------------------------------------------------------


async def test_client_constructed_with_timeout_and_proxy(make_client):
    indexer = _indexer(proxy_url="http://per-indexer:8080", timeout_seconds=12.5)

    client = make_client(indexer, global_proxy_url="http://global:3128")

    assert client.name == "ix"
    assert client.client.timeout == httpx.Timeout(12.5)
    # httpx records a transport mount for a configured proxy.
    assert client.client._mounts


async def test_client_without_proxy_has_no_proxy_mount(make_client):
    client = make_client(global_proxy_url=None)

    assert not client.client._mounts


def test_default_body_cap_is_16_mib(make_client):
    # The config surface is MiB (``max_upstream_body_mib``), but this byte-level
    # boundary still defaults to the equivalent 16 MiB.
    client = make_client()

    assert client._max_body_bytes == 16 * 1024 * 1024


# --- search ------------------------------------------------------------------


async def test_search_returns_text_and_sends_configured_user_agent(make_client, respx_mock):
    route = respx_mock.get(f"{BASE}/api").mock(
        return_value=httpx.Response(200, text="<rss>ok</rss>")
    )
    client = make_client()

    text = await client.search({"t": "search"})

    assert text == "<rss>ok</rss>"
    assert route.called
    request = route.calls.last.request
    assert request.headers["user-agent"] == UA
    assert request.url.path == "/api"


async def test_search_raises_indexer_error_on_http_status(make_client, respx_mock):
    respx_mock.get(f"{BASE}/api").mock(return_value=httpx.Response(401, text="nope"))
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.search({"t": "search"})

    assert excinfo.value.status_code == 401
    assert excinfo.value.indexer_name == "ix"
    assert "HTTP 401" in str(excinfo.value)


async def test_search_raises_indexer_error_on_transport_error(make_client, respx_mock):
    respx_mock.get(f"{BASE}/api").mock(side_effect=httpx.ConnectError("boom"))
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.search({"t": "search"})

    assert excinfo.value.status_code is None
    assert excinfo.value.indexer_name == "ix"


@pytest.mark.parametrize(
    "timeout_exc",
    [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")],
)
async def test_search_raises_indexer_error_on_timeout(make_client, respx_mock, timeout_exc):
    respx_mock.get(f"{BASE}/api").mock(side_effect=timeout_exc)
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.search({"t": "search"})

    assert excinfo.value.status_code is None
    assert "timed out" in str(excinfo.value)


# --- search redirects (audit F1) ---------------------------------------------


async def test_search_follows_same_origin_redirect(make_client, respx_mock):
    first = respx_mock.get(f"{BASE}/api").mock(
        return_value=httpx.Response(302, headers={"location": "/api2"})
    )
    second = respx_mock.get(f"{BASE}/api2").mock(
        return_value=httpx.Response(200, text="<rss>ok</rss>")
    )
    client = make_client()

    text = await client.search({"t": "search"})

    assert text == "<rss>ok</rss>"
    assert first.call_count == 1
    assert second.call_count == 1


async def test_search_blocks_cross_origin_redirect(make_client, respx_mock):
    # A search hop must stay on the owning indexer's configured origin; httpx's
    # internal redirect following is off, so the Location is checked before it
    # is ever requested.
    respx_mock.get(f"{BASE}/api").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.invalid/steal"})
    )
    stolen = respx_mock.get("https://evil.invalid/steal").mock(
        return_value=httpx.Response(200, text="stolen")
    )
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.search({"t": "search"})

    assert "redirect to untrusted host blocked" in str(excinfo.value)
    assert stolen.call_count == 0


async def test_search_maps_invalid_redirect_location_to_indexer_error(make_client, respx_mock):
    # A malformed Location must surface as an IndexerError rather than escaping
    # as an unhandled exception (which Starlette would render as a 500). httpx
    # parses the Location while building ``next_request`` (even with redirect
    # following off), and the manual loop's own join is guarded too.
    route = respx_mock.get(f"{BASE}/api").mock(
        return_value=httpx.Response(302, headers={"location": "http://[::1"})
    )
    client = make_client()

    with pytest.raises(IndexerError):
        await client.search({"t": "search"})

    # The malformed target is never requested.
    assert route.call_count == 1


@pytest.mark.parametrize("headers", [{}, {"location": "   "}])
async def test_search_returns_3xx_without_followable_location(make_client, respx_mock, headers):
    # Nothing to follow: the (empty) body is returned instead of looping.
    route = respx_mock.get(f"{BASE}/api").mock(return_value=httpx.Response(302, headers=headers))
    client = make_client()

    assert await client.search({"t": "search"}) == ""
    assert route.call_count == 1


async def test_search_raises_after_too_many_redirects(make_client, respx_mock):
    route = respx_mock.get(f"{BASE}/api").mock(
        return_value=httpx.Response(302, headers={"location": "/api"})
    )
    client = make_client()

    with pytest.raises(IndexerError, match="too many redirects"):
        await client.search({"t": "search"})

    assert route.call_count == 6


async def test_search_rejects_redirect_hop_body_over_cap(make_client, respx_mock):
    # A 3xx hop used to be buffered unbounded by httpx's internal redirect
    # handling; the manual loop reads every hop through the capped stream.
    respx_mock.get(f"{BASE}/api").mock(
        return_value=httpx.Response(302, headers={"location": "/api2"})
    )
    stream = _TrackingStream([b"x" * 8] * 2)
    respx_mock.get(f"{BASE}/api2").mock(return_value=httpx.Response(200, stream=stream))
    client = make_client(max_body_bytes=10)

    with pytest.raises(IndexerError, match="upstream response too large"):
        await client.search({"t": "search"})

    assert stream.closed is True
    assert stream.chunks_read == 2


# --- fetch -------------------------------------------------------------------


async def test_fetch_returns_error_response_without_raising(make_client, respx_mock):
    respx_mock.get(f"{BASE}/getnzb/abc.nzb").mock(return_value=httpx.Response(404, text="gone"))
    client = make_client()

    response = await client.fetch(f"{BASE}/getnzb/abc.nzb", expected_origin=ORIGIN)

    assert response.status_code == 404
    assert response.text == "gone"


async def test_fetch_raises_indexer_error_on_transport_error(make_client, respx_mock):
    respx_mock.get(f"{BASE}/getnzb/abc.nzb").mock(side_effect=httpx.ConnectError("boom"))
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.fetch(f"{BASE}/getnzb/abc.nzb", expected_origin=ORIGIN)

    assert excinfo.value.status_code is None


async def test_fetch_follows_same_origin_redirect(make_client, respx_mock):
    first = respx_mock.get(f"{BASE}/redirect").mock(
        return_value=httpx.Response(302, headers={"location": "/final"})
    )
    second = respx_mock.get(f"{BASE}/final").mock(
        return_value=httpx.Response(200, content=b"payload")
    )
    client = make_client()

    response = await client.fetch(f"{BASE}/redirect", expected_origin=ORIGIN)

    assert response.status_code == 200
    assert response.content == b"payload"
    assert first.call_count == 1
    assert second.call_count == 1


async def test_fetch_blocks_cross_origin_redirect(make_client, respx_mock):
    respx_mock.get(f"{BASE}/redirect").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.invalid/steal"})
    )
    stolen = respx_mock.get("https://evil.invalid/steal").mock(
        return_value=httpx.Response(200, content=b"stolen")
    )
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.fetch(f"{BASE}/redirect", expected_origin=ORIGIN)

    assert excinfo.value.status_code is None
    assert "redirect to untrusted host blocked" in str(excinfo.value)
    assert stolen.call_count == 0


async def test_fetch_blocks_scheme_downgrade_redirect(make_client, respx_mock):
    # Same host, different scheme: still a different origin.
    respx_mock.get(f"{BASE}/redirect").mock(
        return_value=httpx.Response(302, headers={"location": "http://api.example.invalid/x"})
    )
    downgraded = respx_mock.get("http://api.example.invalid/x").mock(
        return_value=httpx.Response(200, content=b"plaintext")
    )
    client = make_client()

    with pytest.raises(IndexerError):
        await client.fetch(f"{BASE}/redirect", expected_origin=ORIGIN)

    assert downgraded.call_count == 0


async def test_fetch_returns_3xx_without_location_untouched(make_client, respx_mock):
    # A 302 with no Location is a real redirect status with nothing to follow;
    # the response must come back untouched instead of looping on itself.
    route = respx_mock.get(f"{BASE}/redirect").mock(return_value=httpx.Response(302))
    client = make_client()

    response = await client.fetch(f"{BASE}/redirect", expected_origin=ORIGIN)

    assert response.status_code == 302
    assert route.call_count == 1


async def test_fetch_returns_3xx_with_blank_location_untouched(make_client, respx_mock):
    # ``has_redirect_location`` is true for an empty Location, but there is no
    # target to follow and no hop budget worth burning.
    route = respx_mock.get(f"{BASE}/redirect").mock(
        return_value=httpx.Response(302, headers={"location": "   "})
    )
    client = make_client()

    response = await client.fetch(f"{BASE}/redirect", expected_origin=ORIGIN)

    assert response.status_code == 302
    assert route.call_count == 1


async def test_fetch_maps_invalid_redirect_location_to_indexer_error(make_client, respx_mock):
    # A malformed Location must surface as an IndexerError rather than escaping
    # as an unhandled exception (which Starlette would render as a 500).
    # ``httpx.InvalidURL`` is not an ``HTTPError``, so the join in the redirect
    # loop guards it explicitly in case httpx lets the malformed value through.
    route = respx_mock.get(f"{BASE}/redirect").mock(
        return_value=httpx.Response(302, headers={"location": "http://[::1"})
    )
    client = make_client()

    with pytest.raises(IndexerError):
        await client.fetch(f"{BASE}/redirect", expected_origin=ORIGIN)

    # The malformed target is never requested.
    assert route.call_count == 1


async def test_fetch_rejects_off_origin_initial_url(make_client, respx_mock):
    # The expected-origin contract is enforced inside fetch, not only by the
    # callers, so no future call site can bypass it.
    route = respx_mock.get("https://evil.invalid/steal").mock(
        return_value=httpx.Response(200, content=b"stolen")
    )
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.fetch("https://evil.invalid/steal", expected_origin=ORIGIN)

    assert excinfo.value.status_code is None
    assert "untrusted upstream origin" in str(excinfo.value)
    assert route.call_count == 0


async def test_fetch_raises_after_too_many_redirects(make_client, respx_mock):
    loop = respx_mock.get(f"{BASE}/loop").mock(
        return_value=httpx.Response(302, headers={"location": "/loop"})
    )
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.fetch(f"{BASE}/loop", expected_origin=ORIGIN)

    assert "too many redirects" in str(excinfo.value)
    # Five hops may be followed; discovering a sixth needs one more request.
    assert loop.call_count == 6


async def test_fetch_accepts_explicit_default_port_on_expected_origin(make_client, respx_mock):
    # An operator may configure ``host: https://api.example.invalid:443``; hop
    # URLs arrive normalized by httpx (default port stripped) and must still
    # compare equal.
    route = respx_mock.get(f"{BASE}/payload").mock(
        return_value=httpx.Response(200, content=b"payload")
    )
    client = make_client()

    response = await client.fetch(
        f"{BASE}/payload",
        expected_origin=origin_of("https://API.example.invalid:443"),
    )

    assert response.status_code == 200
    assert response.content == b"payload"
    assert route.call_count == 1


# --- body cap (audit 4.4) ----------------------------------------------------


class _TrackingStream(httpx.AsyncByteStream):
    """A finite response stream that records reads and whether it was closed."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_read = 0
        self.closed = False
        self.close_count = 0

    async def __aiter__(self):
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True
        self.close_count += 1


class _FailingStream(httpx.AsyncByteStream):
    """A response stream that yields one chunk and then fails non-httpx-style."""

    def __init__(self) -> None:
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        self.reads += 1
        yield b"partial"
        raise RuntimeError("boom")

    async def aclose(self) -> None:
        self.closed = True


async def test_search_closes_response_when_read_fails_with_non_httpx_error(make_client, respx_mock):
    # ``asyncio.CancelledError``/arbitrary stream failures are not ``HTTPError``
    # subclasses; the read loop's ``finally`` must still release the response.
    stream = _FailingStream()
    respx_mock.get(f"{BASE}/api").mock(return_value=httpx.Response(200, stream=stream))
    client = make_client()

    with pytest.raises(RuntimeError, match="boom"):
        await client.search({"t": "search"})

    assert stream.closed is True


async def test_fetch_response_reclose_is_idempotent(make_client, respx_mock):
    # The redirect loop closes every hop explicitly; httpx resolves the read to
    # completion and the caller may close again. ``aclose`` must stay a no-op.
    stream = _TrackingStream([b"payload"])
    respx_mock.get(f"{BASE}/payload").mock(return_value=httpx.Response(200, stream=stream))
    client = make_client()

    response = await client.fetch(f"{BASE}/payload", expected_origin=ORIGIN)
    await response.aclose()
    await response.aclose()

    assert response.text == "payload"
    assert stream.close_count == 1


async def test_search_returns_body_exactly_at_cap(make_client, respx_mock):
    respx_mock.get(f"{BASE}/api").mock(return_value=httpx.Response(200, content=b"0123456789"))
    client = make_client(max_body_bytes=10)

    assert await client.search({"t": "search"}) == "0123456789"


async def test_search_rejects_body_over_cap_and_stops_reading(make_client, respx_mock):
    stream = _TrackingStream([b"0123456789"] * 5)
    respx_mock.get(f"{BASE}/api").mock(return_value=httpx.Response(200, stream=stream))
    client = make_client(max_body_bytes=25)

    with pytest.raises(IndexerError) as excinfo:
        await client.search({"t": "search"})

    assert excinfo.value.status_code is None
    assert "upstream response too large" in str(excinfo.value)
    # The stream is abandoned as soon as the cap is crossed: the remaining
    # chunks are never read (no partial replay) and it is closed.
    assert stream.closed is True
    assert stream.chunks_read == 3


async def test_fetch_returns_body_exactly_at_cap(make_client, respx_mock):
    respx_mock.get(f"{BASE}/payload").mock(return_value=httpx.Response(200, content=b"payloaddata"))
    client = make_client(max_body_bytes=11)

    response = await client.fetch(f"{BASE}/payload", expected_origin=ORIGIN)

    assert response.status_code == 200
    assert response.content == b"payloaddata"
    assert response.text == "payloaddata"


async def test_fetch_rejects_body_over_cap(make_client, respx_mock):
    stream = _TrackingStream([b"x" * 8] * 4)
    respx_mock.get(f"{BASE}/payload").mock(return_value=httpx.Response(200, stream=stream))
    client = make_client(max_body_bytes=10)

    with pytest.raises(IndexerError, match="upstream response too large"):
        await client.fetch(f"{BASE}/payload", expected_origin=ORIGIN)

    assert stream.closed is True
    assert stream.chunks_read == 2


async def test_fetch_rejects_redirect_hop_body_over_cap(make_client, respx_mock):
    respx_mock.get(f"{BASE}/redirect").mock(
        return_value=httpx.Response(302, headers={"location": "/final"})
    )
    stream = _TrackingStream([b"x" * 8] * 2)
    respx_mock.get(f"{BASE}/final").mock(return_value=httpx.Response(200, stream=stream))
    client = make_client(max_body_bytes=10)

    with pytest.raises(IndexerError, match="upstream response too large"):
        await client.fetch(f"{BASE}/redirect", expected_origin=ORIGIN)

    assert stream.closed is True
    assert stream.chunks_read == 2


# --- origin_of ---------------------------------------------------------------


def test_origin_of_canonicalizes_scheme_host_and_default_port():
    assert origin_of("https://API.Example.NET:443/x") == origin_of("https://api.example.net/y")
    assert origin_of("HTTPS://API.example.invalid/path?q=1") == (
        "https",
        "api.example.invalid",
    )


def test_origin_of_keeps_non_default_port():
    assert origin_of("https://api.example.net:8443/a") != origin_of("https://api.example.net/b")
    assert origin_of("https://api.example.net:8443/a") == (
        "https",
        "api.example.net:8443",
    )


def test_origin_of_includes_userinfo():
    assert origin_of("https://user:pass@api.example.net/a") != origin_of(
        "https://api.example.net/a"
    )
    assert origin_of("https://evil@api.example.net/a") != origin_of("https://api.example.net/a")


def test_origin_of_scheme_downgrade_differs():
    assert origin_of("http://api.example.net/a") != origin_of("https://api.example.net/a")


def test_origin_of_idna_encodes_non_ascii_hosts():
    assert origin_of("https://bücher.example/a") == origin_of("https://xn--bcher-kva.example/b")


def test_origin_of_rejects_unparseable_and_relative_urls():
    assert origin_of("/relative/path") == ("", "")
    assert origin_of("https://[::1/x") == ("", "")


# --- supports ----------------------------------------------------------------


async def test_supports_true_for_advertised_function(make_client):
    client = make_client(_indexer(search_types=["search", "tvsearch"]))

    assert client.supports("tvsearch") is True
    assert client.supports("movie") is False


# --- close -------------------------------------------------------------------


async def test_close_marks_client_closed_and_is_idempotent():
    client = IndexerClient(_indexer(), API_KEY, UA)

    assert client.client.is_closed is False
    await client.close()
    assert client.client.is_closed is True
    await client.close()
    assert client.client.is_closed is True
