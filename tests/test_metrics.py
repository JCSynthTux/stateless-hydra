"""Tests for the Prometheus metrics wrapper."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from stateless_hydra.metrics import Metrics


def test_two_instances_use_independent_registries():
    first = Metrics(CollectorRegistry())
    second = Metrics(CollectorRegistry())

    first.inc_search("search")
    second.inc_search("tvsearch")

    assert first.registry is not second.registry


def test_counters_and_labels_are_exported():
    registry = CollectorRegistry()
    metrics = Metrics(registry)

    metrics.inc_search("search")
    metrics.observe_search_duration("search", 0.25)
    metrics.inc_api_hit("nzbgeek")
    metrics.inc_nzb_pull("nzbgeek")
    metrics.inc_error("slug")
    metrics.inc_limit_reached("nzbgeek", "api")
    metrics.inc_cache_hit("nzbgeek")
    metrics.inc_cache_miss("slug")
    metrics.set_limit_remaining("nzbgeek", "api", 3)

    text = generate_latest(registry).decode("utf-8")

    assert "stateless_hydra_search_requests_total" in text
    assert 'stateless_hydra_search_requests_total{function="search"}' in text
    assert "stateless_hydra_search_duration_seconds" in text
    assert 'stateless_hydra_indexer_api_hits_total{indexer="nzbgeek"}' in text
    assert 'stateless_hydra_indexer_limit_reached_total{indexer="nzbgeek",kind="api"}' in text
    assert 'stateless_hydra_cache_hits_total{indexer="nzbgeek"}' in text
    assert 'stateless_hydra_limit_remaining{indexer="nzbgeek",kind="api"} 3.0' in text
