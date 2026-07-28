"""Fork extensions to upstream's background session titler — FORK-OWNED.

Upstream has no file here, so none of this conflicts on a merge, and
``background_session_titles.py`` (which upstream actively develops) stays
pristine. Two behaviours live here:

1. **Forks get named from their divergence.** A fork is created with a non-null
   ``"Fork of <parent>"`` placeholder, and upstream's titler skips any session
   that already has a title — so forks kept the placeholder forever.
   :func:`prepare_session_title` treats a genuine placeholder as replaceable and
   titles the fork from its FIRST NEW turn, which is what distinguishes one fork
   from another.

2. **Titles refresh as the work drifts.** After N completed turns (at most M
   times) :class:`ForkAwareTitleCoordinator` regenerates the title from a
   compact trajectory digest instead of the first message alone.

Provenance is a ``conversation_labels`` entry, so there is NO schema change and
a hand-typed title is never overwritten — a human rename clears the marker.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    BackgroundTitleRequest,
    PendingBackgroundSessionTitle,
    _background_session_title_harness_supported,
    normalize_background_title,
    prepare_background_session_title,
)
from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY, ConversationStore

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation
    from omnigent.server.schemas import SessionEventInput

_logger = logging.getLogger(__name__)

# Provenance: "1" marks a title this system generated (so it may be refreshed);
# a human rename writes "0" and opts the session out permanently.
TITLE_AUTO_LABEL = "title.auto"

# A fork inherits "Fork of <parent>" (possibly nested) at creation.
_FORK_PLACEHOLDER_RE = re.compile(r"^(?:Fork of )+")

# Upper bound on sessions tracked for re-titling; bounds memory on a long-lived
# server. The coldest entry is evicted first.
_MAX_TRACKED_SESSIONS = 4096

_RETITLE_NOISE_PREFIXES = (
    "[System:",
    "<task-notification>",
    "[Request interrupted",
    "This session is being continued",
)
_RETITLE_MAX_CHARS = 3500


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


def _retitle_after_turns() -> int:
    """Completed turns before a title is refreshed."""
    return _positive_int_env("OMNIGENT_SESSION_RETITLE_AFTER_TURNS", 6)


def _max_retitles() -> int:
    """Maximum number of times one session may be re-titled."""
    return _positive_int_env("OMNIGENT_SESSION_MAX_RETITLES", 2)


def _is_fork_placeholder(title: str | None, labels: dict[str, str] | None = None) -> bool:
    """True when *title* is still the auto-generated 'Fork of ...' placeholder.

    The title string alone is not proof: a user may legitimately NAME a session
    "Fork of my auth experiment", and replacing that would destroy their title.
    Require real fork lineage (the label the store writes at fork creation) and
    that the title was not explicitly human-set.

    :param title: The session's current title.
    :param labels: The session's labels, carrying fork lineage and provenance.
    :returns: ``True`` when the title may be replaced by a generated one.
    """
    if title is None or _FORK_PLACEHOLDER_RE.match(title) is None:
        return False
    labels = labels or {}
    if FORK_SOURCE_LABEL_KEY not in labels:
        return False  # not actually a fork — the prefix is the user's own words
    return labels.get(TITLE_AUTO_LABEL) != "0"  # a human rename opts out


@dataclass
class _SessionTitleState:
    """Per-session re-title bookkeeping (in-memory, LRU-evicted)."""

    turns_since_title: int = 0
    retitles: int = 0
    # Identifies the turn last counted, so repeated ``idle`` publishes for the
    # SAME turn (sub-agent echoes, retries) don't inflate the count.
    last_turn_key: str | None = None


def _message_text(item: Any) -> tuple[str, str]:
    """Return ``(role, plain text)`` for a real message item; ``("", "")`` otherwise.

    Vendored rather than importing upstream's private ``_title_content_from_item``:
    a private symbol carries no stability guarantee, and a silent refactor there
    would break titling with no merge conflict to warn us.

    :param item: A conversation item.
    :returns: The message role and its flattened text.
    """
    data = getattr(item, "data", None)
    if getattr(item, "type", None) != "message" or data is None:
        return "", ""
    if getattr(data, "is_meta", False):
        return "", ""  # injected context, not something the user said
    role = getattr(data, "role", "") or ""
    content = getattr(data, "content", None)
    if not isinstance(content, list):
        return role, ""
    parts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return role, " ".join(" ".join(parts).split())


def build_retitle_prompt(conversation_store: ConversationStore, session_id: str) -> str:
    """Render a compact trajectory (opening, mid turns, ending) for re-titling.

    A fork shares its parent's opening and a drifted session has moved on from
    its first message, so a current title has to reflect the whole arc.

    :param conversation_store: Store used to read the session's items.
    :param session_id: The session to summarize.
    :returns: A prompt string, or ``""`` when there is nothing worth titling.
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

    # A couple from the start, several spread through the middle, the last one —
    # a fork's divergence is a turn or two in, not at the opening.
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


def prepare_session_title(
    *,
    coordinator: BackgroundSessionTitleCoordinator | None,
    conversation: Conversation,
    event: SessionEventInput,
) -> PendingBackgroundSessionTitle | None:
    """Prepare a title attempt, extending upstream's rules to cover forks.

    Delegates to upstream for a fresh (untitled) session. The fork case is the
    fork's own: replace the ``"Fork of ..."`` placeholder using this event, the
    divergence turn, as the prompt.

    :param coordinator: The title coordinator, or ``None`` when disabled.
    :param conversation: The session being dispatched to.
    :param event: The user event being forwarded.
    :returns: A pending attempt, or ``None`` when the session is ineligible.
    """
    if conversation.title is None:
        # Guard upstream's path: a whitespace-only message is truthy there, so
        # it would schedule generation on an empty prompt and yield no usable
        # seed. Cheaper and safer to skip before the LLM call.
        if not _fork_title_prompt(event).strip():
            return None
        return prepare_background_session_title(
            coordinator=coordinator, conversation=conversation, event=event
        )
    if (
        coordinator is None
        or conversation.parent_conversation_id is not None
        or not _is_fork_placeholder(conversation.title, conversation.labels)
        or not _background_session_title_harness_supported(conversation.harness_override)
    ):
        return None

    prompt = _fork_title_prompt(event)
    if not prompt:
        return None
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
        # CAS target: the placeholder itself.
        expected_seed_title=conversation.title,
    )


