"""``X-Omnigent-Principal`` names the authenticated caller on responses."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import principal_header
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_EMAIL = "owner@example.test"


@pytest.fixture(autouse=True)
def _restore() -> Iterator[None]:
    yield
    principal_header.uninstall()


def _app(db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture
async def client(runtime_init: None, db_uri: str, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = _app(db_uri, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.background_title_coordinator.shutdown()


@pytest.mark.asyncio
async def test_authenticated_response_names_the_caller(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions", headers={"X-Forwarded-Email": _EMAIL})
    assert resp.status_code == 200
    assert resp.headers["x-omnigent-principal"] == _EMAIL


@pytest.mark.asyncio
async def test_anonymous_and_health_responses_carry_no_principal(client: httpx.AsyncClient) -> None:
    unauth = await client.get("/v1/sessions")
    assert unauth.status_code == 401
    assert "x-omnigent-principal" not in unauth.headers
    health = await client.get("/health")
    assert "x-omnigent-principal" not in health.headers


@pytest.mark.asyncio
async def test_principal_never_bleeds_between_requests(client: httpx.AsyncClient) -> None:
    await client.get("/v1/sessions", headers={"X-Forwarded-Email": _EMAIL})
    resp = await client.get("/v1/sessions")
    assert "x-omnigent-principal" not in resp.headers


@pytest.mark.asyncio
async def test_env_switch_disables_header(
    monkeypatch: pytest.MonkeyPatch, runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    monkeypatch.setenv(principal_header.PRINCIPAL_HEADER_ENV, "0")
    app = _app(db_uri, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/v1/sessions", headers={"X-Forwarded-Email": _EMAIL})
    await app.state.background_title_coordinator.shutdown()
    assert resp.status_code == 200
    assert "x-omnigent-principal" not in resp.headers


@pytest.mark.asyncio
async def test_inner_scope_copy_still_reports_and_odd_ids_are_sanitised() -> None:
    principal_header.install()
    provider = UnifiedAuthProvider(source="header", local_single_user=False)

    async def endpoint(request: Request) -> PlainTextResponse:
        provider.get_user_id(request)
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/", endpoint)])

    async def copying_layer(scope, receive, send):  # type: ignore[no-untyped-def]
        await inner(dict(scope), receive, send)

    app = principal_header.PrincipalHeaderMiddleware(copying_layer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/", headers={"X-Forwarded-Email": "naïve user@x".encode("utf-8")})
    assert resp.headers["x-omnigent-principal"] == "na??ve?user@x"
