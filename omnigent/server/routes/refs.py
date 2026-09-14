"""REST API routes for external-reference resolution (``/v1/refs``).

The chat UI turns ticket keys (``VIN-1732``), PR/issue numbers (``#37``,
``owner/repo#37``), commit SHAs and branch names into links with hover cards.
These endpoints are the credential boundary for that feature: the browser
never holds a GitHub/Linear/Jira token — it sends candidate references here
and gets back small display cards for the ones that actually exist.

Design rules:

- **Verification is the linkifier.** The client's detection grammar is loose
  on purpose; a candidate that fails to resolve stays plain text. So a miss is
  a normal, frequent answer — it is ``found: false``, never an error.
- **Best-effort, never a turn-breaker.** Upstream failures degrade to misses;
  the batch endpoint itself only errors on bad requests or missing auth.
- **Everything is cached.** A short-TTL in-process cache keyed by a
  credential fingerprint absorbs re-renders and repeat batches.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from typing import Any, Literal
from urllib.parse import quote

import httpx
from cachetools import TTLCache
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from omnigent.server.auth import AuthProvider
from omnigent.server.refs_config import RefsConfig
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type

_logger = logging.getLogger("omnigent.server.refs")

# Reference shapes accepted from the client. Anything failing these is a miss,
# not an error — and, since path segments are interpolated into upstream URLs,
# they are also the injection guard (httpx resolves `../` dot segments, which
# would silently retarget an authenticated request).
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,7}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_NUMBER_RE = re.compile(r"^\d{1,7}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

# Bare `#n` fans out across the configured candidate repos; cap the fan-out so
# one batch of 50 refs cannot become hundreds of upstream calls.
_MAX_CANDIDATE_REPOS = 5
_MAX_CONCURRENT_UPSTREAM = 10
_UPSTREAM_TIMEOUT = httpx.Timeout(8.0, connect=4.0)

_GITHUB_API = "https://api.github.com"
_LINEAR_GRAPHQL = "https://api.linear.app/graphql"

_LINEAR_ISSUE_QUERY = """
query RefCard($id: String!) {
  issue(id: $id) {
    identifier
    title
    url
    updatedAt
    state { name type }
    assignee { displayName }
  }
}
""".strip()

# Cards are tiny and misses dominate; a few thousand entries covers many busy
# transcripts. TTL is short so a just-merged PR reads as merged within minutes.
_CARD_CACHE_TTL_S = 180
_card_cache: TTLCache[tuple[str, str, str, str], list[RefCard]] = TTLCache(
    maxsize=4096, ttl=_CARD_CACHE_TTL_S
)
# TTLCache is not thread-safe and the event loop shares it with nothing else,
# but keep the lock so a future to_thread caller can't corrupt it.
_card_cache_lock = threading.Lock()


class RefSpec(BaseModel):
    """One reference the client wants resolved."""

    kind: Literal["ticket", "github", "commit", "branch"]
    ref: str = Field(min_length=1, max_length=200)
    repo: str | None = Field(default=None, max_length=200)


class ResolveRefsRequest(BaseModel):
    """Batch resolve request; the cap bounds upstream fan-out per call."""

    refs: list[RefSpec] = Field(min_length=1, max_length=50)


class RefCard(BaseModel):
    """Display card for one resolved reference."""

    kind: str
    url: str
    title: str
    identifier: str | None = None
    repo: str | None = None
    state: str | None = None
    state_kind: str | None = None
    author: str | None = None
    assignee: str | None = None
    date: str | None = None


class RefResult(BaseModel):
    """Outcome for one requested reference.

    ``cards`` can hold several entries: a bare ``#n`` that exists in more than
    one candidate repo returns every match, and the client shows them all
    rather than guessing.
    """

    kind: str
    ref: str
    repo: str | None = None
    found: bool
    cards: list[RefCard] = Field(default_factory=list)


def _credential_fingerprint(config: RefsConfig) -> str:
    """Digest of the configured credentials, so cache entries never leak
    across a credential rotation, without storing any secret.

    :param config: The resolver configuration.
    :returns: A short stable hex digest.
    """
    parts = [
        config.github.token if config.github else "",
        config.linear.api_key if config.linear else "",
        config.jira.api_token if config.jira else "",
    ]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:12]


def _cache_get(key: tuple[str, str, str, str]) -> list[RefCard] | None:
    with _card_cache_lock:
        return _card_cache.get(key)


def _cache_put(key: tuple[str, str, str, str], value: list[RefCard]) -> None:
    with _card_cache_lock:
        _card_cache[key] = value


def _safe_repo(repo: str) -> bool:
    """Whether ``repo`` is a plain ``owner/name`` with no dot-segment games."""
    if not _REPO_RE.fullmatch(repo):
        return False
    owner, name = repo.split("/", 1)
    return set(owner) != {"."} and set(name) != {"."}


def _linear_state_kind(state_type: str | None) -> str | None:
    """Normalize a Linear workflow-state type to the shared card vocabulary."""
    mapping = {
        "triage": "todo",
        "backlog": "todo",
        "unstarted": "todo",
        "started": "active",
        "completed": "done",
        "canceled": "closed",
    }
    return mapping.get(state_type or "")


def _jira_state_kind(category_key: str | None) -> str | None:
    """Normalize a Jira status category to the shared card vocabulary."""
    mapping = {"new": "todo", "indeterminate": "active", "done": "done"}
    return mapping.get(category_key or "")


async def _resolve_linear(client: httpx.AsyncClient, api_key: str, key: str) -> list[RefCard]:
    """Resolve a ticket key against Linear; empty list when it doesn't exist."""
    response = await client.post(
        _LINEAR_GRAPHQL,
        headers={"Authorization": api_key},
        json={"query": _LINEAR_ISSUE_QUERY, "variables": {"id": key}},
    )
    # Linear answers 200 with an `errors` list for unknown issues.
    if response.status_code != 200:
        response.raise_for_status()
    issue = (response.json().get("data") or {}).get("issue")
    if not isinstance(issue, dict):
        return []
    state = issue.get("state") or {}
    assignee = issue.get("assignee") or {}
    return [
        RefCard(
            kind="linear",
            url=str(issue.get("url") or ""),
            title=str(issue.get("title") or ""),
            identifier=str(issue.get("identifier") or key),
            state=state.get("name"),
            state_kind=_linear_state_kind(state.get("type")),
            assignee=assignee.get("displayName"),
            date=issue.get("updatedAt"),
        )
    ]


