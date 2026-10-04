"""Tests for the Redis-backed daily limit tracker.

These tests use ``fakeredis`` only; nothing here requires a live Redis. Fixtures
live in this file because ``tests/conftest.py`` is owned by a later task.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakeredis import aioredis

from stateless_hydra.config import IndexerConfig
from stateless_hydra.limits import LimitKind, LimitsManager, next_reset


def _indexer(**overrides) -> IndexerConfig:
    base = {
        "name": "ix",
        "host": "https://api.example.invalid",
        "api_key_ref": "ix",
    }
    base.update(overrides)
    return IndexerConfig(**base)


def _fixed_clock(moment: datetime):
    return lambda: moment


def _manager(redis, indexers, now):
    return LimitsManager(redis, indexers, clock=_fixed_clock(now))


@pytest.fixture
def redis():
    return aioredis.FakeRedis()


# --- next_reset helper -------------------------------------------------------


def test_next_reset_uses_today_when_still_in_future():
    indexer = _indexer(reset_time="23:59", reset_timezone="UTC")
    now = datetime(2026, 10, 4, 0, 30, tzinfo=UTC)

    assert next_reset(indexer, now) == datetime(2026, 10, 4, 23, 59, tzinfo=UTC)


def test_next_reset_rolls_to_tomorrow_when_already_passed():
    indexer = _indexer(reset_time="23:00", reset_timezone="UTC")
    now = datetime(2026, 10, 4, 23, 30, tzinfo=UTC)

    assert next_reset(indexer, now) == datetime(2026, 10, 5, 23, 0, tzinfo=UTC)


def test_next_reset_equal_instant_rolls_to_tomorrow():
    indexer = _indexer(reset_time="12:00", reset_timezone="UTC")
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

    assert next_reset(indexer, now) == datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_next_reset_returns_tz_aware_berlin_time():
    indexer = _indexer(reset_time="06:30", reset_timezone="Europe/Berlin")
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)  # 12:00 Berlin, reset passed

    result = next_reset(indexer, now)

    assert result == datetime(2026, 10, 5, 6, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    assert result.utcoffset() == timedelta(hours=2)  # CEST


def test_next_reset_defaults_to_current_utc_time():
    indexer = _indexer(reset_time="00:00", reset_timezone="UTC")
    result = next_reset(indexer)

    assert result.tzinfo is not None
    assert result > datetime.now(UTC)


# --- consume -----------------------------------------------------------------


async def test_consume_allows_exactly_limit_then_blocks(redis):
    indexer = _indexer(api_hit_limit=2)
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    manager = _manager(redis, {indexer.name: indexer}, now)

    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await manager.consume(indexer.name, LimitKind.API) is False

    status = await manager.status(indexer.name, LimitKind.API)
    assert status.allowed is False
    assert status.remaining == 0


async def test_over_limit_consume_does_not_increment(redis):
    indexer = _indexer(api_hit_limit=1)
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    manager = _manager(redis, {indexer.name: indexer}, now)

    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await manager.consume(indexer.name, LimitKind.API) is False
    assert await manager.consume(indexer.name, LimitKind.API) is False

    key = f"stateless_hydra:limits:{indexer.name}:api:2026-10-04"
    assert int(await redis.get(key)) == 1


async def test_unlimited_always_allowed_and_counts_usage(redis):
    indexer = _indexer(api_hit_limit=0)
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    manager = _manager(redis, {indexer.name: indexer}, now)

    status = await manager.status(indexer.name, LimitKind.API)
    assert status.allowed is True
    assert status.remaining is None

    for _ in range(3):
        assert await manager.consume(indexer.name, LimitKind.API) is True

    key = f"stateless_hydra:limits:{indexer.name}:api:2026-10-04"
    assert int(await redis.get(key)) == 3


async def test_remaining_decreases_as_consumed(redis):
    indexer = _indexer(api_hit_limit=3)
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    manager = _manager(redis, {indexer.name: indexer}, now)

    assert (await manager.status(indexer.name, LimitKind.API)).remaining == 3
    await manager.consume(indexer.name, LimitKind.API)
    assert (await manager.status(indexer.name, LimitKind.API)).remaining == 2


async def test_kind_separation(redis):
    indexer = _indexer(api_hit_limit=1, nzb_pull_limit=1)
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    manager = _manager(redis, {indexer.name: indexer}, now)

    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await manager.consume(indexer.name, LimitKind.API) is False
    # NZB budget independently untouched.
    assert await manager.consume(indexer.name, LimitKind.NZB) is True
    assert await manager.consume(indexer.name, LimitKind.NZB) is False

    api_status = await manager.status(indexer.name, LimitKind.API)
    nzb_status = await manager.status(indexer.name, LimitKind.NZB)
    assert api_status.allowed is False
    assert nzb_status.allowed is False


async def test_unknown_indexer_raises(redis):
    manager = _manager(redis, {}, datetime(2026, 10, 4, 10, 0, tzinfo=UTC))

    with pytest.raises(ValueError, match="unknown indexer"):
        await manager.consume("nope", LimitKind.API)
    with pytest.raises(ValueError, match="unknown indexer"):
        await manager.status("nope", LimitKind.API)


# --- key date + expiry -------------------------------------------------------


async def test_key_date_uses_indexer_timezone(redis):
    # Same UTC instant: Kiritimati (UTC+14) is already tomorrow, UTC is today.
    now = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)
    kiri = _indexer(name="kiri", reset_timezone="Pacific/Kiritimati")
    utc = _indexer(name="utc", reset_timezone="UTC")
    manager = _manager(redis, {kiri.name: kiri, utc.name: utc}, now)

    await manager.consume("kiri", LimitKind.API)
    await manager.consume("utc", LimitKind.API)

    keys = {k.decode() for k in await redis.keys("*")}
    assert "stateless_hydra:limits:kiri:api:2026-10-05" in keys
    assert "stateless_hydra:limits:utc:api:2026-10-04" in keys


async def test_expiry_set_to_next_reset(redis):
    now = datetime.now(UTC)
    indexer = _indexer(api_hit_limit=5, reset_time="23:59", reset_timezone="UTC")
    manager = _manager(redis, {indexer.name: indexer}, now)

    await manager.consume(indexer.name, LimitKind.API)

    key = f"stateless_hydra:limits:{indexer.name}:api:{now.date().isoformat()}"
    expected = int(next_reset(indexer, now).timestamp()) - int(now.timestamp())
    ttl = await redis.ttl(key)

    assert abs(ttl - expected) <= 5


async def test_expiry_not_reset_on_later_increments(redis):
    now = datetime.now(UTC)
    indexer = _indexer(api_hit_limit=5, reset_time="23:59", reset_timezone="UTC")
    manager = _manager(redis, {indexer.name: indexer}, now)

    await manager.consume(indexer.name, LimitKind.API)
    key = f"stateless_hydra:limits:{indexer.name}:api:{now.date().isoformat()}"
    first_ttl = await redis.ttl(key)

    await manager.consume(indexer.name, LimitKind.API)
    assert await redis.ttl(key) <= first_ttl
