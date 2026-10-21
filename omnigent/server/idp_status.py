"""Check a refresh grant's user against the identity provider (fork-owned).

When a status check is configured, refresh grants (login and device-flow)
never age out. Instead every refresh asks the IdP whether the user is still
allowed in. Removing someone from the IdP revokes their grants on their
next refresh, so access ends within one access-token lifetime plus the
cache window.

The check fails open: an IdP that is down, slow, or answers with garbage
yields ``UNKNOWN`` and the refresh goes ahead, so an IdP outage never logs
out every host. Without a configured check, the server keeps upstream's
absolute grant lifetime.

Configuration (environment):

- ``OMNIGENT_IDP_STATUS_PROVIDER``: ``authentik`` enables the check.
- ``OMNIGENT_AUTHENTIK_URL``: base URL, e.g. ``https://auth.example.com``.
- ``OMNIGENT_AUTHENTIK_API_TOKEN``: API token that can view users and
  applications (read-only is enough).
- ``OMNIGENT_AUTHENTIK_APP_SLUG``: application slug, default ``omnigent``.
"""

from __future__ import annotations

import enum
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, cast

import httpx
from sqlalchemy import and_, delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlDeviceGrant, current_workspace_id
from omnigent.db.enum_codecs import encode_device_grant_status
from omnigent.db.utils import run_write_transaction
from omnigent.entities import DeviceGrant
from omnigent.server.device_grant_store import DeviceGrantStore

_logger = logging.getLogger(__name__)

PROVIDER_ENV = "OMNIGENT_IDP_STATUS_PROVIDER"
AUTHENTIK_URL_ENV = "OMNIGENT_AUTHENTIK_URL"
AUTHENTIK_TOKEN_ENV = "OMNIGENT_AUTHENTIK_API_TOKEN"
AUTHENTIK_APP_SLUG_ENV = "OMNIGENT_AUTHENTIK_APP_SLUG"

_DEFAULT_APP_SLUG = "omnigent"
_HTTP_TIMEOUT_SECONDS = 3.0
_ACTIVE_CACHE_SECONDS = 300.0
_FAIL_OPEN_WARNING_INTERVAL_SECONDS = 60.0
# Rotation lifetime when the IdP check replaces the cap. Kept within a
# 32-bit INTEGER so ``now - lifetime`` binds on every backend.
_UNCAPPED_LIFETIME_SECONDS = 2**31 - 1


class IdpStatus(enum.Enum):
    """What the IdP says about a user."""

    ACTIVE = "active"
    REMOVED = "removed"
    UNKNOWN = "unknown"


class IdpStatusChecker(Protocol):
    """Answers whether a user may still hold a refresh grant."""

    def check(self, user_id: str) -> IdpStatus: ...


