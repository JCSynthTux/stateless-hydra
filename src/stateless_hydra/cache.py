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
import json
from collections.abc import Sequence

from redis.asyncio import Redis

_KEY_PREFIX = "stateless_hydra:cache"


def cache_key_for(params_tuple: Sequence[str]) -> str:
    """Return a stable SHA-256 hex digest for a normalized query term tuple.

    The tuple is serialized with JSON rather than joined with a separator
    character: a client-controlled value may contain any character, so a plain
    join lets one value imitate a tuple boundary (a single value containing
    U+001F serializes identically to two values). JSON quotes and escapes each
    string, so values can never impersonate tuple boundaries and distinct
    parameter tuples always produce distinct preimages.
    """
    preimage = json.dumps(list(params_tuple), separators=(",", ":"))
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


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
        """Return the cached response text, or ``None`` on a miss.

        Bytes are decoded with ``errors="replace"``: a non-UTF-8 value is a
        poisoned entry, not a fatal error. Raising here would escape the
        cache's corrupt-entry handling and turn every matching search into
        error 900 until the TTL expired, so invalid bytes decode to U+FFFD
        instead. The caller's parse then fails, which drops the entry and
        refetches upstream.
        """
        raw = await self._redis.get(self._key(indexer, key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
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

    async def delete(self, indexer: str, key: str) -> None:
        """Drop the cached response under ``(indexer, key)`` (best-effort)."""
        await self._redis.delete(self._key(indexer, key))
