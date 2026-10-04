"""Tests for the per-indexer async HTTP client.

All network I/O is intercepted with ``respx``; nothing here touches the network.
Fixtures live in this file because ``tests/conftest.py`` is owned by a later task.
"""

from __future__ import annotations

import httpx
import pytest

from stateless_hydra.config import IndexerConfig
from stateless_hydra.indexer_client import IndexerClient, IndexerError, resolve_proxy_url

UA = "test-agent/1.0"
API_KEY = "s3cret"
BASE = "https://api.example.invalid"


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


# --- fetch -------------------------------------------------------------------


async def test_fetch_returns_error_response_without_raising(make_client, respx_mock):
    respx_mock.get(f"{BASE}/getnzb/abc.nzb").mock(return_value=httpx.Response(404, text="gone"))
    client = make_client()

    response = await client.fetch(f"{BASE}/getnzb/abc.nzb")

    assert response.status_code == 404
    assert response.text == "gone"


async def test_fetch_raises_indexer_error_on_transport_error(make_client, respx_mock):
    respx_mock.get(f"{BASE}/getnzb/abc.nzb").mock(side_effect=httpx.ConnectError("boom"))
    client = make_client()

    with pytest.raises(IndexerError) as excinfo:
        await client.fetch(f"{BASE}/getnzb/abc.nzb")

    assert excinfo.value.status_code is None


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
