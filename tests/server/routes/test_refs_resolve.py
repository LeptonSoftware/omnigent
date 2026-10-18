"""Tests for the external-reference routes (``/v1/refs``).

The refs router proxies to GitHub / Linear / Jira with server-held
credentials, so every upstream is mocked with respx here; requests to the
test app itself pass through to the ASGI transport untouched.

The contract under test: a reference that resolves comes back as a display
card; one that doesn't — unknown prefix, bad shape, upstream 404, upstream
failure — is ``found: false``, never an endpoint error. Bad shapes must never
reach an upstream at all, since ref strings are interpolated into URLs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.refs_config import (
    GithubRefsConfig,
    JiraRefsConfig,
    LinearRefsConfig,
    RefsConfig,
)
from omnigent.server.routes import refs as refs_module
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

FULL_CONFIG = RefsConfig.build(
    github=GithubRefsConfig(token="gh-token", repos=("acme/alpha", "acme/beta")),
    linear=LinearRefsConfig(api_key="lin-key", workspace="acme", teams=("VIN",)),
    jira=JiraRefsConfig(
        base_url="https://acme.atlassian.net",
        email="bot@acme.test",
        api_token="jira-token",
        projects=("NETW",),
    ),
)


@pytest.fixture(autouse=True)
def _clear_card_cache() -> Iterator[None]:
    """The card cache is module-level; isolate tests from each other."""
    with refs_module._card_cache_lock:
        refs_module._card_cache.clear()
    yield


def _build_app(
    db_uri: str,
    tmp_path: Path,
    refs_config: RefsConfig,
    auth_provider: UnifiedAuthProvider | None = None,
) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        auth_provider=auth_provider,
        refs_config=refs_config,
    )


@pytest_asyncio.fixture()
async def refs_client(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    """Client for a single-user app with every provider configured."""
    transport = httpx.ASGITransport(app=_build_app(db_uri, tmp_path, FULL_CONFIG))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _mock_upstreams() -> respx.MockRouter:
    """A respx router that lets requests to the test app itself through."""
    router = respx.mock(assert_all_called=False)
    router.route(host="test").pass_through()
    return router


def _linear_issue_response(identifier: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "issue": {
                    "identifier": identifier,
                    "title": "Fix the flux capacitor",
                    "url": f"https://linear.app/acme/issue/{identifier}/fix",
                    "updatedAt": "2026-09-14T09:00:00.000Z",
                    "state": {"name": "In Progress", "type": "started"},
                    "assignee": {"displayName": "Nikhil"},
                }
            }
        },
    )


def _github_pr_response(repo: str, number: int, merged: bool) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "number": number,
            "title": "Ship the thing",
            "state": "closed" if merged else "open",
            "draft": False,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "user": {"login": "nsaraf98"},
            "created_at": "2026-09-10T05:00:00Z",
            "closed_at": "2026-09-12T05:00:00Z" if merged else None,
            "pull_request": {"merged_at": "2026-09-12T05:00:00Z" if merged else None},
        },
    )


async def test_config_reports_configured_providers(refs_client: httpx.AsyncClient) -> None:
    """The config endpoint describes routing without leaking credentials."""
    resp = await refs_client.get("/v1/refs/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["github"] == {"repos": ["acme/alpha", "acme/beta"]}
    assert body["linear"] == {"workspace": "acme", "teams": ["VIN"]}
    assert body["jira"] == {
        "base_url": "https://acme.atlassian.net",
        "projects": ["NETW"],
    }
    assert "token" not in resp.text and "jira-token" not in resp.text


async def test_config_absent_when_unconfigured(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """With nothing configured, every provider reads as null (feature off)."""
    transport = httpx.ASGITransport(app=_build_app(db_uri, tmp_path, RefsConfig()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/refs/config")
        assert resp.status_code == 200
        assert resp.json() == {
            "object": "refs.config",
            "github": None,
            "linear": None,
            "jira": None,
        }


async def test_resolve_linear_ticket(refs_client: httpx.AsyncClient) -> None:
    """A configured-prefix ticket resolves through Linear into a card."""
    with _mock_upstreams() as router:
        router.post("https://api.linear.app/graphql").mock(
            return_value=_linear_issue_response("VIN-1732")
        )
        resp = await refs_client.post(
            "/v1/refs/resolve", json={"refs": [{"kind": "ticket", "ref": "VIN-1732"}]}
        )
    assert resp.status_code == 200
    [result] = resp.json()["data"]
    assert result["found"] is True
    [card] = result["cards"]
    assert card["kind"] == "linear"
    assert card["identifier"] == "VIN-1732"
    assert card["title"] == "Fix the flux capacitor"
    assert card["state"] == "In Progress"
    assert card["state_kind"] == "active"
    assert card["assignee"] == "Nikhil"
    assert card["url"] == "https://linear.app/acme/issue/VIN-1732/fix"


async def test_resolve_jira_ticket(refs_client: httpx.AsyncClient) -> None:
    """A Jira-prefix ticket resolves through Jira into a card."""
    with _mock_upstreams() as router:
        route = router.get(
            "https://acme.atlassian.net/rest/api/2/issue/NETW-1481",
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "fields": {
                        "summary": "Roll out fiber ontology",
                        "status": {
                            "name": "To Do",
                            "statusCategory": {"key": "new"},
                        },
                        "assignee": {"displayName": "Ravi"},
                    }
                },
            )
        )
        resp = await refs_client.post(
            "/v1/refs/resolve", json={"refs": [{"kind": "ticket", "ref": "NETW-1481"}]}
        )
    assert resp.status_code == 200
    [result] = resp.json()["data"]
    [card] = result["cards"]
    assert card["kind"] == "jira"
    assert card["state_kind"] == "todo"
    assert card["url"] == "https://acme.atlassian.net/browse/NETW-1481"
    assert route.called


async def test_unknown_ticket_prefix_is_a_quiet_miss(refs_client: httpx.AsyncClient) -> None:
    """A prefix no provider owns never reaches an upstream: SHA-256 stays text."""
    with _mock_upstreams() as router:
        resp = await refs_client.post(
            "/v1/refs/resolve", json={"refs": [{"kind": "ticket", "ref": "SHA-256"}]}
        )
        assert not any(c for c in router.calls if c.request.url.host != "test")
    [result] = resp.json()["data"]
    assert result == {
        "kind": "ticket",
        "ref": "SHA-256",
        "repo": None,
        "found": False,
        "cards": [],
    }


async def test_bare_number_fans_out_across_candidate_repos(
    refs_client: httpx.AsyncClient,
) -> None:
    """`#37` with no repo checks every candidate; each hit becomes a card."""
    with _mock_upstreams() as router:
        router.get("https://api.github.com/repos/acme/alpha/issues/37").mock(
            return_value=_github_pr_response("acme/alpha", 37, merged=True)
        )
        router.get("https://api.github.com/repos/acme/beta/issues/37").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        resp = await refs_client.post(
            "/v1/refs/resolve", json={"refs": [{"kind": "github", "ref": "37"}]}
        )
    [result] = resp.json()["data"]
    assert result["found"] is True
    [card] = result["cards"]
    assert card["kind"] == "github_pr"
    assert card["repo"] == "acme/alpha"
    assert card["state"] == "merged"
    assert card["state_kind"] == "merged"
    assert card["author"] == "nsaraf98"


async def test_repo_qualified_number_checks_only_that_repo(
    refs_client: httpx.AsyncClient,
) -> None:
    """`owner/repo#n` must not fan out to the other candidates."""
    with _mock_upstreams() as router:
        route = router.get("https://api.github.com/repos/acme/beta/issues/5").mock(
            return_value=_github_pr_response("acme/beta", 5, merged=False)
        )
        resp = await refs_client.post(
            "/v1/refs/resolve",
            json={"refs": [{"kind": "github", "ref": "5", "repo": "acme/beta"}]},
        )
        github_calls = [c for c in router.calls if c.request.url.host == "api.github.com"]
    assert route.called and len(github_calls) == 1
    [result] = resp.json()["data"]
    assert result["cards"][0]["state_kind"] == "open"


async def test_malformed_refs_never_reach_upstream(refs_client: httpx.AsyncClient) -> None:
    """Shape validation is the URL-injection guard; nothing malformed egresses."""
    hostile = [
        {"kind": "github", "ref": "37", "repo": "../../../evil"},
        {"kind": "github", "ref": "37", "repo": "acme/.."},
        {"kind": "github", "ref": "not-a-number"},
        {"kind": "commit", "ref": "zzzz-not-hex"},
        {"kind": "branch", "ref": "-rf /"},
        {"kind": "ticket", "ref": "vin-123"},
    ]
    with _mock_upstreams() as router:
        resp = await refs_client.post("/v1/refs/resolve", json={"refs": hostile})
        assert not any(c for c in router.calls if c.request.url.host != "test")
    assert resp.status_code == 200
    assert all(r["found"] is False for r in resp.json()["data"])


async def test_upstream_failure_degrades_to_miss(refs_client: httpx.AsyncClient) -> None:
    """A 500 from a provider is a miss for that ref, not a request error."""
    with _mock_upstreams() as router:
        router.post("https://api.linear.app/graphql").mock(
            return_value=httpx.Response(500, text="boom")
        )
        resp = await refs_client.post(
            "/v1/refs/resolve", json={"refs": [{"kind": "ticket", "ref": "VIN-1"}]}
        )
    assert resp.status_code == 200
    assert resp.json()["data"][0]["found"] is False


async def test_second_resolve_answers_from_cache(refs_client: httpx.AsyncClient) -> None:
    """A repeat of the same ref within the TTL makes no upstream call."""
    with _mock_upstreams() as router:
        route = router.post("https://api.linear.app/graphql").mock(
            return_value=_linear_issue_response("VIN-9")
        )
        for _ in range(2):
            resp = await refs_client.post(
                "/v1/refs/resolve", json={"refs": [{"kind": "ticket", "ref": "VIN-9"}]}
            )
            assert resp.json()["data"][0]["found"] is True
    assert route.call_count == 1


async def test_resolve_commit_and_branch(refs_client: httpx.AsyncClient) -> None:
    """Commit SHAs and branch names resolve to cards in the repo that has them."""
    with _mock_upstreams() as router:
        router.get(url__regex=r"https://api\.github\.com/repos/acme/(alpha|beta)/commits/.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "sha": "6981f917fabc0123456789",
                    "html_url": "https://github.com/acme/alpha/commit/6981f917f",
                    "commit": {
                        "message": "web: fix ten defects\n\nlong body",
                        "author": {"name": "Nikhil", "date": "2026-09-12T04:00:00Z"},
                    },
                    "author": {"login": "nsaraf98"},
                },
            )
        )
        router.get(
            url__regex=r"https://api\.github\.com/repos/acme/(alpha|beta)/branches/.*"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": "nsaraf98/web-rich-refs",
                    "commit": {
                        "sha": "abc123",
                        "commit": {
                            "message": "head commit",
                            "committer": {"date": "2026-09-14T08:00:00Z"},
                        },
                    },
                    "_links": {
                        "html": "https://github.com/acme/alpha/tree/nsaraf98/web-rich-refs"
                    },
                },
            )
        )
        resp = await refs_client.post(
            "/v1/refs/resolve",
            json={
                "refs": [
                    {"kind": "commit", "ref": "6981f917f"},
                    {"kind": "branch", "ref": "nsaraf98/web-rich-refs"},
                ]
            },
        )
    commit_result, branch_result = resp.json()["data"]
    assert commit_result["found"] is True
    assert commit_result["cards"][0]["title"] == "web: fix ten defects"
    assert commit_result["cards"][0]["identifier"] == "6981f917fa"
    assert branch_result["found"] is True
    assert branch_result["cards"][0]["kind"] == "github_branch"


async def test_batch_cap_enforced(refs_client: httpx.AsyncClient) -> None:
    """More than 50 refs in one batch is a validation error."""
    refs = [{"kind": "ticket", "ref": f"VIN-{i}"} for i in range(51)]
    resp = await refs_client.post("/v1/refs/resolve", json={"refs": refs})
    assert resp.status_code == 422


async def test_text_plain_body_rejected(refs_client: httpx.AsyncClient) -> None:
    """CSRF guard: a CORS-simple text/plain body is refused before parsing."""
    resp = await refs_client.post(
        "/v1/refs/resolve",
        content=b'{"refs": [{"kind": "ticket", "ref": "VIN-1"}]}',
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == 415


async def test_multi_user_requires_identity(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """Under header auth, an unidentified request is 401 on both endpoints."""
    app = _build_app(
        db_uri,
        tmp_path,
        FULL_CONFIG,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        config_resp = await client.get("/v1/refs/config")
        resolve_resp = await client.post(
            "/v1/refs/resolve", json={"refs": [{"kind": "ticket", "ref": "VIN-1"}]}
        )
        assert config_resp.status_code == 401
        assert resolve_resp.status_code == 401

        ok = await client.get("/v1/refs/config", headers={"X-Forwarded-Email": "a@b.c"})
        assert ok.status_code == 200
