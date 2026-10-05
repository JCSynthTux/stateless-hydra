"""HTTP API routes: the Newznab-compatible endpoint and operational probes.

The public surface mirrors nzbhydra2's external API:

* ``GET /api`` dispatches on the ``t`` query parameter (``caps``, ``search``,
  ``tvsearch``, ``movie``, ``music``, ``book``, ``details``, ``getnzb``, and the
  standard newznab ``get`` alias for downloads);
* ``GET /healthz`` is a dependency-free liveness probe;
* ``GET /readyz`` reports Redis connectivity for the readiness probe;
* ``GET /metrics`` exposes Prometheus metrics without authentication.

Everything in this module operates on ``request.app.state`` (populated by
:func:`stateless_hydra.main.create_app`); there is no global mutable state.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..cache import cache_key_for
from ..config import IndexerConfig
from ..indexer_client import IndexerClient, IndexerError
from ..limits import LimitKind
from ..metrics import Metrics
from ..newznab import (
    NewznabError,
    ParsedRss,
    ResultItem,
    canonical_query,
    compose_guid,
    parse_indexer_rss,
    parse_upstream_error,
    render_caps,
    render_error,
    render_results,
    split_guid,
)

logger = logging.getLogger(__name__)

router = APIRouter()

XML_MEDIA_TYPE = "application/xml"
JSON_MEDIA_TYPE = "application/json"

_CAPS_SEARCH_TYPES = ["search", "tvsearch", "movie", "music", "book"]
_SEARCH_FUNCTIONS = frozenset({"search", "tvsearch", "movie", "music", "book"})
# ``get`` is the standard newznab download function; ``getnzb`` is nzbhydra2's
# own spelling. Both route to the same handler.
_KNOWN_FUNCTIONS = _SEARCH_FUNCTIONS | {"caps", "details", "getnzb", "get"}

# Query parameters interpreted by the proxy itself and never forwarded upstream.
_CONTROL_PARAMS = frozenset({"t", "apikey", "o", "offset", "limit", "indexer"})

# A search is only meaningful with at least one of these.
_SEARCH_PARAMS = (
    "q",
    "imdbid",
    "tmdbid",
    "tvdbid",
    "rid",
    "season",
    "ep",
    "artist",
    "album",
    "track",
    "label",
    "year",
    "author",
    "title",
    "genre",
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Client-facing NZB downloads are addressed by an opaque token, never by the
# upstream URL: that URL may embed an indexer API key (e.g. drunkenSlug's
# ``r=`` parameter). Tokens live in Redis for two weeks so an advertised NZB
# stays grabbable well after the search that produced it; Redis is the shared
# store across replicas, so any pod can resolve any token.
_NZB_TOKEN_TTL = 14 * 24 * 3600
_NZB_TOKEN_PREFIX = "stateless_hydra:nzb"


def _nzb_token_key(token: str) -> str:
    """Redis key holding the upstream URL for a client-facing download token."""
    return f"{_NZB_TOKEN_PREFIX}:{token}"


def _derive_download_url(owner: IndexerConfig, client: IndexerClient, original_guid: str) -> str:
    """Derive the upstream download URL for an item at search time.

    A URL-shaped guid is the download itself (altHUB-style .nzb URLs, and
    nZEDb indexers whose permalink ``<link>`` is the .nzb URL) and is fetched
    directly, unless the indexer opts into a rebuild. Everything else is
    rebuilt through the indexer's configured ``downloadFunction``.
    """
    if not owner.force_getnzb_rebuild and original_guid.startswith(("http://", "https://")):
        return original_guid
    return client.build_url({"t": owner.download_function, "id": original_guid})


def _decode_nzb_token(raw: str | bytes | None) -> dict[str, Any] | None:
    """Decode a stored token payload, returning ``None`` when absent/corrupt."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _nzb_filename(token: str) -> str:
    """A header-safe filename stem for a download token.

    Normal tokens are urlsafe and pass through unchanged. Legacy URL-shaped
    guids are client-supplied, so only their final path segment is used and
    unsafe characters are replaced, preventing Content-Disposition injection.
    """
    candidate = token.rstrip("/").rsplit("/", 1)[-1]
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in candidate)
    return safe or "download"


