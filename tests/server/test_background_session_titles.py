from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    BackgroundTitleRequest,
    RunnerBackgroundTitleGenerator,
    normalize_background_title,
    prepare_background_session_title,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


def _seed_session(store: SqlAlchemyConversationStore, title: str) -> str:
    conversation = store.create_conversation(kind="default", title=title)
    return conversation.id


async def test_prepare_background_title_for_eligible_session(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is not None


async def test_prepare_background_title_from_message(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = uuid.uuid4().hex
    conversation = store.create_conversation(kind="default", agent_id=agent_id)

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    pending = prepare_background_session_title(
        coordinator=coordinator,
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "please investigate"},
                    {"type": "input_text", "text": "the authentication timeout"},
                ],
            },
        ),
    )

    assert pending is not None
    assert pending.request == BackgroundTitleRequest(
        session_id=conversation.id,
        prompt="please investigate the authentication timeout",
        agent_id=agent_id,
    )
    assert pending.expected_seed_title == "please investigate the authentication timeout"


async def test_prepare_background_title_from_slash_command(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Review migration plan"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="slash_command",
            data={"kind": "skill", "name": "grill-me", "arguments": "review this plan"},
        ),
    )

    assert pending is not None
    assert pending.request.prompt == "/grill-me review this plan"
    assert pending.expected_seed_title == "/grill-me review this plan"


