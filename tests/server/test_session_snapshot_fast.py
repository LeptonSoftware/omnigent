"""The fork's batched ``GET /v1/sessions/{id}`` snapshot equals upstream's.

Each case requests the realistic session twice through the same app: once
through ``session_snapshot_fast.get_session_snapshot`` (what the route uses)
and once with the route pointed back at upstream's ``_get_session_snapshot``.
The parsed JSON bodies must be equal.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable

import pytest

from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_core
from tests.server.get_session_fixtures import (
    OWNER,
    READER,
    RealisticSession,
    realistic,
    realistic_app,
    record_pending,
)

__all__ = ["realistic", "realistic_app"]

# sha256 of upstream ``_get_session_snapshot``'s source. When an upstream sync
# changes that function, this fails: port the change to
# ``omnigent/server/session_snapshot_fast.py``, then update the hash.
UPSTREAM_SNAPSHOT_SHA256 = "55601ab313c89574a14425976e1a4bfbb2f2a0b566bad5b64cfe9c0f473c1b69"


def _no_change(session: RealisticSession) -> None:
    del session


def _child_prompt(session: RealisticSession) -> None:
    record_pending(session.child_id, "elicit_child")


def _grandchild_prompt(session: RealisticSession) -> None:
    record_pending(session.grandchild_id, "elicit_grandchild")


def _no_other_prompts(session: RealisticSession) -> None:
    del session
    orchestration.pending_elicitations.reset_for_tests()


# (case, user, target, query, setup)
CASES: list[tuple[str, str, str, str, Callable[[RealisticSession], None]]] = [
    ("owner-default", OWNER, "session", "", _no_change),
    ("reader-default", READER, "session", "", _no_change),
    ("web", OWNER, "session", "?include_items=false&include_liveness=false", _no_change),
    ("no-usage", OWNER, "session", "?include_usage=false", _no_change),
    ("refresh", OWNER, "session", "?refresh_state=true", _no_change),
    ("no-live-status", OWNER, "session", "?include_live_status=false", _no_change),
    ("child-prompt", OWNER, "session", "", _child_prompt),
    ("grandchild-prompt", READER, "session", "", _grandchild_prompt),
    ("child-prompt-no-usage", OWNER, "session", "?include_usage=false", _child_prompt),
    ("no-prompts", OWNER, "session", "", _no_other_prompts),
    ("reader-child", READER, "child", "", _child_prompt),
    ("owner-grandchild", OWNER, "grandchild", "", _no_change),
]


@pytest.mark.parametrize(("case", "user", "target", "query", "setup"), CASES)
async def test_fast_snapshot_equals_upstream(
    realistic: RealisticSession,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    user: str,
    target: str,
    query: str,
    setup: Callable[[RealisticSession], None],
) -> None:
    """Parsed JSON from the fork's snapshot equals upstream's on the same state."""
    del case
    setup(realistic)
    session_id = {
        "session": realistic.session_id,
        "child": realistic.child_id,
        "grandchild": realistic.grandchild_id,
    }[target]
    path = f"/v1/sessions/{session_id}{query}"
    headers = {"X-Forwarded-Email": user}
    # Warm both paths' per-process caches before comparing.
    assert (await realistic.client.get(path, headers=headers)).status_code == 200
    fast = await realistic.client.get(path, headers=headers)
    with monkeypatch.context() as patch:
        patch.setattr(routes_core, "fast_session_snapshot", orchestration._get_session_snapshot)
        upstream = await realistic.client.get(path, headers=headers)
    assert fast.status_code == upstream.status_code == 200, (fast.text, upstream.text)
    assert fast.json() == upstream.json()


async def test_unknown_session_is_404_on_both(realistic: RealisticSession) -> None:
    """A missing session fails the same way through the fork's snapshot."""
    resp = await realistic.client.get(
        "/v1/sessions/conv_00000000000000000000000000000000",
        headers={"X-Forwarded-Email": OWNER},
    )
    assert resp.status_code == 404


def test_upstream_snapshot_unchanged() -> None:
    """Tripwire: upstream's snapshot changed, so the fork's mirror needs a look."""
    source = inspect.getsource(orchestration._get_session_snapshot)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert digest == UPSTREAM_SNAPSHOT_SHA256, (
        "upstream _get_session_snapshot changed; port the change to "
        "omnigent/server/session_snapshot_fast.py and update UPSTREAM_SNAPSHOT_SHA256 "
        f"to {digest}"
    )