def _render(payload: str | dict[str, Any], o: str) -> Response:
    """Render a protocol document as XML (default) or JSON."""
    if o == "json":
        return JSONResponse(content=payload, media_type=JSON_MEDIA_TYPE)
    return Response(content=payload, media_type=XML_MEDIA_TYPE)


def _parse_non_negative(value: str, name: str) -> int:
    """Parse an ``offset``/``limit`` value, mapping failures to error 201."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise NewznabError(201, f"Incorrect parameter: {name}") from None
    if parsed < 0:
        raise NewznabError(201, f"Incorrect parameter: {name}")
    return parsed


def _pub_date_key(pair: tuple[str, ResultItem]) -> datetime:
    """Sort key: parsed RFC 2822 pubDate, or the epoch when unparseable."""
    try:
        parsed = parsedate_to_datetime(pair[1].pub_date)
    except (TypeError, ValueError):
        return _EPOCH
    if parsed is None:
        return _EPOCH
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _dedupe_by_title(
    ordered: list[tuple[str, ResultItem]],
) -> list[tuple[str, ResultItem]]:
    """Drop later duplicates sharing a non-empty title (case-insensitive).

    Items with an empty title are always kept: an empty title carries no
    identity, so collapsing them would silently discard unrelated results.
    """
    seen: set[str] = set()
    deduped: list[tuple[str, ResultItem]] = []
    for pair in ordered:
        title = (pair[1].title or "").strip().lower()
        if not title:
            deduped.append(pair)
            continue
        if title in seen:
            continue
        seen.add(title)
        deduped.append(pair)
    return deduped


def _enabled_owner(request: Request, indexer_name: str) -> bool:
    """Whether ``indexer_name`` exists and is enabled.

    Clients are built for every configured indexer (so configuration errors
    surface at startup), which means ``app.state.clients`` alone would allow a
    composed guid to reach a disabled indexer. Details/getnzb must therefore
    check the owning :class:`IndexerConfig` as well.
    """
    indexer = request.app.state.indexers.get(indexer_name)
    return indexer is not None and indexer.enabled


def _download_url(request: Request, composed_guid: str) -> str:
    """Build this proxy's ``getnzb`` URL for a composed guid.

    The URL points at our own ``/api`` endpoint so downloads flow back through
    the limit tracker and indexer routing, matching nzbhydra2 (which advertises
    its own download link in each item). The caller's API key is carried in the
    query string because clients fetch the enclosure URL verbatim and would not
    otherwise authenticate. It is the caller's own credential, never an indexer
    key.

    The function is advertised as the standard newznab ``t=get`` (with the
    release identity in ``id``) rather than ``t=getnzb``. AIOStreams identifies
    an NZB by hashing its URL: its ``hashNzbUrl`` knows the ``/api?t=get&id=...``
    shape and keeps ``t`` and ``id``, but does not know ``t=getnzb``, so it falls
    back to stripping the whole query. Every item then hashed to the same
    ``/api`` value, collapsing all results into one release (wrong file
    selection and deduplication). ``get`` keeps each item's identity in the
    canonical hash; ``getnzb`` remains accepted server-side for older clients.
    """
    base = str(request.base_url).rstrip("/")
    apikey = request.query_params.get("apikey", "")
    query = urlencode({"t": "get", "id": composed_guid, "apikey": apikey})
    return f"{base}{request.url.path}?{query}"


async def _update_limit_gauge(request: Request, indexer: str, kind: LimitKind) -> None:
    """Refresh the ``limit_remaining`` gauge from the current budget status."""
    status = await request.app.state.limits.status(indexer, kind)
    if status.remaining is not None:
        request.app.state.metrics.set_limit_remaining(indexer, kind.value, status.remaining)


async def _query_indexers(
    request: Request,
    function: str,
    forwarded: dict[str, str],
    limit: int,
    filter_name: str | None,
) -> tuple[list[tuple[str, ResultItem]], bool, int, int]:
    """Fan out a search across eligible indexers and collect their items.

    Returns ``(merged, results_obtained, limit_skips, error_skips)``.
    """
    app = request.app
    settings = app.state.settings
    metrics = app.state.metrics

    indexer_limit = min(limit or 100, settings.max_results_per_indexer)
    merged: list[tuple[str, ResultItem]] = []
    results_obtained = False
    limit_skips = 0
    error_skips = 0

    for name, indexer in app.state.indexers.items():
        if not indexer.enabled:
            continue
        if filter_name is not None and name != filter_name:
            continue

        client = app.state.clients[name]
        if client.supports(function):
            effective = function
        elif client.supports("search"):
            effective = "search"
        else:
            continue

        params = {**forwarded, "t": effective, "limit": str(indexer_limit)}
        key = cache_key_for(canonical_query(params))

        cached = await app.state.cache.get(name, key)
        if cached is not None:
            metrics.inc_cache_hit(name)
            parsed = _parse_or_none(name, cached, metrics)
            if parsed is None:
                error_skips += 1
                continue
            results_obtained = True
            merged.extend((name, item) for item in parsed.items)
            continue

        metrics.inc_cache_miss(name)
        allowed = await app.state.limits.consume(name, LimitKind.API)
        await _update_limit_gauge(request, name, LimitKind.API)
        if not allowed:
            metrics.inc_limit_reached(name, LimitKind.API.value)
            limit_skips += 1
            continue

        metrics.inc_api_hit(name)
        try:
            text = await client.search(params)
        except IndexerError:
            logger.warning("indexer %s search failed", name, exc_info=True)
            metrics.inc_error(name)
            error_skips += 1
            continue

        # Parse before caching: a malformed response must not be cached and
        # re-served as a permanent error on every subsequent search.
        parsed = _parse_or_none(name, text, metrics)
        if parsed is None:
            error_skips += 1
            continue
        await app.state.cache.set(name, key, text, indexer.cache_ttl_seconds)
        results_obtained = True
        merged.extend((name, item) for item in parsed.items)

    return merged, results_obtained, limit_skips, error_skips


def _parse_or_none(name: str, text: str, metrics: Metrics) -> ParsedRss | None:
    """Parse an indexer feed, counting and swallowing malformed responses.

    Indexers frequently answer a *search* with HTTP 200 and a Newznab
    ``<error code=... description=.../>`` body (nZEDb returns HTTP 200 for every
    failure). Such a body has no ``<channel>`` and would otherwise parse as a
    perfectly valid empty feed -- a silent zero-result that is indistinguishable
    from "no matches". Detect it here so the indexer is skipped, an error is
    logged and counted, and the empty-result logic (900/910) sees the failure.
    """
    upstream_error = parse_upstream_error(text)
    if upstream_error is not None:
        code, description = upstream_error
        logger.warning(
            "indexer %s returned upstream error %s: %s",
            name,
            code,
            description or "no description",
        )
        metrics.inc_error(name)
        return None
    try:
        return parse_indexer_rss(text)
    except ValueError:
        logger.warning("indexer %s returned malformed RSS", name, exc_info=True)
        metrics.inc_error(name)
        return None


async def _handle_search(request: Request, function: str, o: str) -> Response:
    query = request.query_params
    metrics = request.app.state.metrics

    if not any(query.get(param) for param in _SEARCH_PARAMS):
        raise NewznabError(200, "Missing query parameter q (or other search parameter)")

    offset = _parse_non_negative(query.get("offset", "0"), "offset")
    limit = _parse_non_negative(query.get("limit", "100"), "limit")

    # A zero limit asks for no results; answer immediately rather than querying
    # indexers for data that would only be sliced away.
    if limit == 0:
        metrics.inc_search(function)
        start = time.monotonic()
        try:
            return _render(render_results([], 0, 0, o), o)
        finally:
            metrics.observe_search_duration(function, time.monotonic() - start)

    forwarded: dict[str, str] = {}
    for key, value in query.multi_items():
        if key in _CONTROL_PARAMS:
            continue
        if key not in forwarded:
            forwarded[key] = value

    filter_name = query.get("indexer")

    metrics.inc_search(function)
    start = time.monotonic()
    try:
        merged, results_obtained, limit_skips, error_skips = await _query_indexers(
            request, function, forwarded, limit, filter_name
        )
    finally:
        metrics.observe_search_duration(function, time.monotonic() - start)

    if not results_obtained:
        if limit_skips > 0:
            raise NewznabError(910)
        if error_skips > 0:
            raise NewznabError(900, "All indexers failed")
        raise NewznabError(203, "Function not available")

    ordered = sorted(merged, key=_pub_date_key, reverse=True)
    if request.app.state.settings.dedupe_by_title:
        ordered = _dedupe_by_title(ordered)

    total = len(ordered)
    page = ordered[offset : offset + limit]
    items = []
    for name, item in page:
        owner = request.app.state.indexers[name]
        client = request.app.state.clients[name]
        upstream_url = _derive_download_url(owner, client, item.guid)
        # The upstream URL may embed an indexer API key, so it is stored
        # server-side and only an opaque token is rendered to the client. On a
        # cache-hit re-render this runs again, refreshing the token's TTL.
        token = secrets.token_urlsafe(16)
        await request.app.state.redis.set(
            _nzb_token_key(token),
            json.dumps({"indexer": name, "url": upstream_url}),
            ex=_NZB_TOKEN_TTL,
        )
        guid = compose_guid(name, token)
        download_url = _download_url(request, guid)
        # nzbhydra2 advertises its own download link as both the item link and
        # the enclosure URL; AIOStreams (newznab) requires the enclosure to
        # build an NZB URL, and Sonarr/Radarr read <link>.
        items.append(
            item.model_copy(
                update={"guid": guid, "link": download_url, "enclosure_url": download_url}
            )
        )
    return _render(render_results(items, total, offset, o), o)


async def _handle_details(request: Request) -> Response:
    app = request.app
    metrics = app.state.metrics
    item_id = request.query_params.get("id")
    if not item_id:
        raise NewznabError(200, "Missing parameter: id")

    split = split_guid(item_id)
    if split is None:
        raise NewznabError(300, "No such item")
    indexer_name, original_guid = split
    if not _enabled_owner(request, indexer_name):
        raise NewznabError(300, "No such item")
    client = app.state.clients[indexer_name]

    allowed = await app.state.limits.consume(indexer_name, LimitKind.API)
    await _update_limit_gauge(request, indexer_name, LimitKind.API)
    if not allowed:
        metrics.inc_limit_reached(indexer_name, LimitKind.API.value)
        raise NewznabError(910)
    metrics.inc_api_hit(indexer_name)

    try:
        response = await client.fetch(client.build_url({"t": "details", "id": original_guid}))
    except IndexerError:
        logger.warning("indexer %s details failed", indexer_name, exc_info=True)
        metrics.inc_error(indexer_name)
        raise NewznabError(900) from None

    if response.status_code >= 400:
        upstream_error = parse_upstream_error(response.content)
        if upstream_error is not None:
            raise NewznabError(*upstream_error)
        raise NewznabError(300 if response.status_code == 404 else 900)
    upstream_error = parse_upstream_error(response.content)
    if upstream_error is not None:
        raise NewznabError(*upstream_error)
    return Response(content=response.content, media_type=XML_MEDIA_TYPE)


async def _handle_getnzb(request: Request) -> Response:
    app = request.app
    metrics = app.state.metrics
    item_id = request.query_params.get("id")
    if not item_id:
        raise NewznabError(200, "Missing parameter: id")

    split = split_guid(item_id)
    if split is None:
        raise NewznabError(300, "No such item")
    indexer_name, token = split
    if not _enabled_owner(request, indexer_name):
        raise NewznabError(300, "No such item")
    client = app.state.clients[indexer_name]

    if token.startswith(("http://", "https://")):
        # Backward compatibility: guids issued before download tokens existed
        # are upstream URLs (possibly carrying an indexer key). Fetch directly.
        upstream_url = token
    else:
        # Resolve the opaque token to the upstream URL stored at search time.
        stored = _decode_nzb_token(await app.state.redis.get(_nzb_token_key(token)))
        if stored is None or stored.get("indexer") != indexer_name:
            raise NewznabError(300, "No such item (download token expired or unknown)")
        upstream_url = stored.get("url")
        if not isinstance(upstream_url, str) or not upstream_url:
            raise NewznabError(300, "No such item (download token expired or unknown)")

    allowed = await app.state.limits.consume(indexer_name, LimitKind.NZB)
    await _update_limit_gauge(request, indexer_name, LimitKind.NZB)
    if not allowed:
        metrics.inc_limit_reached(indexer_name, LimitKind.NZB.value)
        raise NewznabError(930)
    metrics.inc_nzb_pull(indexer_name)

    try:
        response = await client.fetch(upstream_url)
    except IndexerError:
        logger.warning("indexer %s getnzb failed", indexer_name, exc_info=True)
        metrics.inc_error(indexer_name)
        raise NewznabError(900) from None

    # An indexer may answer 200 (or an error status) with a Newznab error
    # document instead of an NZB. Surface its code/description rather than
    # streaming the error body as a fake NZB.
    upstream_error = parse_upstream_error(response.content)
    if upstream_error is not None:
        logger.warning("indexer %s getnzb returned error %s", indexer_name, upstream_error[0])
        metrics.inc_error(indexer_name)
        raise NewznabError(*upstream_error)

    if response.status_code >= 400:
        raise NewznabError(300 if response.status_code == 404 else 900)

    headers = {"Content-Disposition": f'attachment; filename="{_nzb_filename(token)}.nzb"'}
    return StreamingResponse(
        response.aiter_bytes(), media_type="application/x-nzb", headers=headers
    )


@router.get("/api")
async def newznab_api(request: Request) -> Response:
    """Newznab-compatible dispatcher for every supported ``t`` function."""
    query = request.query_params

    apikey = query.get("apikey")
    if not apikey:
        raise NewznabError(100, "Missing API key")
    if apikey not in request.app.state.hydra_api_keys:
        raise NewznabError(100, "Incorrect API key")

    function = query.get("t")
    if not function:
        raise NewznabError(200, "Missing parameter: t")
    if function not in _KNOWN_FUNCTIONS:
        raise NewznabError(202, f"No such function: {function}")

    o = query.get("o", "xml")

    if function == "caps":
        return _render(render_caps(_CAPS_SEARCH_TYPES, o), o)
    if function in _SEARCH_FUNCTIONS:
        return await _handle_search(request, function, o)
    if function == "details":
        return await _handle_details(request)
    return await _handle_getnzb(request)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Dependency-free liveness probe."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> Response:
    """Readiness probe: ready only when Redis is reachable."""
    try:
        await request.app.state.redis.ping()
    except Exception as exc:
        # One concise line per probe cycle; full traceback only at DEBUG.
        logger.warning("readiness check failed: %s", exc)
        logger.debug("readiness check failed", exc_info=True)
        return JSONResponse(content={"status": "not ready"}, status_code=503)
    return JSONResponse(content={"status": "ready"})


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    """Expose Prometheus metrics (no authentication)."""
    registry = request.app.state.metrics.registry
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


def register_exception_handlers(app: FastAPI) -> None:
    """Install the Newznab error and catch-all exception handlers."""

    @app.exception_handler(NewznabError)
    async def _newznab_error_handler(_request: Request, exc: NewznabError) -> Response:
        return Response(
            content=render_error(exc.code, exc.description),
            media_type=XML_MEDIA_TYPE,
            status_code=200,
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, _exc: Exception) -> Response:
        # Newznab clients expect HTTP 200 even for server-side failures; the
        # original exception is logged, never leaked to the caller.
        logger.exception("unhandled error while serving %s", request.url.path)
        return Response(
            content=render_error(900, "Internal error"),
            media_type=XML_MEDIA_TYPE,
            status_code=200,
        )
