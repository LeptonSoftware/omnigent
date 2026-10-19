"""``GET /v1/sessions/{id}`` snapshot with its store reads batched and overlapped.

This module is FORK-OWNED. It builds the same :class:`SessionResponse` as
upstream's ``_get_session_snapshot`` (orchestration.py), which stays the path
for every other caller. Upstream awaits about nine store reads one after
another, each in its own worker-thread hop with its own pooled connection
checkout. Here the reads that only need the conversation row run as three
concurrent lanes:

* ``db``: items, liveness, subtree usage, the host row and the pending
  elicitation walk, back to back on one pooled connection
  (:func:`shared_read_scope`).
* ``agent``: the agent row, its parsed spec and context window, and the
  harness. Kept off the shared connection because a cold bundle load does
  network I/O.
* ``probe``: the runner live-status probe, only on a status-cache miss.

The pending elicitation walk lists every descendant session one query per
node whenever ANY session in the process has an outstanding prompt. The usage
read already loaded the whole spawn tree, so the walk runs only when one of
the pending sessions is actually in this session's subtree.

Upstream helpers are called through the ``orchestration`` module at call time,
so a test that patches one there patches this path too. Equality with the
upstream snapshot is pinned by ``tests/server/test_session_snapshot_fast.py``,
which also fails when upstream's function changes and this mirror needs a look.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
from fastapi import Request

from omnigent.db.utils import shared_read_scope
from omnigent.entities import Conversation, ConversationItem
from omnigent.errors import OmnigentError
from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import server_timing, session_live_state
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes._sessions import orchestration as _orch
from omnigent.server.schemas import SessionResponse
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.host_store import Host, HostStore


@dataclass
class _DbReads:
    items: list[ConversationItem] = field(default_factory=list)
    liveness: dict[str, Any] | None = None
    subtree_usage: dict[str, Any] = field(default_factory=dict)
    host: Host | None = None
    pending_elicitation_events: list[dict[str, Any]] = field(default_factory=list)
    durations: dict[str, float] = field(default_factory=dict)


@dataclass
class _AgentReads:
    llm_model: str | None = None
    context_window: int | None = None
    agent_name: str | None = None
    harness: str | None = None
    durations: dict[str, float] = field(default_factory=dict)


class _TreeCapture:
    """Delegate to a conversation store, keeping the spawn-tree pages it lists.

    Attribute lookups go to the wrapped store at call time, so a test that
    patches a method on the store instance still sees its patch.
    """

    def __init__(self, store: ConversationStore) -> None:
        self._store = store
        self.root_ids: set[str] = set()
        self.rows: list[Conversation] = []
        self.complete = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def list_conversations(self, *args: Any, **kwargs: Any) -> Any:
        page = self._store.list_conversations(*args, **kwargs)
        root = kwargs.get("root_conversation_id")
        if root is not None and kwargs.get("include_archived") and kwargs.get("kind") is None:
            self.root_ids.add(root)
            self.rows.extend(page.data)
            self.complete = not page.has_more or page.last_id is None
        return page

    def subtree_ids(self, conversation_id: str) -> set[str] | None:
        """Ids at or below *conversation_id* in the captured tree, if trustworthy."""
        if not self.complete or len(self.root_ids) != 1:
            return None
        if not any(row.id == conversation_id for row in self.rows):
            return None
        children: dict[str, list[str]] = {}
        for row in self.rows:
            if row.parent_conversation_id is not None:
                children.setdefault(row.parent_conversation_id, []).append(row.id)
        found = {conversation_id}
        stack = [conversation_id]
        while stack:
            for child_id in children.get(stack.pop(), ()):
                if child_id not in found:
                    found.add(child_id)
                    stack.append(child_id)
        return found


def _timed(durations: dict[str, float], name: str, started: float) -> float:
    now = time.perf_counter()
    durations[name] = durations.get(name, 0.0) + (now - started)
    return now


def _read_db_lane(
    conv_store: ConversationStore,
    conv: Conversation,
    *,
    include_items: bool,
    include_usage: bool,
    liveness_lookup: Callable[[list[str]], dict[str, Any]] | None,
    host_store: HostStore | None,
    read_host: bool,
    submitted: float,
) -> _DbReads:
    reads = _DbReads()
    started = _timed(reads.durations, "db_queue", submitted)
    session_id = conv.id
    with shared_read_scope():
        if include_items:
            # Most recent page, returned chronologically (see upstream snapshot).
            page = conv_store.list_items(conversation_id=session_id, limit=100, order="desc")
            reads.items = list(reversed(page.data))
            started = _timed(reads.durations, "items", started)
        if liveness_lookup is not None:
            reads.liveness = liveness_lookup([session_id])
            started = _timed(reads.durations, "liveness", started)
        capture = _TreeCapture(conv_store)
        if include_usage:
            reads.subtree_usage = _orch.load_session_usage(
                conv.id,
                cast(ConversationStore, capture),
                root_conversation_id=conv.root_conversation_id,
            )
            started = _timed(reads.durations, "usage", started)
        if read_host and host_store is not None and conv.host_id is not None:
            reads.host = host_store.get_host(conv.host_id)
            started = _timed(reads.durations, "host", started)
        reads.pending_elicitation_events = _pending_elicitations(conv_store, conv, capture)
        _timed(reads.durations, "elicitations", started)
    return reads


def _pending_elicitations(
    conv_store: ConversationStore, conv: Conversation, capture: _TreeCapture
) -> list[dict[str, Any]]:
    """Upstream's pending-elicitation snapshot, skipping a walk that cannot add events."""
    others = set(pending_elicitations.pending_session_ids()) - {conv.id}
    subtree = capture.subtree_ids(conv.id) if others else None
    if others and subtree is not None and not (others & subtree):
        # Descendants are a subset of the spawn tree (a parent never changes
        # and a child inherits its parent's root), so no descendant has a prompt.
        return pending_elicitations.snapshot_for(conv.id)
    return _orch._pending_elicitation_snapshot_for_session(conv_store, conv)


