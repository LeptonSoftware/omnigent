"""SQL round trips and phase timing for ``GET /v1/sessions/{id}``.

Every SQL statement and pool checkout the request makes on the realistic
session is counted, so a change that adds a round trip to the snapshot fails
here. Budgets are the measured counts after batching; the pre-batching counts
are in the comment above ``CASES``.
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from omnigent.server import server_timing
from tests.server.get_session_fixtures import (
    OWNER,
    READER,
    RealisticSession,
    SqlCounter,
    realistic,
    realistic_app,
    record_pending,
    sql_counter,
)

__all__ = ["realistic", "realistic_app", "sql_counter"]

_WEB = "?include_items=false&include_liveness=false"


async def _measure(session: RealisticSession, counter: SqlCounter, user: str, path: str) -> None:
    headers = {"X-Forwarded-Email": user}
    # The first request warms per-process caches (agent bundle, harness), as
    # on a live server.
    assert (await session.client.get(path, headers=headers)).status_code == 200
    counter.reset()
    counter.armed = True
    try:
        resp = await session.client.get(path, headers=headers)
    finally:
        counter.armed = False
    assert resp.status_code == 200, resp.text


# (case, user, target, query, max queries, max checkouts). Before batching the
# same cases took 19/14, 19/14, 15/10 and 23/11 (queries/checkouts).
CASES = [
    ("owner-default", OWNER, "session", "", 12, 3),
    ("reader-default", READER, "session", "", 12, 3),
    ("owner-web", OWNER, "session", _WEB, 8, 3),
    ("reader-child", READER, "child", "", 19, 3),
]


@pytest.mark.parametrize(
    ("case", "user", "target", "query", "max_queries", "max_checkouts"), CASES
)
async def test_get_session_sql_budget(
    realistic: RealisticSession,
    sql_counter: SqlCounter,
    case: str,
    user: str,
    target: str,
    query: str,
    max_queries: int,
    max_checkouts: int,
) -> None:
    """The realistic snapshot stays within its query and checkout budget."""
    session_id = realistic.session_id if target == "session" else realistic.child_id
    await _measure(realistic, sql_counter, user, f"/v1/sessions/{session_id}{query}")
    print(
        f"\n[{case}] statements={len(sql_counter.statements)} "
        f"queries={len(sql_counter.queries)} checkouts={sql_counter.checkouts}"
    )
    for statement in sql_counter.queries:
        print("   ", statement[:140])
    assert len(sql_counter.queries) <= max_queries
    assert sql_counter.checkouts <= max_checkouts


async def test_descendant_prompt_is_still_found(realistic: RealisticSession) -> None:
    """A prompt open in a sub-agent is replayed; one in an unrelated session is not."""
    record_pending(realistic.grandchild_id, "elicit_grandchild")
    resp = await realistic.client.get(
        f"/v1/sessions/{realistic.session_id}", headers={"X-Forwarded-Email": OWNER}
    )
    assert resp.status_code == 200
    events = resp.json()["pending_elicitations"]
    targets = {
        (e.get("elicitation_id"), (e.get("params") or {}).get("target_session_id")) for e in events
    }
    assert ("elicit_grandchild", realistic.grandchild_id) in targets
    assert "elicit_parent" in {e.get("elicitation_id") for e in events}
    assert "elicit_unrelated" not in {e.get("elicitation_id") for e in events}


async def test_server_timing_header_names_each_phase(realistic: RealisticSession) -> None:
    """Every snapshot response carries a Server-Timing header with its phases."""
    resp = await realistic.client.get(
        f"/v1/sessions/{realistic.session_id}", headers={"X-Forwarded-Email": OWNER}
    )
    assert resp.status_code == 200
    header = resp.headers["server-timing"]
    phases = dict(re.findall(r"([a-z_]+);dur=([0-9.]+)", header))
    for name in ("auth", "conv", "reads", "items", "liveness", "usage", "agent", "build"):
        assert name in phases, header
    assert "encode" in phases, header
    assert list(phases)[-1] == "total", header
    assert float(phases["total"]) >= float(phases["reads"])


async def test_slow_snapshot_logs_its_phases_once(
    realistic: RealisticSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A snapshot over the slow threshold logs one WARNING carrying its phases."""
    monkeypatch.setattr(server_timing, "SLOW_REQUEST_S", 0.0)
    with caplog.at_level(logging.WARNING, logger=server_timing.__name__):
        resp = await realistic.client.get(
            f"/v1/sessions/{realistic.session_id}", headers={"X-Forwarded-Email": OWNER}
        )
    assert resp.status_code == 200
    slow = [r for r in caplog.records if r.name == server_timing.__name__]
    assert len(slow) == 1
    assert "get_session" in slow[0].getMessage()
    assert "reads;dur=" in slow[0].getMessage()


async def test_get_session_latency_report(realistic: RealisticSession) -> None:
    """Latency of the default owner snapshot (informational, no budget)."""
    path = f"/v1/sessions/{realistic.session_id}"
    headers = {"X-Forwarded-Email": OWNER}
    await realistic.client.get(path, headers=headers)
    samples = []
    for _ in range(40):
        started = time.perf_counter()
        resp = await realistic.client.get(path, headers=headers)
        samples.append(time.perf_counter() - started)
        assert resp.status_code == 200
    samples.sort()
    print(f"\n[latency] p50={samples[20] * 1000:.1f}ms p90={samples[36] * 1000:.1f}ms")
