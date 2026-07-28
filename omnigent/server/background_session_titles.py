"""Non-blocking semantic titles for newly started sessions."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.entities.conversation import synthesize_conversation_title
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_plugins import background_title_generators
from omnigent.stores.conversation_store import ConversationStore

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.schemas import SessionEventInput

_logger = logging.getLogger(__name__)


def _background_session_title_harness_supported(harness: str | None) -> bool:
    """Return whether a known session harness may run automatic title inference."""
    if harness is None:
        return True
    canonical = canonicalize_harness(harness)
    return canonical is not None and canonical in background_title_generators()


@dataclass(frozen=True)
class BackgroundTitleRequest:
    """Immutable session inputs captured after the first prompt is forwarded."""

    session_id: str
    prompt: str
    agent_id: str | None = None
    harness_override: str | None = None
    model_override: str | None = None
    sub_agent_name: str | None = None


BackgroundTitleGenerator = Callable[[BackgroundTitleRequest], Awaitable[str | None]]

_TITLE_WRAPPERS = "'\"`“”‘’"
_TRAILING_PUNCTUATION = re.compile(r"[.!?;:,]+$")

# A fork inherits a placeholder title ("Fork of <parent>", possibly nested) at
# creation. Because that title is non-null the first-turn titler would skip the
# fork forever, so it never gets a name of its own. Treat the placeholder as
# replaceable: the fork's FIRST NEW turn (its divergence from the parent) is
# exactly the signal a good fork title should come from.
_FORK_PLACEHOLDER_RE = re.compile(r"^(?:Fork of )+")


def _is_fork_placeholder(title: str | None) -> bool:
    """True when *title* is the auto-generated 'Fork of ...' placeholder."""
    return title is not None and _FORK_PLACEHOLDER_RE.match(title) is not None


# Provenance label: "1" marks a title this system generated, so it may be
# re-titled later; a human rename clears it so a hand-typed title is never
# overwritten. Stored in conversation_labels (no schema change).
TITLE_AUTO_LABEL = "title.auto"


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to *default*."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# A session's title is refreshed from its trajectory after this many completed
# turns, at most this many times — so a title tracks where the work went without
# churning. Both overridable; set OMNIGENT_SESSION_RETITLE_AFTER_TURNS=0-agnostic
# tuning at deploy time.
def _retitle_after_turns() -> int:
    return _positive_int_env("OMNIGENT_SESSION_RETITLE_AFTER_TURNS", 6)


def _max_retitles() -> int:
    return _positive_int_env("OMNIGENT_SESSION_MAX_RETITLES", 2)


def normalize_background_title(value: str | None) -> str | None:
    """Return a compact title or ``None`` when model output is unusable."""
    if not value:
        return None
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    title = " ".join(first_line.strip(_TITLE_WRAPPERS).split())
    title = _TRAILING_PUNCTUATION.sub("", title).strip()
    if len(title) < 2 or len(title) > 60:
        return None
    return title


class RunnerBackgroundTitleGenerator:
    """Request isolated title inference from the session's bound runner."""

    def __init__(self, runner_router: RunnerRouter, *, timeout_seconds: float = 65.0) -> None:
        self._runner_router = runner_router
        self._timeout_seconds = timeout_seconds

    async def __call__(self, request: BackgroundTitleRequest) -> str | None:
        routed = self._runner_router.client_for_existing_conversation(request.session_id)
        if routed is None:
            return None
        response = await routed.client.post(
            f"/v1/sessions/{request.session_id}/background-title",
            json={
                "prompt": request.prompt,
                "agent_id": request.agent_id,
                "harness_override": request.harness_override,
                "model_override": request.model_override,
                "sub_agent_name": request.sub_agent_name,
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "generated":
            return None
        title = payload.get("title")
        return title if isinstance(title, str) else None


class BackgroundSessionTitleCoordinator:
    """Run one guarded title attempt outside the user turn's critical path."""

    def __init__(
        self,
        conversation_store: ConversationStore,
        generator: BackgroundTitleGenerator,
        *,
        timeout_seconds: float = 70.0,
        seed_wait_seconds: float = 15.0,
        max_concurrency: int = 4,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._conversation_store = conversation_store
        self._generator = generator
        self._timeout_seconds = timeout_seconds
        self._seed_wait_seconds = seed_wait_seconds
        self._generation_slots = asyncio.Semaphore(max_concurrency)
        self._pending: set[asyncio.Task[None]] = set()
        self._scheduled_session_ids: set[str] = set()
        # Re-title tracking (in-memory; provenance itself is persisted as a
        # label so a human title is never re-titled even across restarts).
        self._turns_since_title: dict[str, int] = {}
        self._retitle_count: dict[str, int] = {}

    def schedule(
        self,
        *,
        session_id: str,
        prompt: str,
        expected_seed_title: str,
        agent_id: str | None = None,
        harness_override: str | None = None,
        model_override: str | None = None,
        sub_agent_name: str | None = None,
    ) -> None:
        """Schedule at most one title attempt and return without awaiting it."""
        if session_id in self._scheduled_session_ids:
            return
        self._scheduled_session_ids.add(session_id)
        task = asyncio.create_task(
            self._run(
                request=BackgroundTitleRequest(
                    session_id=session_id,
                    prompt=prompt,
                    agent_id=agent_id,
                    harness_override=harness_override,
                    model_override=model_override,
                    sub_agent_name=sub_agent_name,
                ),
                expected_seed_title=expected_seed_title,
            ),
            name=f"background-session-title-{session_id}",
        )
        self._pending.add(task)

        def _discard(completed: asyncio.Task[None]) -> None:
            self._pending.discard(completed)
            self._scheduled_session_ids.discard(session_id)

        task.add_done_callback(_discard)

    async def wait_for_idle(self) -> None:
        """Wait for currently scheduled jobs; used by focused tests."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending))

    async def shutdown(self) -> None:
        """Cancel and drain pending title jobs during server shutdown."""
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run(
        self,
        *,
        request: BackgroundTitleRequest,
        expected_seed_title: str,
    ) -> None:
        started = time.perf_counter()
        try:
            async with self._generation_slots:
                seed_ready = await self._wait_for_seed(
                    session_id=request.session_id,
                    expected_seed_title=expected_seed_title,
                )
                if not seed_ready:
                    _logger.info(
                        "background session title skipped session=%s "
                        "reason=seed_unavailable elapsed_ms=%.1f",
                        request.session_id,
                        (time.perf_counter() - started) * 1000,
                    )
                    return
                generated = await asyncio.wait_for(
                    self._generator(request),
                    timeout=self._timeout_seconds,
                )
            title = normalize_background_title(generated)
            if title is None:
                _logger.info(
                    "background session title skipped session=%s "
                    "reason=invalid_title elapsed_ms=%.1f",
                    request.session_id,
                    (time.perf_counter() - started) * 1000,
                )
                return
            updated = await asyncio.to_thread(
                self._conversation_store.rename_conversation_if_title_matches,
                request.session_id,
                expected_seed_title,
                title,
            )
            if updated is not None:
                await self._mark_auto_titled(request.session_id)
            _logger.info(
                "background session title completed session=%s renamed=%s elapsed_ms=%.1f",
                request.session_id,
                updated is not None,
                (time.perf_counter() - started) * 1000,
            )
        except TimeoutError:
            _logger.info(
                "background session title timed out session=%s elapsed_ms=%.1f",
                request.session_id,
                (time.perf_counter() - started) * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background metadata must never fail the user turn
            _logger.warning(
                "background session title failed session=%s elapsed_ms=%.1f",
                request.session_id,
                (time.perf_counter() - started) * 1000,
                exc_info=True,
            )

    async def _wait_for_seed(
        self,
        *,
        session_id: str,
        expected_seed_title: str,
    ) -> bool:
        deadline = time.monotonic() + self._seed_wait_seconds
        while True:
            conversation = await asyncio.to_thread(
                self._conversation_store.get_conversation,
                session_id,
            )
            if conversation is None:
                return False
            if conversation.title == expected_seed_title:
                return True
            if conversation.title is not None:
                return False
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)

    def maybe_retitle(self, conversation: Conversation) -> None:
        """Re-title a session from its trajectory when it is due (off the send path).

        Called on the turn-complete (idle) edge. Cheap for the common case:
        the turn-count check is in-memory, and only when a re-title is actually
        due does it read recent items to build the prompt.
        """
        title = conversation.title
        if (
            title is None
            or conversation.parent_conversation_id is not None
            or not _background_session_title_harness_supported(conversation.harness_override)
        ):
            return
        title_is_auto = conversation.labels.get(TITLE_AUTO_LABEL) == "1"
        # note_completed_turn is in-memory only — the hot idle path does no I/O.
        # Item fetch + prompt build happen inside the scheduled task below.
        if not self.note_completed_turn(conversation.id, title_is_auto=title_is_auto):
            return
        self.schedule_retitle(
            session_id=conversation.id,
            current_title=title,
            agent_id=conversation.agent_id,
            harness_override=conversation.harness_override,
            model_override=conversation.model_override,
            sub_agent_name=conversation.sub_agent_name,
        )

    async def _mark_auto_titled(self, session_id: str) -> None:
        """Record that this system set the title, and (re)start its turn clock."""
        self._turns_since_title[session_id] = 0
        try:
            await asyncio.to_thread(
                self._conversation_store.set_labels,
                session_id,
                {TITLE_AUTO_LABEL: "1"},
            )
        except Exception:  # noqa: BLE001 - provenance is best-effort metadata
            _logger.warning("failed to mark auto title session=%s", session_id, exc_info=True)

    def note_completed_turn(self, session_id: str, *, title_is_auto: bool) -> bool:
        """Record a finished turn; return whether the session is due a re-title.

        Cheap and side-effect-free on the caller's path (in-memory only). Only
        auto-generated titles are tracked, so a hand-typed title is never
        re-titled. Caller passes ``title_is_auto`` from the already-loaded
        conversation labels, so no extra read happens on the turn path.
        """
        if not title_is_auto:
            self._turns_since_title.pop(session_id, None)
            return False
        if self._retitle_count.get(session_id, 0) >= _max_retitles():
            return False
        turns = self._turns_since_title.get(session_id, 0) + 1
        self._turns_since_title[session_id] = turns
        return turns >= _retitle_after_turns()

    def schedule_retitle(
        self,
        *,
        session_id: str,
        current_title: str,
        agent_id: str | None = None,
        harness_override: str | None = None,
        model_override: str | None = None,
        sub_agent_name: str | None = None,
    ) -> None:
        """Schedule a trajectory-based re-title, at most one in flight per session."""
        if session_id in self._scheduled_session_ids:
            return
        self._scheduled_session_ids.add(session_id)
        # Reset the clock now so turns during generation count toward the NEXT
        # cycle rather than immediately re-triggering.
        self._turns_since_title[session_id] = 0
        task = asyncio.create_task(
            self._run_retitle(
                session_id=session_id,
                current_title=current_title,
                agent_id=agent_id,
                harness_override=harness_override,
                model_override=model_override,
                sub_agent_name=sub_agent_name,
            ),
            name=f"background-session-retitle-{session_id}",
        )
        self._pending.add(task)

        def _discard(completed: asyncio.Task[None]) -> None:
            self._pending.discard(completed)
            self._scheduled_session_ids.discard(session_id)

        task.add_done_callback(_discard)

    async def _run_retitle(
        self,
        *,
        session_id: str,
        current_title: str,
        agent_id: str | None,
        harness_override: str | None,
        model_override: str | None,
        sub_agent_name: str | None,
    ) -> None:
        started = time.perf_counter()
        try:
            # Build the trajectory prompt off the event loop (DB read).
            prompt = await asyncio.to_thread(
                _build_retitle_prompt, self._conversation_store, session_id
            )
            if not prompt:
                return
            request = BackgroundTitleRequest(
                session_id=session_id,
                prompt=prompt,
                agent_id=agent_id,
                harness_override=harness_override,
                model_override=model_override,
                sub_agent_name=sub_agent_name,
            )
            async with self._generation_slots:
                generated = await asyncio.wait_for(
                    self._generator(request),
                    timeout=self._timeout_seconds,
                )
            title = normalize_background_title(generated)
            if title is None or title == current_title:
                return
            # CAS against the title we observed: if a human (or a prior job)
            # changed it meanwhile, this write no-ops and we never clobber.
            updated = await asyncio.to_thread(
                self._conversation_store.rename_conversation_if_title_matches,
                session_id,
                current_title,
                title,
            )
            if updated is not None:
                self._retitle_count[session_id] = self._retitle_count.get(session_id, 0) + 1
                await self._mark_auto_titled(session_id)
            _logger.info(
                "background session re-title session=%s renamed=%s elapsed_ms=%.1f",
                session_id,
                updated is not None,
                (time.perf_counter() - started) * 1000,
            )
        except TimeoutError:
            _logger.info("background session re-title timed out session=%s", session_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background metadata must never fail the turn
            _logger.warning(
                "background session re-title failed session=%s", session_id, exc_info=True
            )


@dataclass(frozen=True)
class PendingBackgroundSessionTitle:
    """A prepared title attempt that starts only after event forwarding succeeds."""

    coordinator: BackgroundSessionTitleCoordinator
    request: BackgroundTitleRequest
    expected_seed_title: str

    def schedule(self) -> None:
        """Start the prepared title attempt without blocking the caller."""
        self.coordinator.schedule(
            session_id=self.request.session_id,
            prompt=self.request.prompt,
            expected_seed_title=self.expected_seed_title,
            agent_id=self.request.agent_id,
            harness_override=self.request.harness_override,
            model_override=self.request.model_override,
            sub_agent_name=self.request.sub_agent_name,
        )


def prepare_background_session_title(
    *,
    coordinator: BackgroundSessionTitleCoordinator | None,
    conversation: Conversation,
    event: SessionEventInput,
) -> PendingBackgroundSessionTitle | None:
    """Prepare a guarded title attempt for a top-level or forked session.

    Fires when the session has no title yet (fresh session) or still carries the
    inherited ``"Fork of ..."`` placeholder (a fork that has taken its first new
    turn). A session that already has a real title is left untouched.
    """
    if (
        coordinator is None
        or conversation.parent_conversation_id is not None
        or not _background_session_title_harness_supported(conversation.harness_override)
    ):
        return None

    prompt = _background_title_prompt(event)
    if not prompt:
        return None

    # The title the CAS write must still see to replace it.
    if conversation.title is None:
        # Fresh session: the app seeds a deterministic title from the first
        # message; wait for that exact seed, then replace it.
        expected_seed_title = synthesize_conversation_title(
            [{"type": "input_text", "text": prompt}]
        )
        if expected_seed_title is None:
            return None
    elif _is_fork_placeholder(conversation.title):
        # Fork: replace the "Fork of ..." placeholder itself, titling from the
        # divergence turn (this event) rather than the shared parent opening.
        expected_seed_title = conversation.title
    else:
        return None  # a real title already exists — never overwrite it

    return PendingBackgroundSessionTitle(
        coordinator=coordinator,
        request=BackgroundTitleRequest(
            session_id=conversation.id,
            prompt=prompt,
            agent_id=conversation.agent_id,
            harness_override=conversation.harness_override,
            model_override=conversation.model_override,
            sub_agent_name=conversation.sub_agent_name,
        ),
        expected_seed_title=expected_seed_title,
    )


def _background_title_prompt(event: SessionEventInput) -> str:
    if event.type == "slash_command":
        name = event.data.get("name")
        arguments = event.data.get("arguments", "")
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str):
            return ""
        return f"/{name.strip()} {arguments}".strip()

    content = event.data.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "input_text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(parts)[:4000]


_RETITLE_NOISE_PREFIXES = ("[System:", "<task-notification>", "[Request interrupted")
_RETITLE_MAX_CHARS = 3500


def _message_text(item: Any) -> tuple[str, str]:
    """Return (role, plain text) for a real message item; ('', '') otherwise."""
    data = getattr(item, "data", None)
    if getattr(item, "type", None) != "message" or data is None:
        return "", ""
    if getattr(data, "is_meta", False):
        return "", ""
    role = getattr(data, "role", "") or ""
    content = getattr(data, "content", None)
    if not isinstance(content, list):
        return role, ""
    parts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return role, " ".join(" ".join(parts).split())


def _build_retitle_prompt(conversation_store: ConversationStore, session_id: str) -> str:
    """Render a compact trajectory (opening, mid turns, ending) for re-titling.

    A fork shares its parent's opening, and a session that drifted has moved on
    from its first message, so a good current title has to reflect the whole arc.
    """
    try:
        page = conversation_store.list_items(session_id, 400, None, None, "asc", None)
    except Exception:  # noqa: BLE001 - best-effort metadata
        return ""

    user_turns: list[str] = []
    last_assistant = ""
    for item in page.data:
        role, text = _message_text(item)
        if not text or text.startswith(_RETITLE_NOISE_PREFIXES):
            continue
        if role == "user":
            user_turns.append(text)
        elif role == "assistant":
            last_assistant = text

    if not user_turns:
        return ""

    # A couple from the start, a few spread through the middle, the last one.
    if len(user_turns) <= 6:
        sampled = user_turns
    else:
        idx = sorted(
            {0, 1, len(user_turns) - 1} | {round(i * (len(user_turns) - 1) / 5) for i in range(6)}
        )
        sampled = [user_turns[i] for i in idx]

    lines = [f"User: {t[:280]}" for t in sampled]
    if last_assistant:
        lines.append(f"Assistant (latest): {last_assistant[:320]}")
    return "\n".join(lines)[:_RETITLE_MAX_CHARS]
