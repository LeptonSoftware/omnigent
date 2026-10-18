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