def _read_agent_lane(
    conv: Conversation,
    agent_store: AgentStore | None,
    agent_cache: AgentCache | None,
    *,
    submitted: float,
) -> _AgentReads:
    reads = _AgentReads()
    started = _timed(reads.durations, "agent_queue", submitted)
    if agent_store is not None and agent_cache is not None and conv.agent_id is not None:
        try:
            with shared_read_scope():
                agent = agent_store.get(conv.agent_id)
            if agent is not None:
                reads.agent_name = agent.name
                if agent.bundle_location is not None:
                    loaded = agent_cache.load(
                        agent.id, agent.bundle_location, expand_env=agent.operator_authored
                    )
                    resolved_spec = loaded.spec
                    if conv.sub_agent_name:
                        sub_spec = _orch._find_spec_by_name(loaded.spec, conv.sub_agent_name)
                        if sub_spec is None:
                            # A native sub-agent is named by the CLI, never by the spec.
                            wrapper = conv.labels.get(_orch._CLAUDE_NATIVE_WRAPPER_LABEL_KEY, "")
                            if not wrapper.endswith("-subagent"):
                                _orch._logger.warning(
                                    "Sub-agent %r for session %s did not resolve in the parent "
                                    "spec; snapshot reports the parent's identity. Likely a "
                                    "renamed/removed sub-agent or stale session metadata.",
                                    conv.sub_agent_name,
                                    conv.id,
                                    extra={"session_id": conv.id},
                                )
                        else:
                            resolved_spec = sub_spec
                    if resolved_spec is not None:
                        if resolved_spec.name:
                            reads.agent_name = resolved_spec.name
                        reads.llm_model = resolved_spec.executor.model
                        reads.context_window = _orch.resolve_effective_context_window(
                            resolved_spec.executor.context_window,
                            reads.llm_model,
                            model_override=conv.model_override,
                        )
        except Exception:  # noqa: BLE001 - mirrors upstream's best-effort agent block
            pass
        started = _timed(reads.durations, "agent", started)
    reads.harness = _orch._resolve_harness(conv, agent_store=agent_store, agent_cache=agent_cache)
    _timed(reads.durations, "harness", started)
    return reads


async def _probe_live_status(
    runner_client: httpx.AsyncClient, conv: Conversation, durations: dict[str, float]
) -> bool:
    started = time.perf_counter()
    try:
        probed = await _orch._probe_runner_live_status(runner_client, conv.id, conv.runner_id)
        return probed is not None
    finally:
        _timed(durations, "probe", started)


async def _none() -> None:
    return None


