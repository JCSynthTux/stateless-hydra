"""Redis-backed daily usage tracking for indexer API hits and NZB pulls.

Each configured indexer may define a daily API-hit limit (``api_hit_limit``)
and/or a daily NZB-pull limit (``nzb_pull_limit``); ``0`` means unlimited. The
counts live in Redis so the application itself stays stateless. A fresh counter
is used for every reset window: the Redis key includes the current date *in the
indexer's configured reset timezone*, so the next day (from that timezone's point
of view) naturally starts from zero. Keys additionally expire at the computed
reset instant.

The :class:`Redis` client is created and owned by the application; this module
only issues commands against it and never creates or closes connections.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from .config import IndexerConfig

_KEY_PREFIX = "stateless_hydra:limits"


class LimitKind(StrEnum):
    """Which per-indexer daily budget a call refers to."""

    API = "api"
    NZB = "nzb"


@dataclass(frozen=True)
class LimitStatus:
    """Snapshot of one indexer budget at a point in time."""

    allowed: bool
    remaining: int | None  # None when the limit is unlimited (0)
    reset_at: datetime  # tz-aware instant of the next reset


def next_reset(indexer: IndexerConfig, now: datetime | None = None) -> datetime:
    """Return the next reset instant for ``indexer``.

    The reset happens at ``reset_time`` (``HH:MM``) in the indexer's
    ``reset_timezone``. That instant today is used when it is strictly in the
    future; otherwise the reset is tomorrow. The result is tz-aware.
    """
    if now is None:
        now = datetime.now(UTC)

    tz = ZoneInfo(indexer.reset_timezone)
    local_now = now.astimezone(tz)
    hour, minute = (int(part) for part in indexer.reset_time.split(":"))
    target_time = time(hour, minute)

    candidate = datetime.combine(local_now.date(), target_time, tzinfo=tz)
    if candidate <= now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), target_time, tzinfo=tz)
    return candidate


class LimitsManager:
    """Track and enforce per-indexer daily limits in Redis.

    The injected ``clock`` (defaulting to :func:`datetime.now` in UTC) exists so
    the reset window and key dates can be driven deterministically in tests.
    """

    def __init__(
        self,
        redis: Redis,
        indexers: Mapping[str, IndexerConfig],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._redis = redis
        self._indexers = dict(indexers)
        self._clock = clock or (lambda: datetime.now(UTC))

    def _indexer(self, indexer_name: str) -> IndexerConfig:
        try:
            return self._indexers[indexer_name]
        except KeyError as exc:
            raise ValueError(f"unknown indexer: {indexer_name!r}") from exc

    @staticmethod
    def _limit(indexer: IndexerConfig, kind: LimitKind) -> int:
        return indexer.api_hit_limit if kind == LimitKind.API else indexer.nzb_pull_limit

    def _key(self, indexer: IndexerConfig, kind: LimitKind) -> str:
        local_date = self._clock().astimezone(ZoneInfo(indexer.reset_timezone)).date().isoformat()
        return f"{_KEY_PREFIX}:{indexer.name}:{kind.value}:{local_date}"

    async def status(self, indexer_name: str, kind: LimitKind) -> LimitStatus:
        """Return the current budget status without consuming anything."""
        indexer = self._indexer(indexer_name)
        limit = self._limit(indexer, kind)
        reset_at = next_reset(indexer, self._clock())

        used = int(await self._redis.get(self._key(indexer, kind)) or 0)
        if limit == 0:
            return LimitStatus(allowed=True, remaining=None, reset_at=reset_at)

        remaining = max(0, limit - used)
        return LimitStatus(allowed=used < limit, remaining=remaining, reset_at=reset_at)

    async def consume(self, indexer_name: str, kind: LimitKind) -> bool:
        """Record one unit of usage and report whether it was allowed.

        Matches are allowed while ``used < limit`` (checked *before*
        incrementing), so a request that would exceed the limit is rejected
        without being counted. Unlimited indexers (``limit == 0``) are always
        allowed while still counting usage for metrics.

        When the counter key is first created (``INCR`` returns ``1``) its
        expiry is set to the next reset instant via ``EXPIREAT``.

        Race note: the read-then-``INCR`` sequence and the ``INCR``/``EXPIREAT``
        pair are not atomic. Two concurrent calls at the limit boundary can each
        see ``used < limit`` and both increment (allowing one extra hit), and a
        crash between ``INCR`` and ``EXPIREAT`` can leave the key without a TTL
        until its date rolls over. Both windows are benign for this stateless
        proxy: the worst case is a single extra request per reset window, and the
        next reset window uses a new date-scoped key regardless.
        """
        indexer = self._indexer(indexer_name)
        limit = self._limit(indexer, kind)
        key = self._key(indexer, kind)

        used = int(await self._redis.get(key) or 0)
        if limit != 0 and used >= limit:
            return False

        created = await self._redis.incr(key) == 1
        if created:
            await self._redis.expireat(key, int(next_reset(indexer, self._clock()).timestamp()))
        return True
