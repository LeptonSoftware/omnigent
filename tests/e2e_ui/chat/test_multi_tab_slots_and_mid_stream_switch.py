"""E2E: origin-wide stream slots across two tabs, and switching mid-turn.

Two behaviours the chat-switch-perf work reasoned about but never measured in
a real browser.

**1. Stream slots are origin-wide**, coordinated through ``navigator.locks``
(``web/src/store/streamSlots.ts``). Every downstream claim — a second tab
cannot silently tear down the first tab's live stream, a tab that finds the
origin saturated degrades to a warning instead of breaking — rests on the lock
manager genuinely being shared between tabs of one browser profile. That
premise is asserted first, because every other assertion here is vacuous
without it.

**2. Switching conversations while a turn is still running.** The registry
keeps the outgoing conversation's SSE stream open, so a turn in flight when
the reader leaves must keep running, apply its events to the background entry,
and be on screen when they come back — exactly once, with no refetch, and
never bleeding into the conversation they switched to.

The spawned test server speaks HTTP/1.1, so ``maxLiveConversations()`` is 3
(the HTTP/2 value is 30). Three slots is what puts saturation in reach here.

Session switches go through the in-app sidebar link, never ``page.goto`` — a
reload drops every stream and dissolves the scenario under test.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
# Substring of StreamBudgetBanner's copy — the over-budget tab's only
# user-visible signal.
_BANNER = "You have a lot of conversations open across your tabs"

# Probe lock name, deliberately outside the `omnigent:stream-slot:` namespace
# so the premise check can never steal a slot the app is using.
_PROBE_LOCK = "omnigent-test-probe:slot"

# Hold a lock until `window.__releaseProbe()` runs. Resolves once GRANTED: the
# callback's promise stays pending, which is what keeps the lock held.
_HOLD_LOCK = """
(name) => new Promise((granted) => {
  navigator.locks.request(name, (lock) => new Promise((release) => {
    window.__releaseProbe = release;
    granted(true);
  }));
})
"""

# True = this agent was granted the lock (nobody else holds it).
_TRY_LOCK = """
(name) => navigator.locks.request(name, { ifAvailable: true }, (lock) => lock !== null)
"""

# Record every /stream and /items request, in order. A torn-down stream shows
# up here as a SECOND /stream request for the same session (the reconnect).
_TRACK_REQUESTS = """
(() => {
  window.__streamUrls = [];
  window.__itemsUrls = [];
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (/\\/v1\\/sessions\\/[^/]+\\/stream/.test(url)) window.__streamUrls.push(url);
    if (/\\/v1\\/sessions\\/[^/]+\\/items/.test(url)) window.__itemsUrls.push(url);
    return originalFetch(input, init);
  };
})();
"""


def _count_for(page: Page, bucket: str, session_id: str) -> int:
    """How many recorded requests in *bucket* addressed *session_id*."""
    urls: list[str] = page.evaluate(f"window.{bucket} || []")
    return sum(1 for u in urls if session_id in u)


def _open_chat(page: Page, base_url: str, session_id: str) -> None:
    """Full page load onto a conversation, settled enough to act on."""
    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)


def _switch_to(page: Page, session_id: str) -> None:
    """Switch conversations the way a user does — client-side nav, no reload."""
    page.locator(f'a[href="/c/{session_id}"]').click()
    page.wait_for_url(re.compile(rf"/c/{re.escape(session_id)}"))


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and send it."""
    page.get_by_label("Message the agent").fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.fixture
