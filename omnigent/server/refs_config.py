"""Configuration for the external-reference resolver (``/v1/refs``).

The chat UI linkifies ticket keys (``VIN-1732``), pull-request numbers
(``#37``), commit SHAs and branch names, and shows hover cards for them. The
server resolves those references against GitHub, Linear and Jira with
credentials that live here — in server env vars, never in the browser.

Each provider is independently optional: with no env vars set the endpoints
still mount, ``GET /v1/refs/config`` reports every provider as absent, and the
web UI simply leaves references as plain text.

Env vars:

- ``OMNIGENT_REFS_GITHUB_TOKEN`` — token with read access to the repos below.
- ``OMNIGENT_REFS_GITHUB_REPOS`` — comma-separated ``owner/repo`` list, in
  priority order; bare ``#n`` references resolve against these candidates.
- ``OMNIGENT_REFS_LINEAR_API_KEY`` — Linear personal API key.
- ``OMNIGENT_REFS_LINEAR_WORKSPACE`` — workspace slug for issue URLs
  (``https://linear.app/<slug>/issue/KEY-1``).
- ``OMNIGENT_REFS_LINEAR_TEAMS`` — comma-separated team key prefixes
  (e.g. ``VIN``); only keys with these prefixes resolve via Linear.
- ``OMNIGENT_REFS_JIRA_BASE_URL`` — e.g. ``https://acme.atlassian.net``.
- ``OMNIGENT_REFS_JIRA_EMAIL`` / ``OMNIGENT_REFS_JIRA_API_TOKEN`` — basic-auth
  credential pair for the Jira Cloud REST API.
- ``OMNIGENT_REFS_JIRA_PROJECTS`` — comma-separated project key prefixes
  (e.g. ``NETW,NET,TRF``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(name: str) -> tuple[str, ...]:
    """Parse a comma-separated env var into a tuple, dropping blanks.

    :param name: The environment variable name.
    :returns: The trimmed, non-empty items, in order.
    """
    raw = os.environ.get(name) or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _opt(name: str) -> str | None:
    """Read an optional env var, treating empty/whitespace as unset.

    Docker compose ``${VAR:-}`` forwards set-to-empty, so ``""`` means absent.

    :param name: The environment variable name.
    :returns: The trimmed value, or ``None`` when unset or blank.
    """
    value = (os.environ.get(name) or "").strip()
    return value or None


@dataclass(frozen=True)
class GithubRefsConfig:
    """GitHub resolver credentials and the candidate repos for bare ``#n``."""

    token: str
    repos: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinearRefsConfig:
    """Linear resolver credentials plus the key prefixes it owns."""

    api_key: str
    workspace: str
    teams: tuple[str, ...] = ()


@dataclass(frozen=True)
class JiraRefsConfig:
    """Jira resolver credentials plus the project key prefixes it owns."""

    base_url: str
    email: str
    api_token: str
    projects: tuple[str, ...] = ()


@dataclass(frozen=True)
class RefsConfig:
    """The full external-reference resolver configuration.

    Providers are ``None`` when unconfigured; :meth:`enabled` is ``False``
    when every provider is absent.
    """

    github: GithubRefsConfig | None = None
    linear: LinearRefsConfig | None = None
    jira: JiraRefsConfig | None = None
    ticket_prefixes: dict[str, str] = field(default_factory=dict)
    """Uppercase key prefix -> provider name (``"linear"`` / ``"jira"``)."""

    def enabled(self) -> bool:
        """Whether any provider is configured."""
        return self.github is not None or self.linear is not None or self.jira is not None

    @staticmethod
    def build(
        github: GithubRefsConfig | None = None,
        linear: LinearRefsConfig | None = None,
        jira: JiraRefsConfig | None = None,
    ) -> RefsConfig:
        """Assemble a config, deriving the prefix->provider routing table.

        A prefix claimed by both providers routes to Linear: Linear is the
        personal board and Jira the mirror in every deployment we run, so the
        collision (which is a misconfiguration) degrades toward the board.

        :param github: GitHub provider config, or ``None``.
        :param linear: Linear provider config, or ``None``.
        :param jira: Jira provider config, or ``None``.
        :returns: The assembled, frozen config.
        """
        prefixes: dict[str, str] = {}
        if jira is not None:
            for prefix in jira.projects:
                prefixes[prefix.upper()] = "jira"
        if linear is not None:
            for prefix in linear.teams:
                prefixes[prefix.upper()] = "linear"
        return RefsConfig(github=github, linear=linear, jira=jira, ticket_prefixes=prefixes)

    @staticmethod
    def from_env() -> RefsConfig:
        """Build the config from ``OMNIGENT_REFS_*`` env vars.

        A provider only activates when its credential AND its routing input
        are both present (a GitHub token with no repos can still resolve
        ``owner/repo#n``, so repos stay optional there).

        :returns: The resolved config; ``RefsConfig()`` when nothing is set.
        """
        github = None
        github_token = _opt("OMNIGENT_REFS_GITHUB_TOKEN")
        if github_token:
            github = GithubRefsConfig(
                token=github_token,
                repos=_csv("OMNIGENT_REFS_GITHUB_REPOS"),
            )

        linear = None
        linear_key = _opt("OMNIGENT_REFS_LINEAR_API_KEY")
        linear_workspace = _opt("OMNIGENT_REFS_LINEAR_WORKSPACE")
        linear_teams = _csv("OMNIGENT_REFS_LINEAR_TEAMS")
        if linear_key and linear_workspace and linear_teams:
            linear = LinearRefsConfig(
                api_key=linear_key,
                workspace=linear_workspace,
                teams=linear_teams,
            )

        jira = None
        jira_base = _opt("OMNIGENT_REFS_JIRA_BASE_URL")
        jira_email = _opt("OMNIGENT_REFS_JIRA_EMAIL")
        jira_token = _opt("OMNIGENT_REFS_JIRA_API_TOKEN")
        jira_projects = _csv("OMNIGENT_REFS_JIRA_PROJECTS")
        if jira_base and jira_email and jira_token and jira_projects:
            jira = JiraRefsConfig(
                base_url=jira_base.rstrip("/"),
                email=jira_email,
                api_token=jira_token,
                projects=jira_projects,
            )

        return RefsConfig.build(github=github, linear=linear, jira=jira)
