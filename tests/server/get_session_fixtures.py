"""A realistic session for ``GET /v1/sessions/{id}`` tests, and a SQL counter.

The session carries what production sessions carry: labels, a reader grant,
twelve items, sub-agent children (one nested, one archived) with usage, a host
binding, a bound runner, and pending elicitations on itself and elsewhere.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_READ, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import _app_client
from tests.server.helpers import create_test_agent

OWNER = "owner@example.com"
READER = "reader@example.com"
HOST_ID = "8f3c2a1b9d4e4f6a8b7c6d5e4f3a2b1c"
RUNNER_ID = "0a1b2c3d4e5f40718293a4b5c6d7e8f9"


@dataclass
class RealisticSession:
    """The session, its relatives, and the client and stores behind them."""

    client: httpx.AsyncClient
    conversations: SqlAlchemyConversationStore
    session_id: str
    child_id: str
    grandchild_id: str
    unrelated_id: str


@dataclass
class SqlCounter:
    """SQL statements and pool checkouts seen while armed, on any engine."""

    statements: list[str] = field(default_factory=list)
    checkouts: int = 0
    armed: bool = False

    def on_statement(self, conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        del conn, cursor, args
        if self.armed:
            self.statements.append(" ".join(statement.split()))

    def on_checkout(self, *args: Any) -> None:
        del args
        if self.armed:
            self.checkouts += 1

    @property
    def queries(self) -> list[str]:
        """Statements minus SQLite's per-session PRAGMA setup."""
        return [s for s in self.statements if not s.startswith("PRAGMA")]

    def reset(self) -> None:
        self.statements.clear()
        self.checkouts = 0


@pytest.fixture()
def sql_counter() -> Iterator[SqlCounter]:
    """Global listeners, removed after the test."""
    counter = SqlCounter()
    event.listen(Engine, "before_cursor_execute", counter.on_statement)
    event.listen(Pool, "checkout", counter.on_checkout)
    try:
        yield counter
    finally:
        event.remove(Engine, "before_cursor_execute", counter.on_statement)
        event.remove(Pool, "checkout", counter.on_checkout)


@pytest.fixture()
def realistic_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """The full app with every store a get_session snapshot reads."""
    del runtime_init
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        host_store=HostStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


def _message(n: int) -> NewConversationItem:
    user = n % 2 == 0
    return NewConversationItem(
        type="message",
        response_id=f"resp_{n}",
        data=MessageData(
            role="user" if user else "assistant",
            agent=None if user else "test-agent",
            content=[{"type": "input_text" if user else "output_text", "text": f"m{n}"}],
        ),
    )


async def build_realistic_session(client: httpx.AsyncClient, db_uri: str) -> RealisticSession:
    """Create the session through the API, then decorate it through the stores."""
    conversations = SqlAlchemyConversationStore(db_uri)
    agent = await create_test_agent(client, user=OWNER, sub_agents=[{"name": "worker"}])
    session_id = agent["_session_id"]
    unrelated = await create_test_agent(client, name="other-agent", user=OWNER)

    conversations.set_labels(session_id, {"project": "roundtrips", "env": "test"})
    conversations.append(session_id, [_message(n) for n in range(12)])
    conversations.set_session_usage(session_id, {"total_cost_usd": 1.5, "total_tokens": 900})

    def sub_agent(parent_id: str, cost: float) -> str:
        child = conversations.create_conversation(
            agent_id=agent["id"],
            kind="sub_agent",
            parent_conversation_id=parent_id,
            sub_agent_name="worker",
        )
        conversations.set_session_usage(child.id, {"total_cost_usd": cost, "total_tokens": 10})
        return child.id

    child_id = sub_agent(session_id, 0.25)
    grandchild_id = sub_agent(child_id, 0.05)
    archived_id = sub_agent(session_id, 0.5)
    conversations.update_conversation(archived_id, archived=True)

    SqlAlchemyPermissionStore(db_uri).grant(READER, session_id, LEVEL_READ)
    HostStore(db_uri).upsert_on_connect(HOST_ID, "roundtrip-box", OWNER)
    conversations.set_host_id(session_id, HOST_ID, workspace="/work")
    conversations.set_runner_id(session_id, RUNNER_ID)

    pending_elicitations.reset_for_tests()
    record_pending(session_id, "elicit_parent")
    # Some other session somewhere has a prompt open: the common case on a
    # shared server, and what used to trigger the descendant walk every time.
    record_pending(unrelated["_session_id"], "elicit_unrelated")
    return RealisticSession(
        client=client,
        conversations=conversations,
        session_id=session_id,
        child_id=child_id,
        grandchild_id=grandchild_id,
        unrelated_id=unrelated["_session_id"],
    )


def record_pending(session_id: str, elicitation_id: str) -> None:
    """Register an outstanding approval prompt the way the SSE publish does."""
    pending_elicitations.record_publish(
        session_id,
        {"type": "response.elicitation_request", "elicitation_id": elicitation_id},
    )


@pytest_asyncio.fixture()
async def realistic(
    realistic_app: FastAPI, db_uri: str, mock_llm: Any, tmp_path: Path
) -> AsyncIterator[RealisticSession]:
    """The realistic session behind a live in-process client."""
    async with _app_client(realistic_app, mock_llm, tmp_path) as client:
        session = await build_realistic_session(client, db_uri)
        try:
            yield session
        finally:
            pending_elicitations.reset_for_tests()
