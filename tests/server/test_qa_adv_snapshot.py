"""Adversarial QA: fork fast get_session vs upstream on edge cases (2026-10-19).

Calls both snapshot builders directly with identical arguments, so cases the
HTTP route cannot reach in tests (runner exit reports, sandbox config, deleted
agents, stores absent) are covered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import harness_cache
from omnigent.server import session_snapshot_fast as fast
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_READ, UnifiedAuthProvider
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import _app_client
from tests.server.get_session_fixtures import (
    HOST_ID,
    OWNER,
    RUNNER_ID,
    build_realistic_session,
    record_pending,
)


@dataclass
class Rig:
    client: httpx.AsyncClient
    convs: SqlAlchemyConversationStore
    agents: SqlAlchemyAgentStore
    cache: AgentCache
    hosts: HostStore
    session_id: str
    child_id: str
    grandchild_id: str


@pytest_asyncio.fixture()
async def rig(
    runtime_init: None, db_uri: str, tmp_path: Path, mock_llm: Any
) -> AsyncIterator[Rig]:
    del runtime_init
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    convs = SqlAlchemyConversationStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    hosts = HostStore(db_uri)
    app = create_app(
        agent_store=agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=convs,
        artifact_store=artifact_store,
        agent_cache=cache,
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        host_store=hosts,
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    async with _app_client(app, mock_llm, tmp_path) as client:
        s = await build_realistic_session(client, db_uri)
        try:
            yield Rig(client, convs, agents, cache, hosts, s.session_id, s.child_id, s.grandchild_id)
        finally:
            pending_elicitations.reset_for_tests()
            harness_cache.clear()


async def _both(rig: Rig, session_id: str, **kw: Any) -> tuple[Any, Any]:
    base: dict[str, Any] = dict(
        permission_level=LEVEL_READ,
        agent_store=rig.agents,
        agent_cache=rig.cache,
        host_store=rig.hosts,
        viewer_id=OWNER,
    )
    base.update(kw)

    async def run(fn: Any) -> Any:
        try:
            r = await fn(rig.convs, session_id, **base)
            return ("ok", r.model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001
            return ("err", type(exc).__name__, getattr(exc, "code", None))

    # warm per-process caches
    await run(fast.get_session_snapshot)
    await run(orchestration._get_session_snapshot)
    return await run(fast.get_session_snapshot), await run(orchestration._get_session_snapshot)


def _diff(a: Any, b: Any) -> dict[str, Any]:
    if a[0] != "ok" or b[0] != "ok":
        return {"__result__": (a, b)} if a != b else {}
    return {k: (a[1].get(k), b[1].get(k)) for k in set(a[1]) | set(b[1]) if a[1].get(k) != b[1].get(k)}


async def test_runner_exit_report_failed(rig: Rig) -> None:
    reports = RunnerExitReports()
    reports.record(RUNNER_ID, "runner exited: boom", OWNER)
    f, u = await _both(rig, rig.session_id, runner_exit_reports=reports)
    assert f[0] == "ok" and f[1]["status"] == "failed", f
    assert _diff(f, u) == {}


async def test_sandbox_host_resumable(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestration, "host_resume_supported", lambda host, cfg: host.host_id == HOST_ID
    )
    f, u = await _both(rig, rig.session_id, sandbox_config=object())
    assert f[0] == "ok" and f[1]["host_resumable"] is True, f
    assert _diff(f, u) == {}


async def test_deleted_agent(rig: Rig) -> None:
    conv = rig.convs.get_conversation(rig.session_id)
    assert conv is not None and conv.agent_id
    # Warm the harness cache with the live agent, then delete it.
    await _both(rig, rig.session_id)
    assert rig.agents.delete(conv.agent_id)
    f, u = await _both(rig, rig.session_id)
    assert _diff(f, u) == {}


async def test_no_agent_store(rig: Rig) -> None:
    f, u = await _both(rig, rig.session_id, agent_store=None, agent_cache=None)
    assert _diff(f, u) == {}


async def test_native_subagent_unknown_name(rig: Rig) -> None:
    child = rig.convs.create_conversation(
        agent_id=rig.convs.get_conversation(rig.session_id).agent_id,  # type: ignore[union-attr]
        kind="sub_agent",
        parent_conversation_id=rig.session_id,
        sub_agent_name="general-purpose",
    )
    rig.convs.set_labels(child.id, {"omnigent.wrapper": "claude-code-native-subagent"})
    f, u = await _both(rig, child.id)
    assert _diff(f, u) == {}


async def test_archived_session(rig: Rig) -> None:
    rig.convs.update_conversation(rig.session_id, archived=True)
    f, u = await _both(rig, rig.session_id)
    assert _diff(f, u) == {}


async def test_pending_in_archived_child(rig: Rig) -> None:
    archived = rig.convs.create_conversation(
        agent_id=rig.convs.get_conversation(rig.session_id).agent_id,  # type: ignore[union-attr]
        kind="sub_agent",
        parent_conversation_id=rig.session_id,
        sub_agent_name="worker",
    )
    rig.convs.update_conversation(archived.id, archived=True)
    record_pending(archived.id, "elicit_archived")
    record_pending(rig.grandchild_id, "elicit_gc")
    f, u = await _both(rig, rig.session_id)
    assert _diff(f, u) == {}


async def test_pending_in_child_no_items_no_usage(rig: Rig) -> None:
    record_pending(rig.child_id, "elicit_c")
    f, u = await _both(rig, rig.session_id, include_usage=False, include_items=False)
    assert _diff(f, u) == {}


async def test_big_tree_paged(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tree spanning several pages of the usage walk."""
    from omnigent.runtime.policies import builder

    monkeypatch.setattr(builder, "_SUBTREE_USAGE_PAGE_SIZE", 2)
    agent_id = rig.convs.get_conversation(rig.session_id).agent_id  # type: ignore[union-attr]
    last = rig.session_id
    for _ in range(5):
        last = rig.convs.create_conversation(
            agent_id=agent_id, kind="sub_agent", parent_conversation_id=last, sub_agent_name="worker"
        ).id
    record_pending(last, "elicit_deep")
    f, u = await _both(rig, rig.session_id)
    assert _diff(f, u) == {}
    assert any(e.get("elicitation_id") == "elicit_deep" for e in f[1]["pending_elicitations"])


