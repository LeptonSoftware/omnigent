"""Refresh grants are checked against the IdP instead of aging out.

The Authentik HTTP layer is a ``httpx.MockTransport``; nothing here calls a
live IdP. Store-level cases take ``db_uri`` so they also run on Postgres
when ``OMNIGENT_TEST_DB_URI`` is set.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from omnigent.server import idp_status
from omnigent.server.device_grant_store import DeviceGrantStore, hash_secret
from omnigent.server.idp_status import (
    AuthentikStatusChecker,
    CachingStatusChecker,
    IdpStatus,
    purge_grants,
)
from tests.server.test_device_auth import (
    _build_accounts_app,
    _exchange_device_code,
    _login_admin,
)

_KEY = b"k" * 32
_DAY = 86400
_LIFETIME = 30 * _DAY
_T0 = 1_000_000


@dataclass
class FakeAuthentik:
    """In-memory Authentik answering the two endpoints the checker uses."""

    users: dict[str, dict[str, object]] = field(default_factory=dict)
    passing: dict[int, bool] = field(default_factory=dict)
    fail_with: int | None = None
    raise_error: Exception | None = None
    calls: int = 0

    def add(self, email: str, pk: int, *, active: bool = True, passing: bool = True) -> None:
        self.users[email] = {"pk": pk, "email": email, "is_active": active}
        self.passing[pk] = passing

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert request.headers["Authorization"] == "Bearer tok"
        if self.raise_error is not None:
            raise self.raise_error
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"detail": "nope"})
        if request.url.path == "/api/v3/core/users/":
            # Authentik's filter is looser than an exact match; return near
            # misses too so the checker's own exact comparison is exercised.
            wanted = request.url.params["email"].lower()
            results = [user for email, user in self.users.items() if wanted in email.lower()]
            return httpx.Response(200, json={"results": results})
        if request.url.path == "/api/v3/core/applications/omnigent/check_access/":
            pk = int(request.url.params["for_user"])
            return httpx.Response(200, json={"passing": self.passing[pk], "messages": []})
        return httpx.Response(404, json={})

    def checker(self) -> AuthentikStatusChecker:
        return AuthentikStatusChecker(
            "https://auth.example.com/", "tok", transport=httpx.MockTransport(self.handler)
        )


# ── The Authentik checker ─────────────────────────────────────────


def test_active_user_with_app_access_is_active() -> None:
    idp = FakeAuthentik()
    idp.add("alice@example.com", 7)
    assert idp.checker().check("Alice@Example.com") is IdpStatus.ACTIVE


def test_deactivated_user_is_removed_even_though_check_access_passes() -> None:
    idp = FakeAuthentik()
    idp.add("alice@example.com", 7, active=False, passing=True)
    assert idp.checker().check("alice@example.com") is IdpStatus.REMOVED


def test_deleted_user_is_removed() -> None:
    idp = FakeAuthentik()
    idp.add("malice@example.com", 8)  # a near miss the email filter returns
    assert idp.checker().check("alice@example.com") is IdpStatus.REMOVED


def test_user_without_app_access_is_removed() -> None:
    idp = FakeAuthentik()
    idp.add("alice@example.com", 7, passing=False)
    assert idp.checker().check("alice@example.com") is IdpStatus.REMOVED


@pytest.mark.parametrize("status_code", [500, 503, 404, 429])
def test_idp_errors_are_unknown(status_code: int) -> None:
    idp = FakeAuthentik(fail_with=status_code)
    assert idp.checker().check("alice@example.com") is IdpStatus.UNKNOWN


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")], ids=["down", "timeout"]
)
def test_unreachable_idp_is_unknown(error: Exception) -> None:
    idp = FakeAuthentik(raise_error=error)
    assert idp.checker().check("alice@example.com") is IdpStatus.UNKNOWN


def test_malformed_response_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    checker = AuthentikStatusChecker(
        "https://auth.example.com", "tok", transport=httpx.MockTransport(handler)
    )
    assert checker.check("alice@example.com") is IdpStatus.UNKNOWN


@pytest.mark.parametrize("status_code", [401, 403])
def test_bad_api_token_is_unknown_and_logged_as_error(
    status_code: int, caplog: pytest.LogCaptureFixture
) -> None:
    idp = FakeAuthentik(fail_with=status_code)
    with caplog.at_level(logging.WARNING, logger="omnigent.server.idp_status"):
        assert idp.checker().check("alice@example.com") is IdpStatus.UNKNOWN
    assert [record.levelno for record in caplog.records] == [logging.ERROR]


def test_timeout_is_about_three_seconds() -> None:
    checker = FakeAuthentik().checker()
    assert checker._client.timeout.read == 3.0


# ── Cache ─────────────────────────────────────────────────────────


def test_cache_answers_active_without_calling_the_idp() -> None:
    idp = FakeAuthentik()
    idp.add("alice@example.com", 7)
    clock = {"now": 0.0}
    checker = CachingStatusChecker(idp.checker(), ttl_seconds=300, clock=lambda: clock["now"])

    assert checker.check("alice@example.com") is IdpStatus.ACTIVE
    calls = idp.calls
    clock["now"] = 299
    assert checker.check("alice@example.com") is IdpStatus.ACTIVE
    assert idp.calls == calls
    clock["now"] = 301
    assert checker.check("alice@example.com") is IdpStatus.ACTIVE
    assert idp.calls > calls


def test_cache_never_keeps_removed_or_unknown() -> None:
    idp = FakeAuthentik()
    checker = CachingStatusChecker(idp.checker(), ttl_seconds=300, clock=lambda: 0.0)

    assert checker.check("alice@example.com") is IdpStatus.REMOVED
    calls = idp.calls
    assert checker.check("alice@example.com") is IdpStatus.REMOVED
    assert idp.calls > calls

    idp.fail_with = 503
    calls = idp.calls
    assert checker.check("alice@example.com") is IdpStatus.UNKNOWN
    assert checker.check("alice@example.com") is IdpStatus.UNKNOWN
    assert idp.calls == calls + 2


def test_removed_answer_drops_a_cached_active() -> None:
    idp = FakeAuthentik()
    idp.add("alice@example.com", 7)
    clock = {"now": 0.0}
    checker = CachingStatusChecker(idp.checker(), ttl_seconds=300, clock=lambda: clock["now"])
    assert checker.check("alice@example.com") is IdpStatus.ACTIVE
    idp.users["alice@example.com"]["is_active"] = False
    clock["now"] = 301
    assert checker.check("alice@example.com") is IdpStatus.REMOVED
    clock["now"] = 302
    assert checker.check("alice@example.com") is IdpStatus.REMOVED


# ── Configuration ─────────────────────────────────────────────────


def test_unconfigured_means_no_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(idp_status.PROVIDER_ENV, raising=False)
    assert idp_status.checker_from_env() is None


@pytest.mark.parametrize(
    "env",
    [
        {"OMNIGENT_IDP_STATUS_PROVIDER": "okta"},
        {"OMNIGENT_IDP_STATUS_PROVIDER": "authentik", "OMNIGENT_AUTHENTIK_URL": "https://a"},
        {"OMNIGENT_IDP_STATUS_PROVIDER": "authentik", "OMNIGENT_AUTHENTIK_API_TOKEN": "t"},
    ],
    ids=["unsupported", "no-token", "no-url"],
)
def test_half_configured_keeps_the_cap(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (idp_status.AUTHENTIK_URL_ENV, idp_status.AUTHENTIK_TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert idp_status.checker_from_env() is None


def test_configured_authentik_is_shared_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(idp_status.PROVIDER_ENV, "authentik")
    monkeypatch.setenv(idp_status.AUTHENTIK_URL_ENV, "https://auth.example.com")
    monkeypatch.setenv(idp_status.AUTHENTIK_TOKEN_ENV, "tok")
    monkeypatch.delenv(idp_status.AUTHENTIK_APP_SLUG_ENV, raising=False)
    checker = idp_status.checker_from_env()
    assert isinstance(checker, CachingStatusChecker)
    assert idp_status.checker_from_env() is checker


# ── Purge (SQLite and Postgres) ───────────────────────────────────


@pytest.fixture
def store(db_uri: str) -> DeviceGrantStore:
    return DeviceGrantStore(db_uri)


def _login_grant(store: DeviceGrantStore, grant_id: str) -> str:
    store.create_redeemed_grant(
        grant_id,
        user_id="alice@example.com",
        client_id="omnigent-cli",
        refresh_token_hash=hash_secret(grant_id, _KEY),
        created_at=_T0,
    )
    return grant_id


def _pending_grant(store: DeviceGrantStore, grant_id: str) -> str:
    store.create_grant(
        grant_id,
        device_code_hash=hash_secret(f"dc-{grant_id}", _KEY),
        user_code=f"UC-{grant_id}",
        client_id="slack",
        created_at=_T0,
        expires_at=_T0 + 600,
    )
    return grant_id


def test_purge_with_checker_keeps_live_grants_forever(store: DeviceGrantStore) -> None:
    live = _login_grant(store, "live")
    revoked = _login_grant(store, "revoked")
    assert store.revoke(revoked)
    pending = _pending_grant(store, "pending")

    deleted = purge_grants(
        store,
        _T0 + 400 * _DAY,
        max_lifetime_seconds=_LIFETIME,
        checker=FakeAuthentik().checker(),
    )

    assert deleted == 2
    assert store.get_by_id(live) is not None
    assert store.get_by_id(revoked) is None
    assert store.get_by_id(pending) is None


def test_purge_without_checker_keeps_the_absolute_lifetime(store: DeviceGrantStore) -> None:
    aged = _login_grant(store, "aged")
    assert purge_grants(store, _T0 + _DAY, max_lifetime_seconds=_LIFETIME, checker=None) == 0
    assert purge_grants(store, _T0 + 31 * _DAY, max_lifetime_seconds=_LIFETIME, checker=None) == 1
    assert store.get_by_id(aged) is None


# ── /oauth/token ──────────────────────────────────────────────────


@dataclass
class Harness:
    client: TestClient
    store: DeviceGrantStore
    idp: FakeAuthentik
    clock: dict[str, float]
    secret: bytes

    def login_grant(self) -> str:
        from omnigent.server.routes.device_auth import issue_login_grant

        return issue_login_grant(self.store, user_id="admin", cookie_secret=self.secret)

    def refresh(self, token: str, *, day: float | None = None) -> httpx.Response:
        if day is not None:
            self.clock["now"] = self.clock["start"] + day * _DAY
        return self.client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token}
        )

    def status_after(self, response: httpx.Response) -> str:
        """Current status of the grant that issued *response*'s access token."""
        claims = jwt.decode(response.json()["access_token"], options={"verify_signature": False})
        grant = self.store.get_by_id(claims["grant_id"])
        assert grant is not None
        return grant.status