def _fork_title_prompt(event: SessionEventInput) -> str:
    """Flatten a session event into prompt text (mirrors upstream's extraction)."""
    if event.type == "slash_command":
        name = event.data.get("name")
        arguments = event.data.get("arguments", "")
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str):
            return ""
        return f"/{name.strip()} {arguments}".strip()
    content = event.data.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "input_text" and isinstance(b.get("text"), str)
    ]
    return " ".join(parts)[:4000]


class ForkAwareTitleCoordinator(BackgroundSessionTitleCoordinator):
    """Upstream coordinator plus provenance tracking and trajectory re-titling."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # LRU-bounded: a long-lived server sees unboundedly many sessions, and
        # losing state for a cold session only costs it a re-title cycle.
        self._title_state: OrderedDict[str, _SessionTitleState] = OrderedDict()

    def _state_for(self, session_id: str) -> _SessionTitleState:
        """Fetch (or create) per-session state, evicting the coldest entry."""
        state = self._title_state.get(session_id)
        if state is None:
            state = _SessionTitleState()
            self._title_state[session_id] = state
            while len(self._title_state) > _MAX_TRACKED_SESSIONS:
                self._title_state.popitem(last=False)
        else:
            self._title_state.move_to_end(session_id)
        return state

    async def _run(self, *, request: BackgroundTitleRequest, expected_seed_title: str) -> None:
        """Run upstream's first-turn attempt, then record provenance if it landed."""
        await super()._run(request=request, expected_seed_title=expected_seed_title)
        conversation = await asyncio.to_thread(
            self._conversation_store.get_conversation, request.session_id
        )
        if conversation is not None and conversation.title not in (None, expected_seed_title):
            await self._mark_auto_titled(request.session_id)

    async def _mark_auto_titled(self, session_id: str) -> None:
        """Record that this system set the title, and restart its turn clock."""
        self._state_for(session_id).turns_since_title = 0
        try:
            await asyncio.to_thread(
                self._conversation_store.set_labels, session_id, {TITLE_AUTO_LABEL: "1"}
            )
        except Exception:  # noqa: BLE001 - provenance is best-effort metadata
            _logger.warning("failed to mark auto title session=%s", session_id, exc_info=True)

    def note_completed_turn(
        self, session_id: str, *, title_is_auto: bool, turn_key: str | None = None
    ) -> bool:
        """Record a finished turn; return whether the session is due a re-title.

        In-memory only, so the caller's path does no I/O. ``turn_key`` (the
        turn's response id) de-duplicates repeated ``idle`` publishes for one
        turn; without it every echo would count as a turn.

        :param session_id: The session that completed a turn.
        :param title_is_auto: Whether the current title was system-generated.
        :param turn_key: Identifier for the turn, for de-duplication.
        :returns: ``True`` when a re-title should be scheduled.
        """
        if not title_is_auto:
            self._title_state.pop(session_id, None)  # renamed: stop tracking
            return False
        state = self._state_for(session_id)
        if turn_key is not None and turn_key == state.last_turn_key:
            return False  # same turn, already counted
        state.last_turn_key = turn_key
        if state.retitles >= _max_retitles():
            return False
        state.turns_since_title += 1
        return state.turns_since_title >= _retitle_after_turns()

    def maybe_retitle(self, conversation: Conversation, *, turn_key: str | None = None) -> None:
        """Refresh an auto-generated title from the trajectory when due.

        Called on the turn-complete (idle) edge. The threshold check is
        in-memory; the item read, prompt build, and generation all happen in the
        scheduled background task.

        :param conversation: The session that just went idle.
        :param turn_key: The turn's response id, for de-duplication.
        """
        title = conversation.title
        if (
            title is None
            or conversation.parent_conversation_id is not None
            or not _background_session_title_harness_supported(conversation.harness_override)
        ):
            return
        title_is_auto = conversation.labels.get(TITLE_AUTO_LABEL) == "1"
        if not self.note_completed_turn(
            conversation.id, title_is_auto=title_is_auto, turn_key=turn_key
        ):
            return
        self._schedule_retitle(
            session_id=conversation.id,
            current_title=title,
            agent_id=conversation.agent_id,
            harness_override=conversation.harness_override,
            model_override=conversation.model_override,
            sub_agent_name=conversation.sub_agent_name,
        )

    def _schedule_retitle(
        self,
        *,
        session_id: str,
        current_title: str,
        agent_id: str | None = None,
        harness_override: str | None = None,
        model_override: str | None = None,
        sub_agent_name: str | None = None,
    ) -> None:
        """Schedule a trajectory re-title, at most one in flight per session."""
        if session_id in self._scheduled_session_ids:
            return
        self._scheduled_session_ids.add(session_id)
        # Reset now so turns during generation count toward the NEXT cycle.
        self._state_for(session_id).turns_since_title = 0
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
                build_retitle_prompt, self._conversation_store, session_id
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
                    self._generator(request), timeout=self._timeout_seconds
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
                self._state_for(session_id).retitles += 1
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
