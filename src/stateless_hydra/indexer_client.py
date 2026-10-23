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
that only the caller can interpret. Both :meth:`IndexerClient.search` and
:meth:`IndexerClient.fetch` follow redirects manually so that every hop is
origin-checked and streamed through the same bounded body read.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode, urlsplit

import httpx

from .config import DEFAULT_MAX_UPSTREAM_BODY_MIB, IndexerConfig
from .exceptions import StatelessHydraError

# An ``(scheme, authority)`` pair identifying the one origin a request may reach.
Origin = tuple[str, str]

# Redirect hops followed by ``search``/``fetch`` before giving up.
_MAX_REDIRECT_HOPS = 5

# Ports that are implied by a scheme and therefore omitted when canonicalizing.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Bytes per MiB, for converting the MiB-valued config knob at this byte-level
# boundary.
_BYTES_PER_MIB = 1024 * 1024


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


def origin_of(url: str) -> Origin:
    """Return the canonical ``(scheme, authority)`` origin of ``url``.

    Callers compare this tuple against the origin they expect a request to
    reach, so a ``getnzb``/``details`` fetch can never leave the origin pinned
    by the caller (the owning indexer's host for rebuilt URLs, and the guid's
    own host for a URL-shaped guid fetched directly). Both sides of that
    comparison go through this function, including hop URLs (which are first
    normalized by ``httpx.URL``), so the normalization here has to match what
    ``httpx`` produces: the host is lowercased and IDNA-encoded, and a port
    equal to the scheme's default is dropped. Otherwise
    ``host: https://API.Example.NET:443`` would never match a redirect to
    ``https://api.example.net/...``.

    Userinfo is preserved verbatim: a hop that carries credentials is a
    different origin from the indexer's own host, so it still fails the
    comparison. An unparseable or relative URL yields ``("", "")`` and
    therefore matches nothing.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "", ""
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not host:
        return "", ""
    host = host.lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            # A label IDNA rejects (too long, empty, ...) keeps its raw form so
            # the comparison still works consistently on both sides.
            pass
    if ":" in host:
        # IPv6 literals come back unbracketed from ``urlsplit().hostname``.
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    userinfo = parts.netloc.rpartition("@")[0]
    if userinfo:
        host = f"{userinfo}@{host}"
    return scheme, host


class IndexerClient:
    """Async HTTP client bound to a single configured indexer."""

    def __init__(
        self,
        indexer: IndexerConfig,
        api_key: str,
        user_agent: str,
        global_proxy_url: str | None = None,
        max_body_bytes: int = DEFAULT_MAX_UPSTREAM_BODY_MIB * _BYTES_PER_MIB,
    ) -> None:
        self._indexer = indexer
        self._api_key = api_key
        self._max_body_bytes = max_body_bytes
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(indexer.timeout_seconds),
            proxy=resolve_proxy_url(indexer, global_proxy_url),
            # Redirects are always followed manually (see _follow_redirects) so
            # every hop is origin-checked and body-capped. Leaving httpx's own
            # redirect handling off also prevents an implicit hop from buffering
            # an unbounded body before the origin check runs.
            follow_redirects=False,
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
        """Perform a Newznab search and return the raw response body.

        The body is streamed and bounded by ``max_body_bytes``; a response that
        exceeds the cap is abandoned and surfaces as :class:`IndexerError`
        instead of being buffered into the pod's memory without limit.

        Redirects are followed manually through :meth:`_follow_redirects`, with
        every hop pinned to the owning indexer's configured origin and read
        through the same capped stream. httpx's internal redirect following is
        not used: it buffers each hop's body without any bound.
        """
        url = self.build_url(params)
        # The URL is built from the indexer's configured host, so that host is
        # the only origin a hop may reach.
        expected_origin = origin_of(self._indexer.host)
        response = await self._follow_redirects(url, expected_origin)

        if response.status_code >= 400:
            raise IndexerError(
                self.name,
                f"unexpected status {response.status_code}",
                response.status_code,
            )
        return response.text

    async def fetch(self, url: str, *, expected_origin: Origin) -> httpx.Response:
        """GET ``url`` for proxying details/NZB payloads.

        Unlike :meth:`search`, HTTP error statuses are returned to the caller
        rather than raised; only transport failures raise :class:`IndexerError`.

        The initial ``url`` must also be on ``expected_origin`` (enforced by
        :meth:`_follow_redirects`). Callers already hold every URL they pass
        here to that origin, so this is a no-op today, but it keeps the
        contract true for any future call site instead of relying on each
        caller to remember.
        """
        return await self._follow_redirects(url, expected_origin)

    async def _follow_redirects(self, url: str, expected_origin: Origin) -> httpx.Response:
        """GET ``url``, following bounded same-origin redirects.

        The initial ``url`` and every ``Location`` hop must be on
        ``expected_origin``; a hop to any other origin raises
        :class:`IndexerError` without being fetched, so an upstream cannot pivot
        this pod onto an unrelated or in-cluster address. At most
        :data:`_MAX_REDIRECT_HOPS` hops are followed; a redirect beyond that
        raises :class:`IndexerError`. A ``3xx`` with no ``Location`` header (or
        a blank one) is returned untouched because there is nothing to follow,
        and a malformed one raises :class:`IndexerError`.

        Every request (including each redirect hop) is streamed through
        :meth:`_get`, so each hop's body is bounded by ``max_body_bytes``; an
        oversized hop raises :class:`IndexerError` just like an oversized direct
        payload.
        """
        if origin_of(url) != expected_origin:
            raise IndexerError(self.name, "untrusted upstream origin", None)
        current_url = url
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            response = await self._get(current_url)
            if not response.has_redirect_location:
                return response
            location = response.headers.get("location", "").strip()
            if not location:
                # Nothing to follow; re-requesting ``current_url`` would only
                # burn the hop budget before failing.
                return response
            try:
                next_url = str(httpx.URL(current_url).join(location))
            except httpx.InvalidURL as exc:
                # ``InvalidURL`` is not an ``HTTPError``, so letting it escape
                # would surface as a 500 instead of an indexer failure.
                await response.aclose()
                raise IndexerError(self.name, "invalid redirect location", None) from exc
            if origin_of(next_url) != expected_origin:
                await response.aclose()
                raise IndexerError(self.name, "redirect to untrusted host blocked", None)
            await response.aclose()
            current_url = next_url
        raise IndexerError(self.name, "too many redirects", None)

    async def _get(self, url: str) -> httpx.Response:
        """GET ``url`` without following redirects, normalizing transport errors.

        Every hop is streamed through :meth:`_read_capped`, so a redirect body
        cannot bypass the response-size ceiling either.
        """
        return await self._streamed_get(url)

    async def _streamed_get(self, url: str) -> httpx.Response:
        """Stream a GET and return a fully-read, size-capped response.

        Transport failures (including ones raised while consuming the body) are
        normalized to :class:`IndexerError`. The response handed back has its
        ``_content`` populated from the bounded buffer, so callers keep the
        usual ``status_code``/``headers``/``content`` contract.
        """
        try:
            request = self._client.build_request("GET", url)
            response = await self._client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise IndexerError(self.name, f"request timed out: {exc}", None) from exc
        except httpx.HTTPError as exc:
            raise IndexerError(self.name, f"request failed: {exc}", None) from exc

        try:
            await self._read_capped(response)
        except httpx.TimeoutException as exc:
            raise IndexerError(self.name, f"request timed out: {exc}", None) from exc
        except httpx.HTTPError as exc:
            raise IndexerError(self.name, f"request failed: {exc}", None) from exc
        finally:
            # Always release the stream/socket, including when the body read is
            # cancelled or fails with a non-httpx exception. ``aclose`` guards
            # on ``is_closed`` internally, so closing an already-closed response
            # is safe (and deliberate: the cap/redirect paths rely on it).
            await response.aclose()
        return response

    async def _read_capped(self, response: httpx.Response) -> None:
        """Drain ``response`` into memory, refusing to exceed the body cap.

        The body is accumulated in a ``bytearray`` and reading stops as soon as
        the cap is crossed, so an oversized upstream response is abandoned (no
        partial replay) and raises :class:`IndexerError`. The caller closes the
        response in a ``finally`` block. On success the buffered bytes are
        installed as ``response._content`` so the returned
        :class:`httpx.Response` behaves like a normally read one.
        """
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self._max_body_bytes:
                raise IndexerError(self.name, "upstream response too large", None)
        response._content = bytes(body)

    async def close(self) -> None:
        """Close the underlying client. Safe to call more than once."""
        await self._client.aclose()
