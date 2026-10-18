"""``OMNIGENT_DISABLE_SCHEDULER`` keeps a shadow instance from arming timers."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.instance_role import scheduler_disabled_by_env
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
    SqlAlchemyScheduledTaskStore,
)

pytestmark = pytest.mark.asyncio


def _build_app(db_uri: str, tmp_path: Path) -> tuple[FastAPI, SqlAlchemyScheduledTaskStore]:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.create(
        scheduled_task_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, "nightly")),
        name="nightly triage",
        prompt="triage the queue",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_id=None,
        agent_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, "agent-1")),
        timezone="UTC",
    )
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        scheduled_task_store=store,
    )
    return app, store


async def test_flag_set_skips_arming_the_scheduler(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag on there is no live scheduler, but "run now" stays wired."""
    monkeypatch.setenv("OMNIGENT_DISABLE_SCHEDULER", "1")
    app, _ = _build_app(db_uri, tmp_path)
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "scheduled_task_scheduler", None) is None
        assert app.state.scheduled_task_run_now is not None


async def test_flag_unset_arms_the_scheduler(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (unset) behaviour is unchanged: the active task gets a timer."""
    monkeypatch.delenv("OMNIGENT_DISABLE_SCHEDULER", raising=False)
    app, _ = _build_app(db_uri, tmp_path)
    async with app.router.lifespan_context(app):
        scheduler = app.state.scheduled_task_scheduler
        assert scheduler.is_started
        assert scheduler.job_count == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), (" on ", True), ("0", False), ("", False)],
)
async def test_flag_parsing(value: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DISABLE_SCHEDULER", value)
    assert scheduler_disabled_by_env() is expected
