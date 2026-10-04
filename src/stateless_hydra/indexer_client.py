"""Per-indexer async HTTP client for Newznab-compatible upstream indexers.

Each :class:`IndexerClient` wraps a single :class:`httpx.AsyncClient` configured
with the indexer's timeout, an explicit ``User-Agent``, and the proxy that
applies to that indexer (a per-indexer override or the global default). The
client is stateless: it holds no history and no credentials beyond the resolved
API key required to build request URLs.

Transport failures and unexpected HTTP error statuses are normalized to
:class:`IndexerError` so callers do not have to know about httpx. The
:meth:`IndexerClient.fetch` method deliberately returns 4xx/5xx responses
untouched because NZB payload endpoints may answer with unusual status codes
that only the caller can interpret.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from .config import IndexerConfig
from .exceptions import StatelessHydraError


class IndexerError(StatelessHydraError):
    """Raised when a request to an upstream indexer fails.

    ``status_code`` is set when the failure was an HTTP error status and
    ``None`` for transport-level failures (connection errors, timeouts).
    """

    def __init__(
        self,
        indexer_name: str,
        message: str,
        status_code: int | None = None,
    ) -> None:
        self.indexer_name = indexer_name
        self.status_code = status_code
        detail = f"indexer {indexer_name!r}: {message}"
        if status_code is not None:
            detail = f"{detail} (HTTP {status_code})"
        super().__init__(detail)


def resolve_proxy_url(indexer: IndexerConfig, global_proxy_url: str | None) -> str | None:
    """Return the proxy URL that applies to ``indexer``.

    A per-indexer ``proxy_url`` takes precedence over ``global_proxy_url``.
    ``None`` means no proxy is configured.
    """
    return indexer.proxy_url or global_proxy_url


class IndexerClient:
    """Async HTTP client bound to a single configured indexer."""

    def __init__(
        self,
        indexer: IndexerConfig,
        api_key: str,
        user_agent: str,
        global_proxy_url: str | None = None,
    ) -> None:
        self._indexer = indexer
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(indexer.timeout_seconds),
            proxy=resolve_proxy_url(indexer, global_proxy_url),
            follow_redirects=True,
        )

    @property
    def name(self) -> str:
        """The indexer's unique name."""
        return self._indexer.name

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying httpx client (exposed for inspection in tests)."""
        return self._client

    def supports(self, function: str) -> bool:
        """Whether this indexer advertises the given Newznab function."""
        return function in self._indexer.search_types

    def build_url(self, params: Mapping[str, str]) -> str:
        """Build the full request URL for ``params``.

        The resolved API key is always appended as ``apikey``; a caller-supplied
        ``apikey`` value is replaced so exactly one key is sent.
        """
        query = {key: value for key, value in params.items() if key != "apikey"}
        query["apikey"] = self._api_key
        return f"{self._indexer.host}{self._indexer.api_path}?{urlencode(query)}"

    async def search(self, params: Mapping[str, str]) -> str:
        """Perform a Newznab search and return the raw response body."""
        url = self.build_url(params)
        try:
            response = await self._client.get(url)
        except httpx.TimeoutException as exc:
            raise IndexerError(self.name, f"request timed out: {exc}", None) from exc
        except httpx.HTTPError as exc:
            raise IndexerError(self.name, f"request failed: {exc}", None) from exc

        if response.status_code >= 400:
            raise IndexerError(
                self.name,
                f"unexpected status {response.status_code}",
                response.status_code,
            )
        return response.text

    async def fetch(self, url: str) -> httpx.Response:
        """GET ``url`` for proxying details/NZB payloads.

        Unlike :meth:`search`, HTTP error statuses are returned to the caller
        rather than raised; only transport failures raise :class:`IndexerError`.
        """
        try:
            return await self._client.get(url)
        except httpx.TimeoutException as exc:
            raise IndexerError(self.name, f"request timed out: {exc}", None) from exc
        except httpx.HTTPError as exc:
            raise IndexerError(self.name, f"request failed: {exc}", None) from exc

    async def close(self) -> None:
        """Close the underlying client. Safe to call more than once."""
        await self._client.aclose()