async def _resolve_jira(
    client: httpx.AsyncClient,
    base_url: str,
    email: str,
    api_token: str,
    key: str,
) -> list[RefCard]:
    """Resolve a ticket key against Jira Cloud; empty list on 404."""
    response = await client.get(
        f"{base_url}/rest/api/2/issue/{quote(key, safe='')}",
        params={"fields": "summary,status,assignee"},
        auth=(email, api_token),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    fields = response.json().get("fields") or {}
    status = fields.get("status") or {}
    category = status.get("statusCategory") or {}
    assignee = fields.get("assignee") or {}
    return [
        RefCard(
            kind="jira",
            url=f"{base_url}/browse/{key}",
            title=str(fields.get("summary") or ""),
            identifier=key,
            state=status.get("name"),
            state_kind=_jira_state_kind(category.get("key")),
            assignee=assignee.get("displayName"),
        )
    ]


async def _resolve_github_issue(
    client: httpx.AsyncClient, token: str, repo: str, number: str
) -> list[RefCard]:
    """Resolve ``repo#number`` via the issues API (it covers PRs too)."""
    response = await client.get(
        f"{_GITHUB_API}/repos/{repo}/issues/{number}",
        headers=_github_headers(token),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    body = response.json()
    pull = body.get("pull_request")
    merged = bool(pull and pull.get("merged_at"))
    state = str(body.get("state") or "")
    if merged:
        state, state_kind = "merged", "merged"
    elif state == "open" and body.get("draft"):
        state, state_kind = "draft", "draft"
    else:
        state_kind = "open" if state == "open" else "closed"
    user = body.get("user") or {}
    return [
        RefCard(
            kind="github_pr" if pull else "github_issue",
            url=str(body.get("html_url") or ""),
            title=str(body.get("title") or ""),
            identifier=f"#{number}",
            repo=repo,
            state=state,
            state_kind=state_kind,
            author=user.get("login"),
            date=body.get("closed_at") or body.get("created_at"),
        )
    ]


async def _resolve_github_commit(
    client: httpx.AsyncClient, token: str, repo: str, sha: str
) -> list[RefCard]:
    """Resolve a commit SHA (prefixes allowed) in one repo."""
    response = await client.get(
        f"{_GITHUB_API}/repos/{repo}/commits/{sha}",
        headers=_github_headers(token),
    )
    # 422 is GitHub's answer for a malformed/unknown-prefix SHA.
    if response.status_code in (404, 422):
        return []
    response.raise_for_status()
    body = response.json()
    commit = body.get("commit") or {}
    commit_author = commit.get("author") or {}
    author = body.get("author") or {}
    message = str(commit.get("message") or "")
    return [
        RefCard(
            kind="github_commit",
            url=str(body.get("html_url") or ""),
            title=message.split("\n", 1)[0],
            identifier=str(body.get("sha") or sha)[:10],
            repo=repo,
            author=author.get("login") or commit_author.get("name"),
            date=commit_author.get("date"),
        )
    ]


async def _resolve_github_branch(
    client: httpx.AsyncClient, token: str, repo: str, branch: str
) -> list[RefCard]:
    """Resolve a branch name in one repo."""
    response = await client.get(
        f"{_GITHUB_API}/repos/{repo}/branches/{quote(branch, safe='')}",
        headers=_github_headers(token),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    body = response.json()
    head = body.get("commit") or {}
    head_commit = head.get("commit") or {}
    committer = head_commit.get("committer") or {}
    links = body.get("_links") or {}
    return [
        RefCard(
            kind="github_branch",
            url=str(links.get("html") or f"https://github.com/{repo}/tree/{quote(branch)}"),
            title=str(head_commit.get("message") or "").split("\n", 1)[0],
            identifier=branch,
            repo=repo,
            date=committer.get("date"),
        )
    ]


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def create_refs_router(
    refs_config: RefsConfig,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the refs router (``/v1/refs``).

    :param refs_config: Provider credentials and routing; may be fully empty,
        in which case every resolve is a miss and the config reads as absent.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()
    fingerprint = _credential_fingerprint(refs_config)

    async def _cached(
        kind: str,
        repo: str,
        ref: str,
        fetch: Any,
    ) -> list[RefCard]:
        """Answer from cache, else fetch and cache; failures degrade to a miss."""
        key = (fingerprint, kind, repo, ref)
        hit = _cache_get(key)
        if hit is not None:
            return hit
        try:
            cards = await fetch()
        except Exception:  # noqa: BLE001 - a card is best-effort metadata
            _logger.warning("ref resolve failed kind=%s repo=%s ref=%s", kind, repo, ref)
            cards = []
        _cache_put(key, cards)
        return cards

    async def _resolve_ticket(client: httpx.AsyncClient, ref: str) -> list[RefCard]:
        if not _TICKET_RE.fullmatch(ref):
            return []
        provider = refs_config.ticket_prefixes.get(ref.split("-", 1)[0])
        if provider == "linear" and refs_config.linear is not None:
            linear = refs_config.linear
            return await _cached(
                "ticket", "", ref, lambda: _resolve_linear(client, linear.api_key, ref)
            )
        if provider == "jira" and refs_config.jira is not None:
            jira = refs_config.jira
            return await _cached(
                "ticket",
                "",
                ref,
                lambda: _resolve_jira(client, jira.base_url, jira.email, jira.api_token, ref),
            )
        return []

    async def _resolve_in_repos(
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        kind: str,
        ref: str,
        repo: str | None,
        valid: re.Pattern[str],
        fetch_one: Any,
    ) -> list[RefCard]:
        """Resolve a repo-scoped ref in its repo, or across the candidates."""
        if refs_config.github is None or not valid.fullmatch(ref):
            return []
        repos = [repo] if repo else list(refs_config.github.repos[:_MAX_CANDIDATE_REPOS])
        repos = [r for r in repos if r and _safe_repo(r)]
        if not repos:
            return []
        token = refs_config.github.token

        async def one(candidate: str) -> list[RefCard]:
            async with semaphore:
                return await _cached(
                    kind, candidate, ref, lambda: fetch_one(client, token, candidate, ref)
                )

        found = await asyncio.gather(*(one(r) for r in repos))
        return [card for cards in found for card in cards]

    @router.get("/refs/config")
    async def get_refs_config(request: Request) -> dict[str, Any]:
        """Describe which reference kinds this server can resolve.

        The web UI shapes its linkification grammar from this: which ticket
        prefixes are live, where their issues live, and whether bare ``#n``
        has candidate repos to resolve against. Credentials never appear.

        :param request: The incoming request, used to identify the user.
        :returns: Per-provider routing info, ``None`` for absent providers.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode.
        """
        require_user(request, auth_provider)
        return {
            "object": "refs.config",
            "github": ({"repos": list(refs_config.github.repos)} if refs_config.github else None),
            "linear": (
                {
                    "workspace": refs_config.linear.workspace,
                    "teams": list(refs_config.linear.teams),
                }
                if refs_config.linear
                else None
            ),
            "jira": (
                {
                    "base_url": refs_config.jira.base_url,
                    "projects": list(refs_config.jira.projects),
                }
                if refs_config.jira
                else None
            ),
        }

    @router.post(
        "/refs/resolve",
        dependencies=[Depends(require_json_content_type)],
    )
    async def resolve_refs(request: Request, body: ResolveRefsRequest) -> dict[str, Any]:
        """Resolve a batch of references to display cards.

        Every requested ref gets a result; unresolvable ones come back
        ``found: false``. Bare ``#n`` may return several cards (one per
        candidate repo that has it).

        :param request: The incoming request, used to identify the user.
        :param body: The references to resolve (at most 50).
        :returns: ``{"object": "list", "data": [RefResult, ...]}``.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode.
        """
        require_user(request, auth_provider)
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_UPSTREAM)

        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as client:

            async def resolve_one(spec: RefSpec) -> RefResult:
                if spec.kind == "ticket":
                    cards = await _resolve_ticket(client, spec.ref)
                elif spec.kind == "github":
                    cards = await _resolve_in_repos(
                        client,
                        semaphore,
                        "github",
                        spec.ref,
                        spec.repo,
                        _NUMBER_RE,
                        _resolve_github_issue,
                    )
                elif spec.kind == "commit":
                    cards = await _resolve_in_repos(
                        client,
                        semaphore,
                        "commit",
                        spec.ref.lower(),
                        spec.repo,
                        _SHA_RE,
                        _resolve_github_commit,
                    )
                else:
                    cards = await _resolve_in_repos(
                        client,
                        semaphore,
                        "branch",
                        spec.ref,
                        spec.repo,
                        _BRANCH_RE,
                        _resolve_github_branch,
                    )
                return RefResult(
                    kind=spec.kind,
                    ref=spec.ref,
                    repo=spec.repo,
                    found=bool(cards),
                    cards=cards,
                )

            data = await asyncio.gather(*(resolve_one(spec) for spec in body.refs))

        return {"object": "list", "data": [result.model_dump() for result in data]}

    return router
