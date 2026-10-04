"""Application factory for stateless-hydra.

:func:`create_app` wires the configuration layer, the Redis-backed cache and
limit tracker, the per-indexer HTTP clients and the Newznab API routes into a
single FastAPI application. It performs no network I/O at startup: the Redis
connection is created lazily by the client library and the upstream indexer
clients are only used while handling requests.

``uvicorn`` is pointed at ``stateless_hydra.main:app``; see the module-level
``__getattr__`` below for why that attribute is built lazily.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime

import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from prometheus_client import CollectorRegistry

from .api.routes import register_exception_handlers, router
from .cache import QueryCache
from .config import (
    AppSettings,
    IndexerConfig,
    load_api_keys,
    load_indexers,
    load_settings,
    resolve_indexer_key,
    setup_logging,
)
from .exceptions import ConfigError
from .indexer_client import IndexerClient
from .limits import LimitsManager
from .metrics import Metrics


def create_app(
    settings: AppSettings | None = None,
    *,
    redis_client: redis_asyncio.Redis | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build a configured :class:`~fastapi.FastAPI` application.

    ``settings`` defaults to :func:`~stateless_hydra.config.load_settings`.
    ``redis_client`` lets a caller (notably tests) inject an externally owned
    Redis connection; when omitted the app creates one from ``settings.redis_url``
    and owns it (closing it on shutdown). ``clock`` is forwarded to
    :class:`~stateless_hydra.limits.LimitsManager` for deterministic limit
    windows in tests.
    """
    settings = settings or load_settings(None)
    setup_logging(settings)

    indexer_list = load_indexers(settings.indexers_file)
    api_keys = load_api_keys(settings.api_keys_file)
    if not api_keys.hydra_api_keys:
        raise ConfigError("at least one hydraApiKeys entry is required (client API keys)")

    indexers: dict[str, IndexerConfig] = {indexer.name: indexer for indexer in indexer_list}
    clients: dict[str, IndexerClient] = {
        indexer.name: IndexerClient(
            indexer,
            resolve_indexer_key(indexer, api_keys),
            settings.user_agent,
            settings.global_proxy_url,
        )
        for indexer in indexer_list
    }

    owns_redis = redis_client is None
    redis = redis_client if redis_client is not None else redis_asyncio.from_url(settings.redis_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Nothing to do at startup: connections are lazy. On shutdown, close
        # the upstream clients and only the Redis connection this app created.
        yield
        for client in clients.values():
            await client.close()
        if owns_redis:
            await redis.aclose()

    app = FastAPI(title="stateless-hydra", lifespan=lifespan)
    app.state.settings = settings
    app.state.redis = redis
    app.state.limits = LimitsManager(redis, indexers, clock)
    app.state.cache = QueryCache(redis, settings.cache_ttl_seconds)
    app.state.metrics = Metrics(CollectorRegistry())
    app.state.indexers = indexers
    app.state.clients = clients
    app.state.hydra_api_keys = api_keys.hydra_api_keys

    app.include_router(router)
    register_exception_handlers(app)
    return app


def __getattr__(name: str) -> object:
    """Lazily build the ASGI application for ``stateless_hydra.main:app``.

    Building eagerly at import time would require the runtime config files to be
    present, which would break ``from stateless_hydra.main import create_app``
    in tests and tooling. The first access to ``app`` builds and caches it, so
    misconfiguration still fails fast with :class:`ConfigError`.
    """
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
