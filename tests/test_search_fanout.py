"""Tests for the concurrent per-indexer search fan-out.

The fan-out helper is exercised directly so concurrency and ordering can be
asserted without timing sleeps: two coroutines that each wait for the other to
start can only finish when they truly overlap.
"""

from __future__ import annotations

import asyncio

from stateless_hydra.api.routes import IndexerSearchResult, _run_indexer_searches


async def test_run_indexer_searches_runs_queries_concurrently():
    """Both indexer queries must be in flight at the same time.

    Indexer ``a`` waits for ``b`` to start and vice versa, so the gather can
    only complete when both coroutines overlap. A sequential implementation
    would deadlock and fail via the timeout. The returned list must also stay
    in input order (``a`` before ``b``) regardless of completion order.
    """
    a_started = asyncio.Event()
    b_started = asyncio.Event()

    async def query(indexer: str) -> IndexerSearchResult:
        if indexer == "a":
            a_started.set()
            await b_started.wait()
            return IndexerSearchResult(error_skipped=True)
        b_started.set()
        await a_started.wait()
        return IndexerSearchResult(limit_skipped=True)

    results = await asyncio.wait_for(
        _run_indexer_searches(["a", "b"], query),
        timeout=1.0,
    )

    assert results[0].error_skipped is True
    assert results[1].limit_skipped is True
