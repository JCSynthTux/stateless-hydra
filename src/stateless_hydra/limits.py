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

Two kinds of budget are tracked:

* **Per-indexer** budgets (``api_hit_limit`` / ``nzb_pull_limit``) are keyed by
  indexer and reset window.
* **Per-client** envelopes (``per_client_api_hit_limit`` /
  ``per_client_nzb_pull_limit``) are keyed by a SHA-256 digest of the presented
  client API key and the UTC calendar date. They reset at UTC midnight, bound
  how much of the shared per-indexer budget one client can consume, and never
  store the raw key. A client envelope of ``0`` is unlimited and writes nothing.

Consumption is atomic in the direction that matters: the counter is incremented
first and compared afterwards, so it can never *end* a window above its limit. A
rejected caller transiently raises the raw counter until its own ``DECR`` lands,
so the counter can momentarily exceed the limit by the number of in-flight
rejected callers and always settles back to the limit. See
:meth:`LimitsManager._consume_key` for the details.

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

import hashlib
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
# Per-client envelope keys: stateless_hydra:clientlimits:<sha256(apikey)>:<kind>:<date>
_CLIENT_KEY_PREFIX = "stateless_hydra:clientlimits"


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

    @staticmethod
    def _client_key(client: str, kind: LimitKind, now: datetime) -> str:
        """Redis key for one client's envelope on the current UTC date.

        The raw API key is never part of the key: only its SHA-256 digest is
        used, so neither a Redis dump nor a ``KEYS`` listing can leak a client
        secret. The date suffix scopes the envelope to a UTC calendar day.
        """
        digest = hashlib.sha256(client.encode()).hexdigest()
        day = now.astimezone(UTC).date().isoformat()
        return f"{_CLIENT_KEY_PREFIX}:{digest}:{kind.value}:{day}"

    async def _consume_key(self, key: str, limit: int, reset_at: datetime) -> bool:
        """``INCR``-then-compare one unit against ``key`` and report permission.

        The counter is incremented first, so concurrent callers cannot all
        observe the same pre-increment value: the stored count never *ends* a
        window above ``limit``. A rejected call rolls its own increment back
        with ``DECR``, but until that rollback lands the raw counter is
        transiently ``limit + k``, where ``k`` is the number of in-flight
        rejected callers. No extra hit is admitted, and the counter always
        settles back to ``limit``.

        ``limit == 0`` is unlimited: the call is always allowed but still
        counted. ``EXPIREAT`` is (re)written on every consume (best-effort,
        because a Redis hiccup must not fail the request) so a counter can
        never outlive its window.
        """
        new_value = await self._redis.incr(key)
        try:
            await self._redis.expireat(key, int(reset_at.timestamp()))
        except Exception:
            logger.warning("failed to set expiry on limit key %s", key, exc_info=True)
        if limit != 0 and new_value > limit:
            await self._redis.decr(key)
            return False
        return True

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
        """Record one unit of an indexer's usage and report whether it was allowed.

        A call that would exceed the limit is rejected and its increment rolled
        back, so the stored count never ends a window above the limit (see
        :meth:`_consume_key` for the transient overshoot while rejected callers'
        rollbacks are in flight). Unlimited indexers
        (``limit == 0``) are always allowed while still counting usage for
        metrics.

        The counter key is scoped to the current reset window (see the module
        docstring), so crossing midnight does not start a new budget; only
        reaching the indexer's reset time does. After every consume the key's
        expiry is (re)set to the next reset instant via ``EXPIREAT``; this is
        idempotent and self-heals any missing expiry.
        """
        indexer = self._indexer(indexer_name)
        limit = self._limit(indexer, kind)
        now = self._now()
        return await self._consume_key(
            self._key(indexer, kind, now), limit, next_reset(indexer, now)
        )

    async def consume_for_client(self, client: str, kind: LimitKind, limit: int) -> bool:
        """Record one unit of a client's daily envelope and report whether allowed.

        The envelope caps how much of the *shared* per-indexer budget a single
        authenticated client may consume per UTC day, so one client can no
        longer exhaust a shared budget and starve every peer until the
        indexer's reset. Its key is
        ``stateless_hydra:clientlimits:<sha256(client)>:<kind>:<UTC date>`` and
        it expires at the next UTC midnight; the raw client key is never stored.

        ``limit == 0`` disables the envelope: the call returns ``True`` without
        reading or writing Redis, so behaviour is unchanged when unconfigured.
        """
        if limit < 0:
            raise ValueError("per-client limit must be >= 0")
        if limit == 0:
            return True

        now = self._now()
        day = now.astimezone(UTC).date()
        reset_at = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=UTC)
        return await self._consume_key(self._client_key(client, kind, now), limit, reset_at)

    async def refund_for_client(self, client: str, kind: LimitKind, limit: int) -> None:
        """Return one unit to a client's envelope after a later check denies.

        The client envelope is charged *before* the indexer's shared budget, so
        a request the indexer budget then refuses produced no upstream API hit
        and must not cost the client quota: callers refund the unit here. The
        counter is ``DECR``-ed with a floor of zero, so an already-empty or
        missing envelope stays at zero (and no new key is created, which would
        live on without the envelope's expiry).

        ``limit == 0`` disables the envelope, so there is nothing to refund and
        Redis is never touched. TTLs are left to :meth:`consume_for_client`:
        ``DECR`` preserves the key's expiry.
        """
        if limit < 0:
            raise ValueError("per-client limit must be >= 0")
        if limit == 0:
            return

        key = self._client_key(client, kind, self._now())
        # Reading first keeps a refund from *creating* the key: ``DECR`` on a
        # missing key would store -1 (with no expiry) before the clamp below.
        current = await self._redis.get(key)
        if current is None or int(current) <= 0:
            return
        if await self._redis.decr(key) < 0:
            # Two refunds raced past the read above; clamp back to the floor.
            await self._redis.incr(key)