def _harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured: bool,
    device_flow: bool = False,
) -> Iterator[Harness]:
    from omnigent.server.routes import device_auth

    idp = FakeAuthentik()
    idp.add("admin", 1)
    checker = idp.checker() if configured else None
    monkeypatch.setattr(idp_status, "checker_from_env", lambda: checker)
    start = float(int(time.time()))
    clock = {"start": start, "now": start}
    monkeypatch.setattr(device_auth, "time", SimpleNamespace(time=lambda: clock["now"]))
    monkeypatch.setattr(device_auth, "_PURGE_MIN_INTERVAL_SECONDS", 10**9)
    for client in _build_accounts_app(tmp_path, monkeypatch, device_grant_enabled=device_flow):
        yield Harness(
            client=client,
            store=DeviceGrantStore(f"sqlite:///{tmp_path}/test.db"),
            idp=idp,
            clock=clock,
            secret=bytes.fromhex(os.environ["OMNIGENT_ACCOUNTS_COOKIE_SECRET"]),
        )


@pytest.fixture
def checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    yield from _harness(tmp_path, monkeypatch, configured=True)


@pytest.fixture
def unchecked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    yield from _harness(tmp_path, monkeypatch, configured=False)


@pytest.fixture
def checked_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    yield from _harness(tmp_path, monkeypatch, configured=True, device_flow=True)