async def get_session_snapshot(
    conv_store: ConversationStore,
    session_id: str,
    permission_level: int | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    conversation: Conversation | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, Any]] | None = None,
    include_items: bool = True,
    include_live_status: bool = True,
    runner_exit_reports: RunnerExitReports | None = None,
    refresh_state: bool = False,
    host_store: HostStore | None = None,
    sandbox_config: Any = None,
    viewer_id: str | None = None,
    request: Request | None = None,
    include_usage: bool = True,
) -> SessionResponse:
    """Drop-in for upstream ``_get_session_snapshot`` with overlapped reads.

    Same parameters, same response, same errors. Phase timings go to the
    request's :class:`~omnigent.server.server_timing.RequestTiming` when one
    is running.
    """
    timing = server_timing.current()
    conv = conversation
    if conv is None:
        conv = await asyncio.to_thread(conv_store.get_conversation, session_id)
    if conv is None:
        raise _orch._session_not_found()
    if timing is not None:
        timing.mark("conv")

    from omnigent.runtime import get_runner_client, get_runner_router

    runner_client: httpx.AsyncClient | None = None
    runner_router = get_runner_router()
    if runner_router is not None:
        try:
            routed = runner_router.client_for_session_resources(session_id, conversation=conv)
            runner_client = routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _orch._logger.debug(
                "No runner bound for session=%s on snapshot build",
                session_id,
                extra={"session_id": session_id},
            )
    if runner_client is None:
        runner_client = get_runner_client()

    if refresh_state:
        wrapper = conv.labels.get(_orch._CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
        _orch._invalidate_runner_backed_snapshot_state(
            session_id,
            cancel_inflight=False,
            drop_model_options=(
                runner_client is not None and wrapper != _orch._CURSOR_NATIVE_WRAPPER_LABEL_VALUE
            ),
        )

    status = _orch._session_status_from_cache(session_id, conv.live_status)
    should_probe = (
        status == "idle"
        and include_live_status
        and _orch._session_status_cache.get(session_id) is None
        and runner_client is not None
    )

    probe_durations: dict[str, float] = {}
    submitted = time.perf_counter()
    db_reads, agent_reads, probed = await asyncio.gather(
        asyncio.to_thread(
            _read_db_lane,
            conv_store,
            conv,
            include_items=include_items,
            include_usage=include_usage,
            liveness_lookup=liveness_lookup,
            host_store=host_store,
            read_host=sandbox_config is not None,
            submitted=submitted,
        ),
        asyncio.to_thread(_read_agent_lane, conv, agent_store, agent_cache, submitted=submitted),
        _probe_live_status(runner_client, conv, probe_durations)
        if should_probe and runner_client is not None
        else _none(),
    )
    if timing is not None:
        timing.mark("reads")
        for durations in (db_reads.durations, agent_reads.durations, probe_durations):
            for name, seconds in durations.items():
                timing.add(name, seconds)
    if probed:
        status = _orch._session_status_from_cache(session_id)

    last_total_tokens: int | None = None
    raw_label = conv.labels.get(_orch._LAST_CONTEXT_TOKENS_LABEL_KEY)
    if isinstance(raw_label, str) and raw_label.isdigit():
        last_total_tokens = int(raw_label)
    last_task_error = _orch._last_task_error_from_labels(conv.labels)
    if runner_exit_reports is not None and conv.runner_id is not None:
        exit_error = runner_exit_reports.get(conv.runner_id)
        runner_ran = (
            runner_exit_reports.was_connected(conv.runner_id)
            or session_live_state.last_liveness_stamp(conv.runner_id) is not None
        )
        # A cause recorded by a previous runner attempt is stale once a new
        # runner is bound; surface that runner's exit instead of hiding it.
        cause_runner = _orch._last_task_error_runner_id_from_labels(conv.labels)
        stale_cause = (
            last_task_error is not None
            and cause_runner is not None
            and cause_runner != conv.runner_id
        )
        if (
            exit_error is not None
            and (status != "idle" or not runner_ran)
            and (status != "failed" or last_task_error is None or stale_cause)
        ):
            last_task_error = {"code": "runner_failed_to_start", "message": exit_error}
            status = "failed"
    llm_model = agent_reads.llm_model
    context_window = agent_reads.context_window
    if reported_model := _orch.concrete_reported_model(conv.reported_model):
        llm_model = reported_model

    from omnigent.server.routes.sandbox_inference import configured_snapshot, inference_service

    inference_configured = configured_snapshot(conv.inference_snapshot)
    inference_error = None
    if inference_configured and conv.inference_snapshot is not None:
        catalog = (
            await inference_service(request).catalog(conv.inference_snapshot)
            if request is not None
            else conv.inference_snapshot["catalog"]
        )
        model_options = catalog["models"]
        inference_error = catalog.get("error")
        if not conv.reported_model:
            llm_model = conv.model_override or catalog.get("default_model")
    else:
        model_options = await _orch._fetch_model_options(
            runner_client, session_id, conv, agent_store
        )
    raw_window_label = conv.labels.get(_orch._LAST_CONTEXT_WINDOW_LABEL_KEY)
    if isinstance(raw_window_label, str) and raw_window_label.isdigit():
        observed = int(raw_window_label)
        if observed > 0:
            context_window = observed
    runner_online: bool | None = None
    host_online: bool | None = None
    if db_reads.liveness is not None:
        result = db_reads.liveness.get(session_id)
        if result is not None:
            runner_online = result.runner_online
            host_online = result.host_online
    host_resumable = False
    if db_reads.host is not None and sandbox_config is not None:
        host_resumable = _orch.host_resume_supported(db_reads.host, sandbox_config)
    if timing is not None:
        timing.mark("options")
    response = _orch._build_session_response(
        conv,
        db_reads.items,
        status,
        permission_level,
        side_chat_sealed=await _orch._codex_side_chat_fork_sealed(conv, conv_store),
        background_task_count=_orch._session_background_task_count_cache.get(session_id),
        background_tasks=_orch._session_background_tasks_cache.get(session_id),
        llm_model=llm_model,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        last_task_error=last_task_error,
        agent_name=agent_reads.agent_name,
        model_options=model_options,
        runner_online=runner_online,
        host_online=host_online,
        host_resumable=host_resumable,
        pending_elicitation_events=db_reads.pending_elicitation_events,
        subtree_usage=db_reads.subtree_usage,
        viewer_id=viewer_id,
        agent_store=agent_store,
        agent_cache=agent_cache,
        resolved_harness=(agent_reads.harness,),
    )
    response.inference_configured = inference_configured
    response.inference_error = inference_error
    response.usage_included = include_usage
    if timing is not None:
        timing.mark("build")
        timing.publish()
    return response
