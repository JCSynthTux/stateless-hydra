"""Redis-backed daily usage tracking for indexer API hits and NZB pulls.

Each configured indexer may define a daily API-hit limit (``api_hit_limit``)
and/or a daily NZB-pull limit (``nzb_pull_limit``); ``0`` means unlimited. The
counts live in Redis so the application itself stays stateless.

Counters are scoped to the *reset window*, not the calendar day. A window runs
from one reset instant to the next, and the Redis key suffix is the date (in the
indexer's configured ``reset_timezone``) on which the window started. The window
start is the most recent reset instant at or before the current time. This
prevents a limit from being bypassed when the calendar date rolls over before the
indexer's non-midnight reset time is reached.

Keys additionally expire at the *next* reset instant, and the expiry is written
after every successful consume (not only when the counter is first created), so
the operation is self-healing and a counter can never outlive its window.

Daylight-saving transitions: wall-clock reset times are resolved with
:class:`zoneinfo.ZoneInfo` using its stdlib defaults (``fold=0``). A nonexistent
local time (the spring-forward gap) resolves to the instant implied by the
pre-transition offset, and an ambiguous local time (the fall-back overlap)
resolves to the first occurrence. Such a reset still yields a deterministic
window and key.

The :class:`Redis` client is created and owned by the application; this module
only issues commands against it and never creates or closes connections.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from .config import IndexerConfig

logger = logging.getLogger(__name__)

_KEY_PREFIX = "stateless_hydra:limits"


class LimitKind(StrEnum):
    """Which per-indexer daily budget a call refers to."""

    API = "api"
    NZB = "nzb"


# Explicit kind -> IndexerConfig attribute mapping (no implicit fallback).
_LIMIT_FIELDS: dict[LimitKind, str] = {
    LimitKind.API: "api_hit_limit",
    LimitKind.NZB: "nzb_pull_limit",
}


@dataclass(frozen=True)
class LimitStatus:
    """Snapshot of one indexer budget at a point in time."""

    allowed: bool
    remaining: int | None  # None when the limit is unlimited (0)
    reset_at: datetime  # tz-aware instant of the next reset


def _parse_reset_time(value: str) -> time:
    """Parse a validated ``HH:MM`` reset time into a :class:`datetime.time`."""
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _local_reset(indexer: IndexerConfig, now: datetime, day_offset: int = 0) -> datetime:
    """Reset instant on the local date ``day_offset`` days from ``now``'s date."""
    tz = ZoneInfo(indexer.reset_timezone)
    local_date = now.astimezone(tz).date() + timedelta(days=day_offset)
    return datetime.combine(local_date, _parse_reset_time(indexer.reset_time), tzinfo=tz)


def _window_start(indexer: IndexerConfig, now: datetime) -> datetime:
    """Most recent reset instant at or before ``now`` (the current window start)."""
    today_reset = _local_reset(indexer, now)
    if today_reset <= now:
        return today_reset
    return _local_reset(indexer, now, day_offset=-1)


def next_reset(indexer: IndexerConfig, now: datetime | None = None) -> datetime:
    """Return the next reset instant for ``indexer``.

    The reset happens at ``reset_time`` (``HH:MM``) in the indexer's
    ``reset_timezone``. That instant today is used when it is strictly in the
    future; otherwise the reset is tomorrow. The result is tz-aware.
    """
    if now is None:
        now = datetime.now(UTC)

    today_reset = _local_reset(indexer, now)
    if today_reset > now:
        return today_reset
    return _local_reset(indexer, now, day_offset=1)


class LimitsManager:
    """Track and enforce per-indexer daily limits in Redis.

    The injected ``clock`` (defaulting to :func:`datetime.now` in UTC) exists so
    the reset window and key dates can be driven deterministically in tests; it
    must return a tz-aware datetime.
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

    def _now(self) -> datetime:
        """Read the clock once and require a tz-aware value."""
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a tz-aware datetime")
        return now

    def _indexer(self, indexer_name: str) -> IndexerConfig:
        try:
            return self._indexers[indexer_name]
        except KeyError as exc:
            raise ValueError(f"unknown indexer: {indexer_name!r}") from exc

    def _limit(self, indexer: IndexerConfig, kind: LimitKind) -> int:
        try:
            field = _LIMIT_FIELDS[kind]
        except KeyError as exc:
            raise ValueError(f"unknown limit kind: {kind!r}") from exc
        return getattr(indexer, field)

    def _key(self, indexer: IndexerConfig, kind: LimitKind, now: datetime) -> str:
        window_date = _window_start(indexer, now).date().isoformat()
        return f"{_KEY_PREFIX}:{indexer.name}:{kind.value}:{window_date}"

    async def status(self, indexer_name: str, kind: LimitKind) -> LimitStatus:
        """Return the current budget status without consuming anything."""
        indexer = self._indexer(indexer_name)
        limit = self._limit(indexer, kind)
        now = self._now()
        reset_at = next_reset(indexer, now)

        if limit == 0:
            # Nothing is consumed, so there is no need to touch Redis at all.
            return LimitStatus(allowed=True, remaining=None, reset_at=reset_at)

        key = self._key(indexer, kind, now)
        # A corrupt (non-numeric) counter value raises here on purpose: that is
        # an operational error worth surfacing rather than silently ignoring.
        used = int(await self._redis.get(key) or 0)
        remaining = max(0, limit - used)
        return LimitStatus(allowed=used < limit, remaining=remaining, reset_at=reset_at)

    async def consume(self, indexer_name: str, kind: LimitKind) -> bool:
        """Record one unit of usage and report whether it was allowed.

        Calls are allowed while ``used < limit`` (checked *before* incrementing),
        so a request that would exceed the limit is rejected without being
        counted. Unlimited indexers (``limit == 0``) are always allowed while
        still counting usage for metrics.

        The counter key is scoped to the current reset window (see the module
        docstring), so crossing midnight does not start a new budget; only
        reaching the indexer's reset time does. After every successful consume
        the key's expiry is (re)set to the next reset instant via ``EXPIREAT``;
        this is idempotent and self-heals any missing expiry.

        Race note: the read-then-``INCR`` sequence is not atomic, so two
        concurrent calls at the limit boundary can each observe ``used < limit``
        and both increment, allowing one extra hit. This is benign for a
        stateless proxy: the worst case is a single extra request per reset
        window, and the repeated ``EXPIREAT`` keeps the key from outliving it.
        """
        indexer = self._indexer(indexer_name)
        limit = self._limit(indexer, kind)
        now = self._now()
        key = self._key(indexer, kind, now)

        # A corrupt (non-numeric) counter value raises here on purpose: that is
        # an operational error worth surfacing rather than silently ignoring.
        used = int(await self._redis.get(key) or 0)
        if limit != 0 and used >= limit:
            return False

        await self._redis.incr(key)

        reset_at = next_reset(indexer, now)
        try:
            await self._redis.expireat(key, int(reset_at.timestamp()))
        except Exception:
            # Expiry is best-effort: a Redis hiccup must not fail the request.
            logger.warning("failed to set expiry on limit key %s", key, exc_info=True)
        return True
