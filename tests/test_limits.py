"""Tests for the Redis-backed daily limit tracker.

These tests use ``fakeredis`` only; nothing here requires a live Redis. Fixtures
live in this file because ``tests/conftest.py`` is owned by a later task.

Note: ``fakeredis`` evaluates ``EXPIREAT`` against *real* wall-clock time, not an
injected one. Tests that drive the manager with a fake clock therefore anchor
their scenario to a day a couple of days in the future so that the expiry the
manager writes is never already in the past (which would make fakeredis delete
the key). Assertions target specific keys rather than the whole keyspace, since
old window keys legitimately linger until their expiry.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakeredis import aioredis

from stateless_hydra.config import IndexerConfig
from stateless_hydra.limits import LimitKind, LimitsManager, next_reset

# A future UTC date used as "day D" by the fake-clock scenarios below.
_DAY = (datetime.now(UTC) + timedelta(days=2)).date()


def _indexer(**overrides) -> IndexerConfig:
    base = {
        "name": "ix",
        "host": "https://api.example.invalid",
        "api_key_ref": "ix",
    }
    base.update(overrides)
    return IndexerConfig(**base)


def _at(day_offset: int, hour: int, minute: int, tz=UTC) -> datetime:
    day = _DAY + timedelta(days=day_offset)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def _key(day: date, *, name: str = "ix", kind: str = "api") -> str:
    return f"stateless_hydra:limits:{name}:{kind}:{day.isoformat()}"


def _manager(redis, indexers, now):
    return LimitsManager(redis, indexers, clock=lambda: now)


async def _exists(redis, key: str) -> bool:
    return bool(await redis.exists(key))


async def _only_key(redis) -> str:
    keys = [key.decode() for key in await redis.keys("*")]
    assert len(keys) == 1, keys
    return keys[0]


class _Clock:
    """Mutable tz-aware clock for tests that cross reset windows."""

    def __init__(self, moment: datetime) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now


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
    manager = _manager(redis, {indexer.name: indexer}, _at(0, 10, 0))

    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await manager.consume(indexer.name, LimitKind.API) is False

    status = await manager.status(indexer.name, LimitKind.API)
    assert status.allowed is False
    assert status.remaining == 0


async def test_over_limit_consume_does_not_increment(redis):
    indexer = _indexer(api_hit_limit=1)
    manager = _manager(redis, {indexer.name: indexer}, _at(0, 10, 0))

    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await manager.consume(indexer.name, LimitKind.API) is False
    assert await manager.consume(indexer.name, LimitKind.API) is False

    assert int(await redis.get(_key(_DAY))) == 1


async def test_unlimited_always_allowed_and_counts_usage(redis):
    indexer = _indexer(api_hit_limit=0)
    manager = _manager(redis, {indexer.name: indexer}, _at(0, 10, 0))

    status = await manager.status(indexer.name, LimitKind.API)
    assert status.allowed is True
    assert status.remaining is None

    for _ in range(3):
        assert await manager.consume(indexer.name, LimitKind.API) is True

    assert int(await redis.get(_key(_DAY))) == 3


async def test_remaining_decreases_as_consumed(redis):
    indexer = _indexer(api_hit_limit=3)
    manager = _manager(redis, {indexer.name: indexer}, _at(0, 10, 0))

    assert (await manager.status(indexer.name, LimitKind.API)).remaining == 3
    await manager.consume(indexer.name, LimitKind.API)
    assert (await manager.status(indexer.name, LimitKind.API)).remaining == 2


async def test_kind_separation(redis):
    indexer = _indexer(api_hit_limit=1, nzb_pull_limit=1)
    manager = _manager(redis, {indexer.name: indexer}, _at(0, 10, 0))

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
    manager = _manager(redis, {}, _at(0, 10, 0))

    with pytest.raises(ValueError, match="unknown indexer"):
        await manager.consume("nope", LimitKind.API)
    with pytest.raises(ValueError, match="unknown indexer"):
        await manager.status("nope", LimitKind.API)


async def test_unknown_kind_raises(redis):
    indexer = _indexer()
    manager = _manager(redis, {indexer.name: indexer}, _at(0, 10, 0))

    with pytest.raises(ValueError, match="unknown limit kind"):
        await manager.status(indexer.name, "bogus")
    with pytest.raises(ValueError, match="unknown limit kind"):
        await manager.consume(indexer.name, "bogus")


async def test_naive_clock_is_rejected(redis):
    indexer = _indexer()
    manager = LimitsManager(
        redis,
        {indexer.name: indexer},
        clock=lambda: datetime(2026, 10, 4, 10, 0),  # naive on purpose
    )

    with pytest.raises(ValueError, match="tz-aware"):
        await manager.status(indexer.name, LimitKind.API)
    with pytest.raises(ValueError, match="tz-aware"):
        await manager.consume(indexer.name, LimitKind.API)


# --- reset-window scoping (regression for the non-midnight bypass) -----------


async def test_reset_window_prevents_bypass_at_non_midnight_reset(redis):
    indexer = _indexer(api_hit_limit=1, reset_time="06:00", reset_timezone="UTC")
    clock = _Clock(_at(0, 7, 0))
    manager = LimitsManager(redis, {indexer.name: indexer}, clock=clock)

    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await _exists(redis, _key(_DAY))

    # Midnight rollover stays inside the same 06:00-anchored window.
    clock.now = _at(1, 0, 30)
    assert await manager.consume(indexer.name, LimitKind.API) is False
    status = await manager.status(indexer.name, LimitKind.API)
    assert status.allowed is False
    assert status.remaining == 0
    assert await _exists(redis, _key(_DAY))
    assert not await _exists(redis, _key(_DAY + timedelta(days=1)))

    # Crossing the 06:00 reset opens a fresh window.
    clock.now = _at(1, 6, 30)
    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert int(await redis.get(_key(_DAY + timedelta(days=1)))) == 1


async def test_reset_window_spans_midnight_for_late_reset(redis):
    indexer = _indexer(api_hit_limit=1, reset_time="23:00", reset_timezone="UTC")
    clock = _Clock(_at(0, 23, 30))
    manager = LimitsManager(redis, {indexer.name: indexer}, clock=clock)

    assert await manager.consume(indexer.name, LimitKind.API) is True
    # 00:30 the next day is still inside the window that started at D 23:00.
    clock.now = _at(1, 0, 30)
    assert await manager.consume(indexer.name, LimitKind.API) is False
    assert await _exists(redis, _key(_DAY))
    # Reaching the next reset starts a new window.
    clock.now = _at(1, 23, 0)
    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert int(await redis.get(_key(_DAY + timedelta(days=1)))) == 1


async def test_reset_window_key_is_not_calendar_day(redis):
    # reset 23:59: just before the reset the key is the previous day's window;
    # just after it the key is the current reset's day, never the calendar day.
    indexer = _indexer(api_hit_limit=5, reset_time="23:59", reset_timezone="UTC")
    clock = _Clock(_at(0, 23, 58))
    manager = LimitsManager(redis, {indexer.name: indexer}, clock=clock)

    await manager.consume(indexer.name, LimitKind.API)
    assert await _exists(redis, _key(_DAY - timedelta(days=1)))

    clock.now = _at(1, 0, 1)  # calendar date rolled to D+1
    await manager.consume(indexer.name, LimitKind.API)
    assert await _exists(redis, _key(_DAY))
    assert not await _exists(redis, _key(_DAY + timedelta(days=1)))


async def test_key_changes_across_reset_instant(redis):
    indexer = _indexer(api_hit_limit=1, reset_time="06:00", reset_timezone="UTC")
    clock = _Clock(_at(0, 5, 59))
    manager = LimitsManager(redis, {indexer.name: indexer}, clock=clock)

    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await _exists(redis, _key(_DAY - timedelta(days=1)))

    clock.now = _at(0, 6, 1)
    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert int(await redis.get(_key(_DAY))) == 1


# --- timezone / DST ----------------------------------------------------------


async def test_key_date_uses_indexer_timezone(redis):
    # Same UTC instant: Kiritimati (UTC+14) is already tomorrow, UTC is today.
    kiri = _indexer(name="kiri", reset_timezone="Pacific/Kiritimati")
    utc = _indexer(name="utc", reset_timezone="UTC")
    manager = _manager(redis, {kiri.name: kiri, utc.name: utc}, _at(0, 20, 0))

    await manager.consume("kiri", LimitKind.API)
    await manager.consume("utc", LimitKind.API)

    assert await _exists(redis, _key(_DAY + timedelta(days=1), name="kiri"))
    assert await _exists(redis, _key(_DAY, name="utc"))


def _next_spring_forward(tz: ZoneInfo, start: date) -> date:
    """Return the first spring-forward date on or after ``start`` for ``tz``."""
    prev = start
    prev_noon = datetime(prev.year, prev.month, prev.day, 12, tzinfo=tz)
    for _ in range(400):
        cur = prev + timedelta(days=1)
        cur_noon = datetime(cur.year, cur.month, cur.day, 12, tzinfo=tz)
        if cur_noon.utcoffset() > prev_noon.utcoffset():
            return cur
        prev, prev_noon = cur, cur_noon
    raise AssertionError("no spring-forward transition found")


async def test_spring_forward_nonexistent_reset_time(redis):
    # On the spring-forward day a 02:30 wall time does not exist. ZoneInfo
    # resolves it deterministically (pre-transition offset), so this must not
    # crash and must produce a stable window key.
    berlin = ZoneInfo("Europe/Berlin")
    dst_date = _next_spring_forward(berlin, _DAY)
    indexer = _indexer(api_hit_limit=1, reset_time="02:30", reset_timezone="Europe/Berlin")
    now = datetime(dst_date.year, dst_date.month, dst_date.day, 12, 0, tzinfo=UTC)
    manager = _manager(redis, {indexer.name: indexer}, now)

    assert await manager.consume(indexer.name, LimitKind.API) is True
    assert await _exists(redis, _key(dst_date))

    status = await manager.status(indexer.name, LimitKind.API)
    assert status.allowed is False
    # The next (valid) reset is the following day's 02:30 Berlin wall time.
    nxt = dst_date + timedelta(days=1)
    assert status.reset_at == datetime(nxt.year, nxt.month, nxt.day, 2, 30, tzinfo=berlin)


# --- expiry ------------------------------------------------------------------


async def test_expiry_set_to_next_reset(redis):
    now = datetime.now(UTC)
    indexer = _indexer(api_hit_limit=5, reset_time="23:59", reset_timezone="UTC")
    manager = _manager(redis, {indexer.name: indexer}, now)

    await manager.consume(indexer.name, LimitKind.API)

    key = await _only_key(redis)
    expected = int(next_reset(indexer, now).timestamp()) - int(now.timestamp())
    ttl = await redis.ttl(key)

    assert abs(ttl - expected) <= 5


async def test_repeated_consume_keeps_same_expiry_deadline(redis):
    now = datetime.now(UTC)
    indexer = _indexer(api_hit_limit=5, reset_time="23:59", reset_timezone="UTC")
    manager = _manager(redis, {indexer.name: indexer}, now)

    await manager.consume(indexer.name, LimitKind.API)
    key = await _only_key(redis)
    first_pttl = await redis.pttl(key)

    await manager.consume(indexer.name, LimitKind.API)
    second_pttl = await redis.pttl(key)

    # Same clock -> same next-reset epoch -> the two deadlines differ only by the
    # real time elapsed between the calls.
    assert abs(first_pttl - second_pttl) <= 2_000
    assert int(await redis.get(key)) == 2


async def test_unlimited_key_still_gets_ttl(redis):
    now = datetime.now(UTC)
    indexer = _indexer(api_hit_limit=0, reset_time="23:59", reset_timezone="UTC")
    manager = _manager(redis, {indexer.name: indexer}, now)

    assert await manager.consume(indexer.name, LimitKind.API) is True

    key = await _only_key(redis)
    assert await redis.ttl(key) > 0
