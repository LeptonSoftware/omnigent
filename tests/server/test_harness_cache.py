"""Repeat harness lookups skip the agent store until the agent's spec changes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server import harness_cache


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    harness_cache.clear()


class _Cache:
    def __init__(self) -> None:
        self.location: str | None = "loc-1"

    def cached_location(self, agent_id: str) -> str | None:
        return self.location


def _conv(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {"agent_id": "ag_1", "harness_override": None, "sub_agent_name": None}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _counting_resolver(answer: str | None = "codex") -> tuple[Any, list[int]]:
    calls: list[int] = []

    def resolve(conv: Any, *, agent_store: Any = None, agent_cache: Any = None) -> str | None:
        calls.append(1)
        return answer

    return harness_cache.cache_resolved_harness(resolve), calls


def test_second_lookup_is_served_from_cache() -> None:
    resolve, calls = _counting_resolver()
    store, cache = object(), _Cache()
    assert resolve(_conv(), agent_store=store, agent_cache=cache) == "codex"
    assert resolve(_conv(), agent_store=store, agent_cache=cache) == "codex"
    assert len(calls) == 1


def test_spec_change_invalidates() -> None:
    resolve, calls = _counting_resolver()
    store, cache = object(), _Cache()
    resolve(_conv(), agent_store=store, agent_cache=cache)
    cache.location = "loc-2"  # AgentCache.replace swapped the bundle
    resolve(_conv(), agent_store=store, agent_cache=cache)
    assert len(calls) == 2


def test_override_none_and_other_stores_bypass_or_miss() -> None:
    resolve, calls = _counting_resolver()
    cache = _Cache()
    resolve(_conv(harness_override="pi"), agent_store=object(), agent_cache=cache)
    resolve(_conv(agent_id=None), agent_store=object(), agent_cache=cache)
    resolve(_conv(), agent_store=object(), agent_cache=cache)
    resolve(_conv(), agent_store=object(), agent_cache=cache)
    assert len(calls) == 4


def test_none_answers_are_not_cached() -> None:
    resolve, calls = _counting_resolver(answer=None)
    store, cache = object(), _Cache()
    resolve(_conv(), agent_store=store, agent_cache=cache)
    resolve(_conv(), agent_store=store, agent_cache=cache)
    assert len(calls) == 2


def test_ttl_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_HARNESS_CACHE_TTL_S", "0")
    resolve, calls = _counting_resolver()
    store, cache = object(), _Cache()
    resolve(_conv(), agent_store=store, agent_cache=cache)
    resolve(_conv(), agent_store=store, agent_cache=cache)
    assert len(calls) == 2


async def test_in_place_agent_edit_is_not_served_stale(
    db_uri: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH /v1/agents/{id} (AgentStore.update_configuration) drops the entry.

    The route rewrites the agent row and evicts the agent cache; the very next
    snapshot must report the edited harness, not the cached one.
    """
    import httpx
    from fastapi import FastAPI

    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl
    from omnigent.server.routes.builtin_agents import create_builtin_agents_router
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from tests.server.helpers import build_agent_bundle

    monkeypatch.setenv("OMNIGENT_HARNESS_CACHE_TTL_S", "3600")
    agent_store = SqlAlchemyAgentStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")
    app = FastAPI()
    app.include_router(
        create_builtin_agents_router(
            agent_store,
            agent_cache,
            artifact_store=artifacts,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        ),
        prefix="/v1",
    )
    alice = {"X-Forwarded-Email": "alice@example.com"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        installed = await client.post(
            "/v1/agents",
            headers=alice,
            files={"bundle": ("b.tar.gz", build_agent_bundle("orion"), "application/gzip")},
        )
        assert installed.status_code == 200, installed.text
        agent_id = installed.json()["id"]
        conv = _conv(agent_id=agent_id)

        def harness() -> str | None:
            return _resolve_harness_impl(conv, agent_store=agent_store, agent_cache=agent_cache)

        before = harness()
        assert before is not None
        assert harness() == before  # now served from the cache
        target = "codex" if before != "codex" else "claude-sdk"

        config = await client.get(f"/v1/agents/{agent_id}/config", headers=alice)
        assert config.status_code == 200, config.text
        patched = await client.patch(
            f"/v1/agents/{agent_id}",
            headers={**alice, "If-Match": config.headers["etag"]},
            json={"harness": target},
        )
        assert patched.status_code == 200, patched.text
        assert agent_store.get(agent_id).version == installed.json()["version"] + 1

        assert harness() == target
