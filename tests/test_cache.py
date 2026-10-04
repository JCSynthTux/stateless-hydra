"""Tests for the Redis-backed query cache.

Everything runs against ``fakeredis``; no live Redis is required.
"""

from __future__ import annotations

import pytest
from fakeredis import aioredis

from stateless_hydra.cache import QueryCache, cache_key_for


@pytest.fixture
def redis():
    return aioredis.FakeRedis()


async def test_set_get_roundtrip(redis):
    cache = QueryCache(redis, default_ttl=60)

    await cache.set("nzbgeek", "key", "<rss>payload</rss>", None)

    assert await cache.get("nzbgeek", "key") == "<rss>payload</rss>"


async def test_get_miss_returns_none(redis):
    cache = QueryCache(redis, default_ttl=60)

    assert await cache.get("nzbgeek", "absent") is None


async def test_stored_key_uses_documented_prefix(redis):
    cache = QueryCache(redis, default_ttl=60)

    await cache.set("nzbgeek", "abc", "value", None)

    assert await redis.get("stateless_hydra:cache:nzbgeek:abc") is not None


async def test_ttl_is_applied(redis):
    cache = QueryCache(redis, default_ttl=60)

    await cache.set("nzbgeek", "key", "value", 120)

    ttl = await redis.ttl("stateless_hydra:cache:nzbgeek:key")
    assert 118 <= ttl <= 120


async def test_ttl_none_uses_default(redis):
    cache = QueryCache(redis, default_ttl=42)

    await cache.set("nzbgeek", "key", "value", None)

    ttl = await redis.ttl("stateless_hydra:cache:nzbgeek:key")
    assert 40 <= ttl <= 42


async def test_ttl_zero_is_not_stored(redis):
    cache = QueryCache(redis, default_ttl=60)

    await cache.set("nzbgeek", "key", "value", 0)

    assert await cache.get("nzbgeek", "key") is None


async def test_ttl_negative_is_not_stored(redis):
    cache = QueryCache(redis, default_ttl=60)

    await cache.set("nzbgeek", "key", "value", -1)

    assert await cache.get("nzbgeek", "key") is None


def test_cache_key_is_deterministic():
    params = ("q=ubuntu", "t=search", "limit=10")

    assert cache_key_for(params) == cache_key_for(params)


def test_cache_key_differs_for_different_params():
    assert cache_key_for(("q=ubuntu",)) != cache_key_for(("q=debian",))