def test_active_user_refreshes_at_day_400(checked: Harness) -> None:
    token = checked.login_grant()
    response = checked.refresh(token, day=400)
    assert response.status_code == 200, response.text
    assert response.json()["refresh_token"] == token


@pytest.mark.parametrize("change", ["deactivated", "deleted", "no-app-access"])
def test_removed_user_is_refused_and_grant_revoked(
    change: str, checked: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    token = checked.login_grant()
    first = checked.refresh(token, day=1)
    assert first.status_code == 200
    if change == "deactivated":
        checked.idp.users["admin"]["is_active"] = False
    elif change == "deleted":
        del checked.idp.users["admin"]
    else:
        checked.idp.passing[1] = False

    with caplog.at_level(logging.WARNING, logger="omnigent.server.idp_status"):
        response = checked.refresh(token, day=2)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert checked.status_after(first) == "revoked"
    assert any("revoked grant" in record.getMessage() for record in caplog.records)
    # Restoring the user does not bring the revoked grant back.
    checked.idp.add("admin", 1)
    assert checked.refresh(token, day=3).status_code == 400


@pytest.mark.parametrize(
    "failure",
    [503, 500, httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["503", "500", "down", "timeout"],
)
def test_idp_trouble_lets_the_refresh_through(failure: int | Exception, checked: Harness) -> None:
    token = checked.login_grant()
    if isinstance(failure, int):
        checked.idp.fail_with = failure
    else:
        checked.idp.raise_error = failure
    response = checked.refresh(token, day=400)
    assert response.status_code == 200, response.text


def test_bad_idp_token_lets_the_refresh_through_and_logs_error(
    checked: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    token = checked.login_grant()
    checked.idp.fail_with = 401
    with caplog.at_level(logging.ERROR, logger="omnigent.server.idp_status"):
        response = checked.refresh(token, day=1)
    assert response.status_code == 200, response.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_unconfigured_keeps_the_absolute_cap(unchecked: Harness) -> None:
    token = unchecked.login_grant()
    assert unchecked.refresh(token, day=29).status_code == 200
    response = unchecked.refresh(token, day=31)
    assert response.status_code == 400
    assert response.json()["error"] == "expired_token"
    assert unchecked.idp.calls == 0


def test_device_flow_grant_follows_the_same_rules(checked_device: Harness) -> None:
    harness = checked_device
    client = harness.client
    data = client.post("/oauth/device/authorize", json={"client_id": "slack"}).json()
    _login_admin(client)
    client.post(
        "/oauth/device/approve",
        data={"user_code": data["user_code"]},
        headers={"Origin": "http://localhost:8000"},
    )
    exchanged = _exchange_device_code(client, data["device_code"])
    assert exchanged.status_code == 200, exchanged.text
    token = exchanged.json()["refresh_token"]

    # Past the old 30-day cap, an active user's device grant still rotates.
    rotated = harness.refresh(token, day=400)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token"] != token
    token = rotated.json()["refresh_token"]

    harness.idp.users["admin"]["is_active"] = False
    refused = harness.refresh(token, day=401)
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_grant"
    assert harness.status_after(rotated) == "revoked"