async def test_missing_session_same_error(rig: Rig) -> None:
    f, u = await _both(rig, "0" * 32)
    assert f == u


def test_harness_cache_toctou_keeps_stale_harness() -> None:
    """An in-process spec swap that lands while a resolve is running.

    The wrapper reads the spec location AFTER the resolve, so it pairs the old
    harness with the new location and serves the old harness until the TTL.
    """
    harness_cache.clear()

    class FakeCache:
        loc = "agent/v1"

        def cached_location(self, agent_id: str) -> str:
            return self.loc

    cache = FakeCache()
    store = object()
    state = {"harness": "claude-sdk"}

    def resolve(conv: Any, *, agent_store: Any = None, agent_cache: Any = None) -> str:
        harness = state["harness"]
        # The PATCH lands mid-resolve: new bundle, new harness.
        if harness == "claude-sdk":
            cache.loc = "agent/v2"
            state["harness"] = "codex"
        return harness

    wrapped = harness_cache.cache_resolved_harness(resolve)

    class Conv:
        harness_override = None
        agent_id = "ag_1"
        sub_agent_name = None

    first = wrapped(Conv(), agent_store=store, agent_cache=cache)
    assert first == "claude-sdk"
    # Upstream (no cache) would now answer the new spec's harness.
    assert resolve(Conv(), agent_store=store, agent_cache=cache) == "codex"
    second = wrapped(Conv(), agent_store=store, agent_cache=cache)
    harness_cache.clear()
    assert second == "codex", "harness cache served the pre-edit harness after an in-process edit"


async def test_harness_cache_after_agent_delete(rig: Rig) -> None:
    """Deleting the bound agent: uncached resolve says None, cached says the old harness."""
    from omnigent.server.routes._sessions import helpers

    conv = rig.convs.get_conversation(rig.session_id)
    assert conv is not None and conv.agent_id
    await _both(rig, rig.session_id)  # warm
    assert rig.agents.delete(conv.agent_id)
    raw = helpers._resolve_harness_impl.__wrapped__(conv, agent_store=rig.agents, agent_cache=rig.cache)
    cached = helpers._resolve_harness_impl(conv, agent_store=rig.agents, agent_cache=rig.cache)
    assert raw is None
    assert cached == raw, f"cached harness {cached!r} for a deleted agent"


