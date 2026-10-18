"""Regression check: hot session reads must not block the event loop.

The server is one process with one event loop. A synchronous store call
inside an ``async def`` handler stalls every other request, ``/health``
included. These tests make the agent store slow and assert that a
``/health`` probe stays fast while several ``GET /v1/sessions/{id}`` calls
are in flight.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_STORE_DELAY_S = 0.25
_CONCURRENT_GETS = 8
# Far below one blocked store call; generous for a loaded CI box.
_HEALTH_BUDGET_S = 0.2


def _slow_agent_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``SqlAlchemyAgentStore.get`` sleep like a slow DB round trip."""
    real_get = SqlAlchemyAgentStore.get

    def slow_get(self: SqlAlchemyAgentStore, agent_id: str) -> Any:
        time.sleep(_STORE_DELAY_S)
        return real_get(self, agent_id)

    monkeypatch.setattr(SqlAlchemyAgentStore, "get", slow_get)


async def _probe_health(client: httpx.AsyncClient, stop: asyncio.Event) -> list[float]:
    """Time ``GET /health`` repeatedly until *stop* is set."""
    latencies: list[float] = []
    while not stop.is_set():
        started = time.perf_counter()
        resp = await client.get("/health")
        latencies.append(time.perf_counter() - started)
        assert resp.status_code == 200
        await asyncio.sleep(0.01)
    return latencies


async def test_health_stays_fast_while_get_session_reads_a_slow_store(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N concurrent snapshot reads against a slow agent store leave /health responsive."""
    agent = await create_test_agent(client)
    session_id = agent["_session_id"]
    _slow_agent_get(monkeypatch)

    stop = asyncio.Event()
    probe = asyncio.create_task(_probe_health(client, stop))
    await asyncio.sleep(0.05)
    started = time.perf_counter()
    responses = await asyncio.gather(
        *(
            client.get(f"/v1/sessions/{session_id}?include_items=false")
            for _ in range(_CONCURRENT_GETS)
        )
    )
    elapsed = time.perf_counter() - started
    stop.set()
    latencies = await probe

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert {r.json()["id"] for r in responses} == {session_id}
    assert latencies, "the health probe never ran"
    assert max(latencies) < _HEALTH_BUDGET_S, (
        f"/health stalled behind get_session: max {max(latencies):.3f}s over "
        f"{len(latencies)} probes (get_session batch took {elapsed:.3f}s)"
    )
    # The slow reads ran in worker threads side by side, not one after another
    # on the loop.
    assert elapsed < _CONCURRENT_GETS * _STORE_DELAY_S
