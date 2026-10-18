"""Short-lived cache of a session's resolved harness.

This module is FORK-OWNED. Resolving a session's harness reads the agent row
from the store and loads its spec. Every session snapshot does it, and agent
specs almost never change, so the answer is kept for a few seconds.

An entry is dropped early when the agent cache's in-memory spec for that agent
now comes from a different bundle or is gone. In-process edits do one of the
two: MCP edits call ``AgentCache.replace``, and the in-place
``PATCH /v1/agents/{id}`` (``AgentStore.update_configuration``) and reinstalls
call ``AgentCache.evict``. Either way the edit shows on the next read. The TTL
bounds staleness for edits made by another process, such as another replica.
Only successful lookups of an existing agent are cached; ``None`` answers and
errors always re-resolve.
"""

from __future__ import annotations

import functools
import os
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

_DEFAULT_TTL_S = 30.0
_MAX_ENTRIES = 4096

_lock = threading.Lock()
# key -> (expires_at, agent_store, agent_cache, spec_location, harness)
_Entry = tuple[float, Any, Any, str | None, str]
# custom-lint: disable-next=workspace-scoped-cache -- key carries the workspace id
_entries: dict[tuple[Any, ...], _Entry] = {}

_F = TypeVar("_F", bound=Callable[..., Any])


def _ttl_seconds() -> float:
    raw = os.environ.get("OMNIGENT_HARNESS_CACHE_TTL_S")
    if raw is None:
        return _DEFAULT_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_TTL_S


def _spec_location(agent_cache: Any, agent_id: str) -> str | None:
    if agent_cache is None:
        from omnigent.runtime import get_agent_cache

        try:
            agent_cache = get_agent_cache()
        except RuntimeError:
            return None
    lookup = getattr(agent_cache, "cached_location", None)
    if not callable(lookup):
        return None
    location = lookup(agent_id)
    return location if isinstance(location, str) else None


def _workspace_id() -> int:
    from omnigent.db.db_models import current_workspace_id

    return current_workspace_id()


def cache_resolved_harness(resolve: _F) -> _F:
    """Wrap ``_resolve_harness_impl`` so repeat lookups skip the store.

    Calls with no bound agent, or with a per-session harness override, pass
    straight through: they never touch the store.

    :param resolve: The real resolver, ``(conv, *, agent_store, agent_cache)``.
    :returns: The caching resolver with the same signature.
    """

    @functools.wraps(resolve)
    def wrapper(conv: Any, *, agent_store: Any = None, agent_cache: Any = None) -> Any:
        if conv is None or conv.harness_override or conv.agent_id is None:
            return resolve(conv, agent_store=agent_store, agent_cache=agent_cache)
        ttl = _ttl_seconds()
        if ttl <= 0:
            return resolve(conv, agent_store=agent_store, agent_cache=agent_cache)
        key = (
            id(agent_store),
            id(agent_cache),
            _workspace_id(),
            conv.agent_id,
            conv.sub_agent_name,
        )
        now = time.monotonic()
        with _lock:
            entry = _entries.get(key)
        if entry is not None:
            expires_at, entry_store, entry_cache, location, harness = entry
            if (
                expires_at > now
                and entry_store is agent_store
                and entry_cache is agent_cache
                and _spec_location(agent_cache, conv.agent_id) == location
            ):
                return harness
        harness = resolve(conv, agent_store=agent_store, agent_cache=agent_cache)
        if harness is not None:
            location = _spec_location(agent_cache, conv.agent_id)
            with _lock:
                if len(_entries) >= _MAX_ENTRIES:
                    _entries.clear()
                _entries[key] = (now + ttl, agent_store, agent_cache, location, harness)
        return harness

    return wrapper  # type: ignore[return-value]


def clear() -> None:
    """Drop every cached harness."""
    with _lock:
        _entries.clear()