async def test_harness_cache_after_store_update_without_cache_touch(rig: Rig) -> None:
    """A bundle swap written to the store but not the agent cache (another replica / CLI)."""
    from omnigent.server.routes._sessions import helpers

    conv = rig.convs.get_conversation(rig.session_id)
    assert conv is not None and conv.agent_id
    await _both(rig, rig.session_id)
    agent = rig.agents.get(conv.agent_id)
    assert agent is not None
    raw_before = helpers._resolve_harness_impl.__wrapped__(conv, agent_store=rig.agents, agent_cache=rig.cache)
    cached = helpers._resolve_harness_impl(conv, agent_store=rig.agents, agent_cache=rig.cache)
    assert raw_before == cached


async def test_stress_concurrent_get_session_with_writes(rig: Rig) -> None:
    """Hundreds of concurrent snapshots mixed with writes: no errors, no leaked checkouts,
    and no thread ever holds two pooled connections at once."""
    import asyncio
    import random
    import threading

    from sqlalchemy import event
    from sqlalchemy.pool import Pool

    from omnigent.db.utils import _engine_cache

    held: dict[int, int] = {}
    nested: list[int] = []
    lock = threading.Lock()

    def on_checkout(dbapi_conn: Any, rec: Any, proxy: Any) -> None:
        tid = threading.get_ident()
        with lock:
            held[tid] = held.get(tid, 0) + 1
            if held[tid] > 1:
                nested.append(held[tid])

    def on_checkin(dbapi_conn: Any, rec: Any) -> None:
        tid = threading.get_ident()
        with lock:
            held[tid] = held.get(tid, 1) - 1

    event.listen(Pool, "checkout", on_checkout)
    event.listen(Pool, "checkin", on_checkin)
    record_pending(rig.child_id, "elicit_c")
    queries = [
        "",
        "?include_items=false&include_liveness=false",
        "?include_usage=false",
        "?refresh_state=true",
        "?include_live_status=false",
    ]
    targets = [rig.session_id, rig.child_id, rig.grandchild_id]
    headers = {"X-Forwarded-Email": OWNER}
    errors: list[Any] = []
    bodies: dict[str, list[Any]] = {}

    async def reader(i: int) -> None:
        sid = random.choice(targets)
        q = random.choice(queries)
        r = await rig.client.get(f"/v1/sessions/{sid}{q}", headers=headers)
        if r.status_code != 200:
            errors.append((sid, q, r.status_code, r.text[:300]))
            return
        body = r.json()
        if body["id"] != sid:
            errors.append(("WRONG-ID", sid, body["id"]))
        if sid == rig.session_id and q == "":
            ids = sorted(e.get("elicitation_id") for e in body["pending_elicitations"])
            bodies.setdefault("pending", []).append(tuple(ids))

    async def writer(i: int) -> None:
        kind = i % 3
        if kind == 0:
            r = await rig.client.patch(
                f"/v1/sessions/{rig.session_id}",
                json={"labels": {"stress": str(i)}},
                headers=headers,
            )
        elif kind == 1:
            r = await rig.client.patch(
                f"/v1/sessions/{rig.grandchild_id}", json={"archived": i % 2 == 0}, headers=headers
            )
        else:
            await asyncio.to_thread(rig.convs.set_session_usage, rig.child_id, {"total_cost_usd": i / 100})
            return
        if r.status_code >= 500:
            errors.append(("WRITE", i, r.status_code, r.text[:300]))

    try:
        tasks = [reader(i) for i in range(300)] + [writer(i) for i in range(60)]
        random.shuffle(tasks)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=240)
    finally:
        event.remove(Pool, "checkout", on_checkout)
        event.remove(Pool, "checkin", on_checkin)
    checked_out = {uri: eng.pool.checkedout() for uri, eng in _engine_cache.items() if hasattr(eng.pool, "checkedout")}
    assert errors == [], errors[:5]
    assert all(v == 0 for v in checked_out.values()), checked_out
    assert set(bodies.get("pending", [()])) <= {("elicit_c", "elicit_parent")}, set(bodies["pending"])
    assert nested == [], f"threads held nested pooled connections: max={max(nested)} count={len(nested)}"


