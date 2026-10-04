"""Prometheus metrics for stateless-hydra.

A :class:`Metrics` instance owns a :class:`~prometheus_client.CollectorRegistry`
and registers the application's counters, histogram and gauge against it. A
fresh registry must be created per application instance (see
:func:`stateless_hydra.main.create_app`) so that building more than one app in
the same process -- as the test suite does -- never triggers a duplicate
collector registration error.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

_PREFIX = "stateless_hydra_"


class Metrics:
    """Typed accessors for the application's Prometheus instruments."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.registry = registry

        self.search_requests_total = Counter(
            f"{_PREFIX}search_requests_total",
            "Search requests handled, by Newznab function.",
            ["function"],
            registry=registry,
        )
        self.search_duration_seconds = Histogram(
            f"{_PREFIX}search_duration_seconds",
            "End-to-end search latency, by Newznab function.",
            ["function"],
            registry=registry,
        )
        self.indexer_api_hits_total = Counter(
            f"{_PREFIX}indexer_api_hits_total",
            "Search requests sent to each indexer.",
            ["indexer"],
            registry=registry,
        )
        self.indexer_nzb_pulls_total = Counter(
            f"{_PREFIX}indexer_nzb_pulls_total",
            "NZB downloads served from each indexer.",
            ["indexer"],
            registry=registry,
        )
        self.indexer_errors_total = Counter(
            f"{_PREFIX}indexer_errors_total",
            "Upstream errors per indexer.",
            ["indexer"],
            registry=registry,
        )
        self.indexer_limit_reached_total = Counter(
            f"{_PREFIX}indexer_limit_reached_total",
            "Times a per-indexer budget was hit.",
            ["indexer", "kind"],
            registry=registry,
        )
        self.cache_hits_total = Counter(
            f"{_PREFIX}cache_hits_total",
            "Cache hits per indexer.",
            ["indexer"],
            registry=registry,
        )
        self.cache_misses_total = Counter(
            f"{_PREFIX}cache_misses_total",
            "Cache misses per indexer.",
            ["indexer"],
            registry=registry,
        )
        self.limit_remaining = Gauge(
            f"{_PREFIX}limit_remaining",
            "Remaining budget for the current window, per indexer and kind.",
            ["indexer", "kind"],
            registry=registry,
        )

    def inc_search(self, function: str) -> None:
        """Count one handled search request for ``function``."""
        self.search_requests_total.labels(function=function).inc()

    def observe_search_duration(self, function: str, seconds: float) -> None:
        """Record the duration of one search request for ``function``."""
        self.search_duration_seconds.labels(function=function).observe(seconds)

    def inc_api_hit(self, indexer: str) -> None:
        """Count one API hit sent to ``indexer``."""
        self.indexer_api_hits_total.labels(indexer=indexer).inc()

    def inc_nzb_pull(self, indexer: str) -> None:
        """Count one NZB pull served from ``indexer``."""
        self.indexer_nzb_pulls_total.labels(indexer=indexer).inc()

    def inc_error(self, indexer: str) -> None:
        """Count one upstream error for ``indexer``."""
        self.indexer_errors_total.labels(indexer=indexer).inc()

    def inc_limit_reached(self, indexer: str, kind: str) -> None:
        """Count one time a per-indexer budget was hit."""
        self.indexer_limit_reached_total.labels(indexer=indexer, kind=kind).inc()

    def inc_cache_hit(self, indexer: str) -> None:
        """Count one cache hit for ``indexer``."""
        self.cache_hits_total.labels(indexer=indexer).inc()

    def inc_cache_miss(self, indexer: str) -> None:
        """Count one cache miss for ``indexer``."""
        self.cache_misses_total.labels(indexer=indexer).inc()

    def set_limit_remaining(self, indexer: str, kind: str, value: int) -> None:
        """Set the remaining budget gauge for ``indexer`` and ``kind``."""
        self.limit_remaining.labels(indexer=indexer, kind=kind).set(value)
