"""Redis-backed cache for upstream indexer search responses.

The cache shortens indexer API usage, which is a core part of the daily limit
story (see :mod:`stateless_hydra.limits`). It stores one Redis string per
``(indexer, normalized query)`` pair; the Redis client is created and owned by
the application, so this module never creates or closes connections.

Cache keys are the SHA-256 digest of the normalized query term tuple produced
by :func:`stateless_hydra.newznab.canonical_query`, so parameter order cannot
affect lookups. A non-positive TTL disables caching.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from redis.asyncio import Redis

_KEY_PREFIX = "stateless_hydra:cache"
_SEPARATOR = "\x1f"


def cache_key_for(params_tuple: Sequence[str]) -> str:
    """Return a stable SHA-256 hex digest for a normalized query term tuple."""
    joined = _SEPARATOR.join(params_tuple)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class QueryCache:
    """Store and retrieve upstream indexer responses in Redis.

    ``redis`` is an externally owned :class:`redis.asyncio.Redis` client; this
    class only issues commands against it. ``default_ttl`` is used whenever a
    caller passes ``ttl=None``; TTLs of zero or below disable caching.
    """

    def __init__(self, redis: Redis, default_ttl: int) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    @staticmethod
    def _key(indexer: str, key: str) -> str:
        return f"{_KEY_PREFIX}:{indexer}:{key}"

    async def get(self, indexer: str, key: str) -> str | None:
        """Return the cached response text, or ``None`` on a miss."""
        raw = await self._redis.get(self._key(indexer, key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return raw

    async def set(self, indexer: str, key: str, value: str, ttl: int | None) -> None:
        """Cache ``value`` under ``(indexer, key)`` with the effective TTL.

        ``ttl=None`` falls back to ``default_ttl``; an effective TTL of zero or
        below is a no-op (caching disabled).
        """
        effective_ttl = self._default_ttl if ttl is None else ttl
        if effective_ttl <= 0:
            return
        await self._redis.set(self._key(indexer, key), value, ex=effective_ttl)