@pytest.mark.parametrize("excluded_session", ["titled", "child"])
async def test_prepare_background_title_skips_non_initial_sessions(
    db_uri: str,
    excluded_session: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(kind="default")
    conversation = (
        store.create_conversation(kind="default", title="Existing title")
        if excluded_session == "titled"
        else store.create_conversation(
            kind="sub_agent",
            title="researcher:auth",
            parent_conversation_id=parent.id,
        )
    )

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is None


@pytest.mark.parametrize("harness_override", ["pi"])
async def test_prepare_background_title_skips_unsupported_explicit_harnesses(
    db_uri: str,
    harness_override: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(
        title=None,
        agent_id=uuid.uuid4().hex,
    )
    conversation.harness_override = harness_override

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is None


async def test_prepare_background_title_supports_codex_native(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(
        title=None,
        agent_id=uuid.uuid4().hex,
    )
    conversation.harness_override = "codex-native"

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Debug authentication timeout"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is not None
    assert pending.request.harness_override == "codex-native"


async def test_default_seed_wait_allows_slow_native_session_startup(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)

    assert coordinator._seed_wait_seconds == 15.0


async def test_schedule_returns_before_delayed_generator_finishes(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    release = asyncio.Event()

    async def generator(request: BackgroundTitleRequest) -> str:
        assert request.prompt == "please investigate the authentication timeout"
        assert request.harness_override == "claude-sdk"
        assert request.model_override == "claude-sonnet-4-6"
        await release.wait()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    started = time.perf_counter()
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
        harness_override="claude-sdk",
        model_override="claude-sonnet-4-6",
    )
    schedule_elapsed = time.perf_counter() - started

    assert schedule_elapsed < 0.05
    assert store.get_conversation(session_id).title == "Investigate authentication timeout"

    release.set()
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_unsupported_harness_preserves_deterministic_seed(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def unsupported(_request: BackgroundTitleRequest) -> None:
        return None

    coordinator = BackgroundSessionTitleCoordinator(store, unsupported)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
        harness_override="pi",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_generated_title_is_normalized_before_rename(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return '  "Debug   authentication timeout."  \nIgnored explanation'

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_title_normalizer_rejects_empty_and_oversized_output() -> None:
    assert normalize_background_title(None) is None
    assert normalize_background_title("   \n  ") is None
    assert normalize_background_title("x" * 61) is None


async def test_manual_rename_wins_background_title_race(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    release = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        await release.wait()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    store.update_conversation(session_id, title="My manual title")
    release.set()
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "My manual title"


async def test_generator_waits_for_deterministic_seed(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")
    generator_started = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        generator_started.set()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=conversation.id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await asyncio.sleep(0.1)
    assert not generator_started.is_set()

    store.update_conversation(
        conversation.id,
        title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert generator_started.is_set()
    assert store.get_conversation(conversation.id).title == "Debug authentication timeout"


async def test_timeout_preserves_deterministic_title(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        await asyncio.sleep(1)
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        timeout_seconds=0.01,
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_generator_failure_preserves_deterministic_title(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        raise RuntimeError("fake generator failed")

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


class _FakeRunnerResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeRunnerClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> _FakeRunnerResponse:
        assert timeout == 65.0
        self.requests.append((url, json))
        return _FakeRunnerResponse(
            {"status": "generated", "title": "Debug authentication timeout"}
        )


class _FakeRoutedRunner:
    def __init__(self, client: _FakeRunnerClient) -> None:
        self.client = client


class _FakeRunnerRouter:
    def __init__(self, client: _FakeRunnerClient) -> None:
        self.client = client
        self.session_ids: list[str] = []

    def client_for_existing_conversation(self, session_id: str) -> _FakeRoutedRunner:
        self.session_ids.append(session_id)
        return _FakeRoutedRunner(self.client)


async def test_runner_generator_posts_session_configuration() -> None:
    client = _FakeRunnerClient()
    router = _FakeRunnerRouter(client)
    generator = RunnerBackgroundTitleGenerator(router)  # type: ignore[arg-type]

    title = await generator(
        BackgroundTitleRequest(
            session_id="conv_test",
            prompt="please investigate the authentication timeout",
            agent_id="agent_test",
            harness_override="claude-sdk",
            model_override="claude-sonnet-4-6",
        )
    )

    assert title == "Debug authentication timeout"
    assert router.session_ids == ["conv_test"]
    assert client.requests == [
        (
            "/v1/sessions/conv_test/background-title",
            {
                "prompt": "please investigate the authentication timeout",
                "agent_id": "agent_test",
                "harness_override": "claude-sdk",
                "model_override": "claude-sonnet-4-6",
                "sub_agent_name": None,
            },
        )
    ]


async def test_schedule_is_one_shot_per_session(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    calls = 0

    async def generator(_request: BackgroundTitleRequest) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    for _ in range(2):
        coordinator.schedule(
            session_id=session_id,
            prompt="please investigate the authentication timeout",
            expected_seed_title="Investigate authentication timeout",
        )
    await coordinator.wait_for_idle()

    assert calls == 1
    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_generation_concurrency_is_bounded(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_ids = [
        _seed_session(store, f"Investigate authentication timeout {index}") for index in range(3)
    ]
    release = asyncio.Event()
    running = 0
    peak_running = 0
    two_generators_started = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        nonlocal peak_running, running
        running += 1
        peak_running = max(peak_running, running)
        if running == 2:
            two_generators_started.set()
        await release.wait()
        running -= 1
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator, max_concurrency=2)
    for index, session_id in enumerate(session_ids):
        coordinator.schedule(
            session_id=session_id,
            prompt="please investigate the authentication timeout",
            expected_seed_title=f"Investigate authentication timeout {index}",
        )
    await asyncio.wait_for(two_generators_started.wait(), timeout=1.0)
    assert peak_running == 2

    release.set()
    await coordinator.wait_for_idle()

    assert all(
        store.get_conversation(session_id).title == "Debug authentication timeout"
        for session_id in session_ids
    )


async def test_seed_polling_is_bounded_by_generation_slots(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_ids = [store.create_conversation(kind="default").id for _ in range(3)]
    release = asyncio.Event()
    two_waiters_started = asyncio.Event()
    running = 0
    peak_running = 0

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    coordinator = BackgroundSessionTitleCoordinator(store, generator, max_concurrency=2)

    async def wait_for_seed(*, session_id: str, expected_seed_title: str) -> bool:
        del session_id, expected_seed_title
        nonlocal peak_running, running
        running += 1
        peak_running = max(peak_running, running)
        if running == 2:
            two_waiters_started.set()
        await release.wait()
        running -= 1
        return False

    coordinator._wait_for_seed = wait_for_seed  # type: ignore[method-assign]
    for session_id in session_ids:
        coordinator.schedule(
            session_id=session_id,
            prompt="hello",
            expected_seed_title="hello",
        )

    await asyncio.wait_for(two_waiters_started.wait(), timeout=1.0)
    await asyncio.sleep(0.05)
    assert peak_running == 2

    release.set()
    await coordinator.wait_for_idle()


# ─────────────────────────────────────────────────────────────────────
# FORK NAMING + RE-TITLE ON TRAJECTORY DRIFT
# ─────────────────────────────────────────────────────────────────────

from omnigent.entities import NewConversationItem  # noqa: E402
from omnigent.entities.conversation import MessageData  # noqa: E402
from omnigent.server.session_title_extensions import (  # noqa: E402
    TITLE_AUTO_LABEL,
    ForkAwareTitleCoordinator,
    _is_fork_placeholder,
    prepare_session_title,
)
from omnigent.server.session_title_extensions import (  # noqa: E402
    build_retitle_prompt as _build_retitle_prompt,
)


async def _generator_const(_request: BackgroundTitleRequest) -> str:
    return "Generated title"


def test_is_fork_placeholder() -> None:
    from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY

    fork_labels = {FORK_SOURCE_LABEL_KEY: "src"}
    assert _is_fork_placeholder("Fork of Something", fork_labels)
    assert _is_fork_placeholder("Fork of Fork of Something", fork_labels)
    assert not _is_fork_placeholder("A real handwritten title", fork_labels)
    assert not _is_fork_placeholder(None, fork_labels)
    # Without fork lineage the prefix is the user's own words — never a placeholder.
    assert not _is_fork_placeholder("Fork of my auth experiment", {})
    # A human rename opts the fork out permanently.
    assert not _is_fork_placeholder("Fork of Something", {**fork_labels, TITLE_AUTO_LABEL: "0"})


async def test_fork_placeholder_is_eligible_for_renaming(db_uri: str) -> None:
    """A fork born with a 'Fork of ...' title gets titled from its first NEW turn."""
    from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY

    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(kind="default", title="Clone the repo")
    created = store.create_conversation(kind="default", title="Fork of Clone the repo")
    store.set_labels(created.id, {FORK_SOURCE_LABEL_KEY: source.id})
    conv = store.get_conversation(created.id)
    assert conv is not None

    pending = prepare_session_title(
        coordinator=ForkAwareTitleCoordinator(store, _generator_const),
        conversation=conv,
        event=SessionEventInput(
            type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": "now add auth"}]},
        ),
    )

    assert pending is not None
    # The divergence turn is the prompt, and the CAS targets the fork placeholder.
    assert pending.request.prompt == "now add auth"
    assert pending.expected_seed_title == "Fork of Clone the repo"


async def test_real_title_is_never_touched(db_uri: str) -> None:
    """A session with a hand-written title is left alone by the titler."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default", title="My careful title")

    pending = prepare_session_title(
        coordinator=ForkAwareTitleCoordinator(store, _generator_const),
        conversation=conv,
        event=SessionEventInput(
            type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": "keep going"}]},
        ),
    )
    assert pending is None


async def test_note_completed_turn_thresholds_and_provenance() -> None:
    """Only auto titles are counted; re-title triggers at N turns, capped."""
    store = object()  # not used by note_completed_turn
    coord = ForkAwareTitleCoordinator(store, _generator_const)  # type: ignore[arg-type]

    # Human-titled session: never counted, never re-titled.
    assert coord.note_completed_turn("human", title_is_auto=False) is False

    # Auto-titled: counts up, fires exactly at the threshold (default 6).
    fired = [coord.note_completed_turn("auto", title_is_auto=True) for _ in range(6)]
    assert fired == [False, False, False, False, False, True]

    # Simulate the re-title landing (resets clock, bumps count).
    coord._state_for("auto").turns_since_title = 0
    coord._state_for("auto").retitles = 1
    fired2 = [coord.note_completed_turn("auto", title_is_auto=True) for _ in range(6)]
    assert fired2[-1] is True  # second re-title still allowed (cap is 2)

    # At the cap, no further re-titles even after many turns.
    coord._state_for("auto").turns_since_title = 0
    coord._state_for("auto").retitles = 2
    assert all(coord.note_completed_turn("auto", title_is_auto=True) is False for _ in range(20))


async def test_mark_auto_titled_sets_label(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default", title="seed")
    coord = ForkAwareTitleCoordinator(store, _generator_const)

    await coord._mark_auto_titled(conv.id)

    refreshed = store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.labels.get(TITLE_AUTO_LABEL) == "1"


async def test_build_retitle_prompt_spans_the_arc(db_uri: str) -> None:
    """The re-title prompt samples user turns across the session plus the ending."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default", title="Fork of X")
    turns = [
        ("user", "clone the repo"),
        ("assistant", "cloned"),
        ("user", "now wire up the OpenBao secret injector"),  # the divergence
        ("assistant", "injector wired, 16 exports"),
        ("user", "make the container entrypoint part of this PR"),
        ("assistant", "PR opened, fail-closed injector verified end to end"),
    ]
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=f"r{i}",
                data=MessageData(
                    role=role,
                    agent="a" if role == "assistant" else None,
                    content=[{"type": "input_text", "text": text}],
                ),
            )
            for i, (role, text) in enumerate(turns)
        ],
    )

    prompt = _build_retitle_prompt(store, conv.id)
    # Captures the divergence and the outcome, not just the shared opening.
    assert "OpenBao secret injector" in prompt
    assert "container entrypoint" in prompt
    assert "injector verified" in prompt.lower() or "PR opened" in prompt


async def test_noise_turns_are_excluded_from_retitle_prompt(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default", title="Fork of X")
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="r0",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "[System: sub-agent finished]"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="r1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "audit the reverse proxy exposure"}],
                ),
            ),
        ],
    )
    prompt = _build_retitle_prompt(store, conv.id)
    assert "reverse proxy exposure" in prompt
    assert "[System:" not in prompt


# ── Regressions found by adversarial QA (each of these was a real bug) ──


def test_human_title_starting_with_fork_of_is_never_clobbered(db_uri: str) -> None:
    """A user may legitimately NAME a session 'Fork of ...' — that is not a placeholder.

    Regression: matching on the title string alone renamed user-named sessions.
    Real fork lineage (the fork-source label) is now required.
    """
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default", title="Fork of my auth experiment")

    pending = prepare_session_title(
        coordinator=ForkAwareTitleCoordinator(store, _generator_const),
        conversation=conv,
        event=SessionEventInput(
            type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": "keep going"}]},
        ),
    )
    assert pending is None


def test_turn_bookkeeping_is_memory_bounded(db_uri: str) -> None:
    """Regression: per-session turn state grew without bound on a long-lived server."""
    store = SqlAlchemyConversationStore(db_uri)
    coord = ForkAwareTitleCoordinator(store, _generator_const)

    for i in range(5000):
        coord.note_completed_turn(f"session-{i}", title_is_auto=True)

    assert len(coord._title_state) <= 4096


def test_duplicate_idle_for_one_turn_counts_once(db_uri: str) -> None:
    """Regression: every idle publish counted as a turn, so re-titles fired far too soon.

    Sub-agent echoes and retries republish idle for the SAME turn; the turn key
    (response id) collapses them.
    """
    store = SqlAlchemyConversationStore(db_uri)
    coord = ForkAwareTitleCoordinator(store, _generator_const)

    fired = [
        coord.note_completed_turn("s1", title_is_auto=True, turn_key="resp-1") for _ in range(10)
    ]
    assert not any(fired)

    # Distinct turns still advance the counter to the threshold (default 6).
    fired2 = [
        coord.note_completed_turn("s1", title_is_auto=True, turn_key=f"resp-{i}")
        for i in range(2, 8)
    ]
    assert fired2[-1] is True