def four_sessions(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, list[str]]]:
    """Four runner-bound sessions — one more than the HTTP/1.1 slot budget.

    Three is the whole origin's budget on this transport, so a fourth live
    conversation is what forces a tab over it.

    :param live_server: Spawned server fixture.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, [session_id, ...])`` with four ids.
    """
    from tests.e2e_ui.conftest import (
        _create_runner_bound_session,
        _ensure_runner_online,
        _server_state,
    )

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    ids = [_create_runner_bound_session(live_server, runner_id) for _ in range(4)]
    try:
        yield (live_server, ids)
    finally:
        for sid in ids:
            httpx.delete(f"{live_server}/v1/sessions/{sid}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)


def test_stream_slot_locks_are_shared_between_tabs_of_one_profile(
    page: Page,
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Web Locks span tabs of one profile, and stop at a profile boundary.

    The premise under `streamSlots`: a lock held in one tab is visible as held
    in every other same-origin tab of the same profile, which is what makes the
    budget origin-wide rather than per-tab. The profile boundary is asserted too
    because it is what separates "another tab of mine" (coordinated) from
    "a different browser" (not) — and it is why the suite's other two-tab tests,
    which use two browser contexts, do NOT exercise slot sharing.
    """
    base_url, session_id = seeded_session
    _open_chat(page, base_url, session_id)

    same_profile_tab = page.context.new_page()
    other_profile = browser.new_context()
    try:
        same_profile_tab.goto(f"{base_url}/c/{session_id}")
        expect(same_profile_tab.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

        # Nobody holds it yet, in either place.
        assert same_profile_tab.evaluate(_TRY_LOCK, _PROBE_LOCK) is True

        page.evaluate(_HOLD_LOCK, _PROBE_LOCK)

        # The sharing the design depends on: the second tab sees it held.
        assert same_profile_tab.evaluate(_TRY_LOCK, _PROBE_LOCK) is False, (
            "a lock held in one tab must be visible as held in another tab of the "
            "same profile — without this the stream budget is per-tab, not origin-wide"
        )

        # And the boundary: a separate profile coordinates with neither.
        other_tab = other_profile.new_page()
        other_tab.goto(f"{base_url}/c/{session_id}")
        expect(other_tab.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
        assert other_tab.evaluate(_TRY_LOCK, _PROBE_LOCK) is True, (
            "a separate browser profile must not see this profile's locks"
        )

        # Releasing hands it straight back to the other tab.
        page.evaluate("() => window.__releaseProbe && window.__releaseProbe()")
        assert same_profile_tab.evaluate(_TRY_LOCK, _PROBE_LOCK) is True
    finally:
        other_profile.close()
        same_profile_tab.close()


def test_a_saturating_second_tab_leaves_the_first_tabs_stream_alone(
    page: Page,
    four_sessions: tuple[str, list[str]],
    mock_llm_server_url: str,
) -> None:
    """A second tab taking the rest of the budget must not disturb tab A.

    Tab A holds the conversation the user is reading; tab B then goes live on
    two more, taking the origin to its cap. Tab A's stream must neither be torn
    down (which would show up as a reconnect — a second ``/stream`` request) nor
    stop delivering, and A must not be blamed for another tab's pressure with a
    banner it cannot act on.
    """
    base_url, ids = four_sessions
    marker = f"tab-a-alive-{uuid.uuid4().hex[:8]}"
    prompt = f"probe-tab-a-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url, [{"text": marker}], key=f"tab-a-{marker}", match=prompt
    )

    page.add_init_script(_TRACK_REQUESTS)
    _open_chat(page, base_url, ids[0])
    assert _count_for(page, "__streamUrls", ids[0]) == 1

    tab_b = page.context.new_page()
    try:
        tab_b.set_viewport_size({"width": 1280, "height": 720})
        tab_b.goto(f"{base_url}/c/{ids[1]}")
        expect(tab_b.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
        # A switch keeps the outgoing stream open, so B now holds two slots —
        # with tab A's, that is the whole budget.
        _switch_to(tab_b, ids[2])
        expect(tab_b.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

        # Tab A still works: the reply can only reach it over its own stream.
        _send(page, prompt)
        expect(page.locator(_ASSISTANT).filter(has_text=marker)).to_be_visible(timeout=90_000)

        # And it was the SAME stream throughout — no eviction, no reconnect.
        assert _count_for(page, "__streamUrls", ids[0]) == 1, (
            "tab A's stream was re-opened, so something tore the original down"
        )
        # The banner is a statement about the user's own tabs being over budget;
        # tab A got its slot first and is not over anything.
        expect(page.get_by_text(_BANNER, exact=False)).to_have_count(0)
    finally:
        tab_b.close()


def test_a_tab_that_opens_over_budget_warns_instead_of_breaking(
    page: Page,
    four_sessions: tuple[str, list[str]],
) -> None:
    """The fourth live conversation degrades to a warning, not a dead tab.

    Tab A goes live on all three slots, so tab B's open finds the origin
    saturated with streams it may not evict (they are another tab's). B must
    still open and render its conversation — over budget, with the banner
    explaining why — rather than hanging on an unobtainable slot.
    """
    base_url, ids = four_sessions
    _open_chat(page, base_url, ids[0])
    # Two in-app switches, so tab A holds three live conversations = every slot.
    for sid in (ids[1], ids[2]):
        _switch_to(page, sid)
        expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    tab_b = page.context.new_page()
    try:
        tab_b.set_viewport_size({"width": 1280, "height": 720})
        tab_b.goto(f"{base_url}/c/{ids[3]}")

        # Usable first — the degradation has to be graceful.
        expect(tab_b.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
        # Then the explanation.
        expect(tab_b.get_by_text(_BANNER, exact=False)).to_be_visible(timeout=30_000)

        # Dismissing it sticks for this episode.
        tab_b.get_by_role("button", name="Dismiss").click()
        expect(tab_b.get_by_text(_BANNER, exact=False)).to_have_count(0)

        # The tab that was within budget all along stays quiet.
        expect(page.get_by_text(_BANNER, exact=False)).to_have_count(0)
    finally:
        tab_b.close()


def test_switching_away_mid_turn_lands_the_reply_on_return(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """A turn in flight survives the switch away and is on screen on return.

    The mock LLM parks the turn on a gate, so it is genuinely mid-flight — not
    a race against a turn that finishes in under a second. The reply is then
    released while the reader is in ANOTHER conversation, which is the case the
    background-stream design exists for and the one never exercised: the events
    have to land on the backgrounded entry, stay out of the conversation on
    screen, and be rendered (not hidden behind the history render window) when
    the reader switches back — without refetching history to find them.
    """
    base_url, session_a, session_b = seeded_session_pair
    marker = f"mid-stream-reply-{uuid.uuid4().hex[:8]}"
    prompt = f"park-this-turn-{uuid.uuid4().hex[:8]}"
    # `block` holds the LLM call open until /gate/release, so the turn is
    # running with nothing to show yet.
    configure_mock_llm(
        mock_llm_server_url,
        [{"block": True, "text": marker}],
        key=f"mid-stream-{marker}",
        match=prompt,
    )

    page.add_init_script(_TRACK_REQUESTS)
    _open_chat(page, base_url, session_a)
    _send(page, prompt)

    # The turn is live and parked in the LLM call.
    expect(page.locator(_WORKING)).to_be_visible(timeout=60_000)
    gate = httpx.Client(base_url=mock_llm_server_url, timeout=10.0)
    try:
        for _ in range(300):
            if gate.get("/gate/pending").json().get("pending") is True:
                break
            page.wait_for_timeout(100)
        else:
            raise AssertionError("the turn never reached the mock LLM's gate")

        # Leave mid-turn, the way a user does.
        _switch_to(page, session_b)
        expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
        items_before_return = _count_for(page, "__itemsUrls", session_a)

        # Now let A's reply arrive — while the reader is looking at B.
        gate.post("/gate/release")

        # It must land in A, not in the conversation on screen.
        page.wait_for_timeout(3_000)
        expect(page.get_by_text(marker, exact=False)).to_have_count(0)

        # Back to A: the reply that arrived while away is simply there.
        _switch_to(page, session_a)
        expect(page.locator(_ASSISTANT).filter(has_text=marker)).to_be_visible(timeout=60_000)
        # Exactly once — a stream applied twice, or re-hydrated on top of
        # itself, shows up as a duplicate bubble.
        expect(page.locator(_ASSISTANT).filter(has_text=marker)).to_have_count(1)
        # The turn finished while backgrounded, so A is idle on return.
        expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
        # And returning cost no history fetch: the entry stayed live.
        assert _count_for(page, "__itemsUrls", session_a) == items_before_return, (
            "switching back re-fetched history for a conversation that was still live"
        )
    finally:
        gate.close()