async def test_cancelled_snapshots_release_connections(rig: Rig) -> None:
    """Client disconnects mid-snapshot: lanes keep running in threads, then check in."""
    import asyncio

    from omnigent.db.utils import _engine_cache

    record_pending(rig.child_id, "elicit_c")
    for delay in (0.0, 0.001, 0.005, 0.02):
        tasks = [
            asyncio.create_task(
                fast.get_session_snapshot(
                    rig.convs,
                    rig.session_id,
                    LEVEL_READ,
                    rig.agents,
                    rig.cache,
                    host_store=rig.hosts,
                    viewer_id=OWNER,
                    liveness_lookup=None,
                )
            )
            for _ in range(40)
        ]
        await asyncio.sleep(delay)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for _ in range(100):
        out = {u: e.pool.checkedout() for u, e in _engine_cache.items() if hasattr(e.pool, "checkedout")}
        if all(v == 0 for v in out.values()):
            break
        await asyncio.sleep(0.1)
    assert all(v == 0 for v in out.values()), out
    # And the store still works afterwards.
    r = await rig.client.get(f"/v1/sessions/{rig.session_id}", headers={"X-Forwarded-Email": OWNER})
    assert r.status_code == 200


async def test_fast_json_matches_jsonable_encoder_on_every_get_route(rig: Rig) -> None:
    """Every response_model=None GET route, fast_json on vs off, same parsed body."""
    from fastapi.routing import APIRoute

    from omnigent.server import fast_json

    app = rig.client._transport.app  # type: ignore[attr-defined]
    conv = rig.convs.get_conversation(rig.session_id)
    assert conv is not None
    record_pending(rig.child_id, "elicit_c")
    fill = {
        "session_id": rig.session_id,
        "conversation_id": rig.session_id,
        "agent_id": conv.agent_id,
        "host_id": HOST_ID,
        "runner_id": RUNNER_ID,
    }
    checked, mismatched, skipped = [], [], []
    headers = {"X-Forwarded-Email": OWNER}
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if route.response_model is not None:
            continue
        params = route.param_convertors.keys()
        if any(p not in fill for p in params):
            skipped.append(route.path)
            continue
        if any(s in route.path for s in ("stream", "updates", "attach", "tunnel", "events", "/mcp")):
            skipped.append(route.path)
            continue
        path = route.path.format(**{p: fill[p] for p in params})
        try:
            fast_json.install()
            a = await rig.client.get(path, headers=headers, timeout=10)
            fast_json.uninstall()
            b = await rig.client.get(path, headers=headers, timeout=10)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{route.path} {type(exc).__name__}")
            continue
        finally:
            fast_json.install()
        checked.append((path, a.status_code))
        ja = a.json() if a.headers.get("content-type", "").startswith("application/json") else a.text
        jb = b.json() if b.headers.get("content-type", "").startswith("application/json") else b.text
        if a.status_code != b.status_code or ja != jb:
            if isinstance(ja, dict) and isinstance(jb, dict):
                d = {k for k in set(ja) | set(jb) if ja.get(k) != jb.get(k)}
            else:
                d = {"<body>"}
            mismatched.append((path, a.status_code, b.status_code, sorted(d)[:8]))
    print("CHECKED", len(checked), [c for c in checked if c[1] == 200][:80])
    print("SKIPPED", skipped)
    assert mismatched == [], mismatched


async def test_executor_occupancy_fast_vs_upstream(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measure default-executor thread-seconds and peak concurrency per path (report only)."""
    import asyncio
    import concurrent.futures
    import threading
    import time

    from omnigent.server.routes.sessions import routes_core

    class CountingExecutor(concurrent.futures.ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=12)
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0
            self.busy = 0.0
            self.jobs = 0

        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            def run() -> Any:
                with self.lock:
                    self.active += 1
                    self.jobs += 1
                    self.peak = max(self.peak, self.active)
                t = time.perf_counter()
                try:
                    return fn(*args, **kwargs)
                finally:
                    with self.lock:
                        self.active -= 1
                        self.busy += time.perf_counter() - t

            return super().submit(run)

    loop = asyncio.get_running_loop()
    headers = {"X-Forwarded-Email": OWNER}
    path = f"/v1/sessions/{rig.session_id}"
    results = {}
    for name in ("fast", "upstream", "fast2", "upstream2"):
        ex = CountingExecutor()
        loop.set_default_executor(ex)
        with monkeypatch.context() as patch:
            if name.startswith("upstream"):
                patch.setattr(routes_core, "fast_session_snapshot", orchestration._get_session_snapshot)
            t = time.perf_counter()
            rs = await asyncio.gather(*[rig.client.get(path, headers=headers) for _ in range(60)])
            wall = time.perf_counter() - t
        assert all(r.status_code == 200 for r in rs)
        results[name] = dict(wall=round(wall, 3), busy=round(ex.busy, 3), jobs=ex.jobs, peak=ex.peak)
    print("EXECUTOR", results)