class AuthentikStatusChecker:
    """Ask Authentik whether a user exists, is active and may use the app.

    ``check_access`` alone is not enough: Authentik reports ``passing: true``
    for a deactivated user, so ``is_active`` is required as well.

    :param base_url: Authentik base URL.
    :param api_token: Bearer token with view access to users and applications.
    :param app_slug: Slug of the Omnigent application in Authentik.
    :param transport: Optional httpx transport, for tests.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        app_slug: str = _DEFAULT_APP_SLUG,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v3",
            headers={"Authorization": f"Bearer {api_token}", "Accept": "application/json"},
            timeout=_HTTP_TIMEOUT_SECONDS,
            transport=transport,
        )
        self._app_slug = app_slug
        self._clock = clock
        self._warning_lock = threading.Lock()
        self._next_warning_at = 0.0
        self._suppressed_warnings = 0

    def check(self, user_id: str) -> IdpStatus:
        try:
            users = self._get_json("/core/users/", {"email": user_id})
            matches = [
                user
                for user in users["results"]
                if str(user.get("email", "")).casefold() == user_id.casefold()
            ]
            if not matches:
                return IdpStatus.REMOVED
            for user in matches:
                if user.get("is_active") is not True:
                    continue
                access = self._get_json(
                    f"/core/applications/{self._app_slug}/check_access/",
                    {"for_user": user["pk"]},
                )
                if access["passing"] is True:
                    return IdpStatus.ACTIVE
            return IdpStatus.REMOVED
        except _AuthentikAuthError as error:
            _logger.error(
                "idp status: Authentik rejected our API token (%s); allowing refresh for %s",
                error,
                user_id,
            )
        except (httpx.HTTPError, _AuthentikUnavailable, KeyError, TypeError, ValueError) as error:
            self._warn_fail_open(user_id, error)
        return IdpStatus.UNKNOWN

    def _warn_fail_open(self, user_id: str, error: Exception) -> None:
        """Log a fail-open refresh at most once a minute, counting the rest."""
        with self._warning_lock:
            now = self._clock()
            if now < self._next_warning_at:
                self._suppressed_warnings += 1
                return
            suppressed, self._suppressed_warnings = self._suppressed_warnings, 0
            self._next_warning_at = now + _FAIL_OPEN_WARNING_INTERVAL_SECONDS
        _logger.warning(
            "idp status: Authentik check failed for %s (%s); allowing refresh "
            "(%d similar failures suppressed since the last warning)",
            user_id,
            error,
            suppressed,
        )

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._client.get(path, params=params)
        if response.status_code in (401, 403):
            raise _AuthentikAuthError(f"HTTP {response.status_code} from {path}")
        if response.status_code != 200:
            raise _AuthentikUnavailable(f"HTTP {response.status_code} from {path}")
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"non-object JSON from {path}")
        return cast(dict[str, Any], body)


class _AuthentikAuthError(Exception):
    """Authentik answered 401/403: our API token is wrong or lacks a scope."""


class _AuthentikUnavailable(Exception):
    """Authentik answered something other than 200, 401 or 403."""


class CachingStatusChecker:
    """Remember ``ACTIVE`` answers for a while; never cache anything else.

    A removed user must be refused on every refresh, and an ``UNKNOWN``
    must be retried, so only ``ACTIVE`` is worth caching.
    """

    def __init__(
        self,
        inner: IdpStatusChecker,
        *,
        ttl_seconds: float = _ACTIVE_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._active_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, user_id: str) -> IdpStatus:
        key = user_id.casefold()
        with self._lock:
            if self._active_until.get(key, 0.0) > self._clock():
                return IdpStatus.ACTIVE
        status = self._inner.check(user_id)
        with self._lock:
            if status is IdpStatus.ACTIVE:
                self._active_until[key] = self._clock() + self._ttl_seconds
            else:
                self._active_until.pop(key, None)
        return status


def checker_from_env(auth_provider: object) -> IdpStatusChecker | None:
    """Build the configured status check, or ``None`` when none is configured.

    The check looks a grant's user up by email, which only holds for an
    OIDC login backed by the IdP. Under any other auth mode (an accounts
    username, a GitHub login) every user would read as removed and lose
    their grants, so the check refuses to start there. A refused or
    half-configured check logs an error and returns ``None``, which keeps
    the absolute grant lifetime in force rather than lifting it.

    :param auth_provider: The server's active auth provider.
    """
    provider = os.environ.get(PROVIDER_ENV, "").strip().lower()
    if not provider:
        return None
    source = getattr(auth_provider, "_source", None)
    oidc_type = getattr(getattr(auth_provider, "_oidc_config", None), "provider_type", None)
    if source != "oidc" or oidc_type == "github":
        _logger.error(
            "%s is set but the server's auth provider is %r%s, not an OIDC IdP; "
            "the IdP status check is OFF and grant lifetime stays capped",
            PROVIDER_ENV,
            source,
            " (GitHub OAuth)" if oidc_type == "github" else "",
        )
        return None
    if provider != "authentik":
        _logger.error(
            "%s=%r is not supported; grant lifetime stays capped", PROVIDER_ENV, provider
        )
        return None
    url = os.environ.get(AUTHENTIK_URL_ENV, "").strip()
    token = os.environ.get(AUTHENTIK_TOKEN_ENV, "").strip()
    if not url or not token:
        _logger.error(
            "%s=authentik needs %s and %s; grant lifetime stays capped",
            PROVIDER_ENV,
            AUTHENTIK_URL_ENV,
            AUTHENTIK_TOKEN_ENV,
        )
        return None
    slug = os.environ.get(AUTHENTIK_APP_SLUG_ENV, "").strip() or _DEFAULT_APP_SLUG
    # Both routers ask at mount; one shared instance means one cache.
    key = (url, token, slug)
    with _shared_lock:
        checker = _shared_checkers.get(key)
        if checker is None:
            checker = CachingStatusChecker(AuthentikStatusChecker(url, token, app_slug=slug))
            _shared_checkers[key] = checker
    return checker


_shared_checkers: dict[tuple[str, str, str], IdpStatusChecker] = {}
_shared_lock = threading.Lock()


def rotation_lifetime(max_lifetime_seconds: int, checker: IdpStatusChecker | None) -> int:
    """Return the lifetime a device-flow rotation is held to."""
    return max_lifetime_seconds if checker is None else _UNCAPPED_LIFETIME_SECONDS


def admit_refresh(checker: IdpStatusChecker, store: DeviceGrantStore, grant: DeviceGrant) -> bool:
    """Decide whether a refresh may proceed; revoke the grant if the user is gone.

    :returns: ``False`` only when the IdP says the user was removed.
    """
    if grant.user_id is None:
        return True
    status = checker.check(grant.user_id)
    if status is IdpStatus.REMOVED:
        store.revoke(grant.id)
        _logger.warning(
            "oauth/token: %s is no longer allowed by the IdP; revoked grant %s",
            grant.user_id,
            grant.id,
        )
        return False
    return True


def purge_grants(
    store: DeviceGrantStore,
    now_epoch_seconds: int,
    *,
    max_lifetime_seconds: int,
    checker: IdpStatusChecker | None,
) -> int:
    """Purge dead grants, never a live one while the IdP check is on.

    Without a checker this is upstream's purge, absolute lifetime included.
    With one, a live ``redeemed`` grant is kept however old it is; expired
    device codes and revoked grants are deleted.
    """
    if checker is None:
        return store.purge_expired(now_epoch_seconds, max_lifetime_seconds=max_lifetime_seconds)
    deleted = store.purge_expired(now_epoch_seconds)

    def write(session: Session) -> int:
        result = cast(
            CursorResult[tuple[object]],
            session.execute(
                delete(SqlDeviceGrant).where(
                    and_(
                        SqlDeviceGrant.workspace_id == current_workspace_id(),
                        SqlDeviceGrant.status == encode_device_grant_status("revoked"),
                    )
                )
            ),
        )
        return result.rowcount

    return deleted + run_write_transaction(
        store._session_immediate, "purge_revoked_device_grants", write
    )
