"""The redirect form of ``/auth/cli-login``: the Jarvis web sign-in.

A ticket that names a registered ``redirect_uri`` skips the consent page,
sends a one-time code to that URI, and is redeemed only at
``/auth/cli-redeem`` with code + PKCE verifier. ``cli-poll`` never answers
it, so whoever made a login link cannot collect the person who used it.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from fastapi import FastAPI

from omnigent.server.auth import RESERVED_USER_LOCAL, UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.routes.auth import (
    LOGIN_REDIRECT_ALLOW_HTTP_LOOPBACK_ENV,
    LOGIN_REDIRECT_URIS_ENV,
    login_redirect_uris_from_env,
)
from omnigent.server.routes.device_auth import create_oauth_token_router
from tests.server.integration.oidc_fixtures import (
    TEST_SIGNING_KEY,
    _mint_state_cookie,
    _mock_httpx_client_for_github,
    _pkce_pair,
    make_oidc_config,
)
from tests.server.integration.oidc_fixtures import (
    build_oidc_transport as _build_app,
)

_REDIRECT_URI = "https://jarvis.example.com/auth/callback"
_STATE = "jarvis-state-0123456789._~-abcdef"


@pytest.fixture(autouse=True)
def _registered_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_OIDC_LEGACY_TICKET_LOGIN", "0")
    monkeypatch.setenv(LOGIN_REDIRECT_URIS_ENV, f"{_REDIRECT_URI}, https://other.example.com/cb")
    monkeypatch.delenv(LOGIN_REDIRECT_ALLOW_HTTP_LOOPBACK_ENV, raising=False)


@asynccontextmanager
async def _client(store: DeviceGrantStore | None = None) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=_build_app(store), base_url="http://test", follow_redirects=False
    ) as client:
        yield client


async def _create_redirect_ticket(
    client: httpx.AsyncClient, *, redirect_uri: str = _REDIRECT_URI, state: str = _STATE
) -> tuple[str, str]:
    verifier, challenge = _pkce_pair()
    response = await client.post(
        "/auth/cli-login",
        json={
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
            "state": state,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["login_url"] == f"/auth/login?ticket={body['ticket']}&reauth=1"
    assert "user_code" not in body
    return body["ticket"], verifier


async def _sign_in(client: httpx.AsyncClient, ticket: str, email: str = "alice@example.com"):
    """The browser's IdP round trip, ending at the step the callback redirects to."""
    mock_cm = _mock_httpx_client_for_github(
        emails_json=[{"email": email, "primary": True, "verified": True}]
    )
    with patch("omnigent.server.routes.auth.httpx.AsyncClient", return_value=mock_cm):
        return await client.get(
            "/auth/callback",
            params={"code": "auth-code", "state": "state"},
            cookies={"ap_auth_state": _mint_state_cookie("state", ticket=ticket)},
        )


async def _deliver_code(client: httpx.AsyncClient, ticket: str) -> str:
    """Sign in, follow the callback to cli-redirect, and return the delivered code."""
    callback = await _sign_in(client, ticket)
    assert callback.status_code == 302
    assert callback.headers["location"] == f"/auth/cli-redirect?ticket={ticket}"
    redirect = await client.get(callback.headers["location"])
    assert redirect.status_code == 302, redirect.text
    location = redirect.headers["location"]
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == _REDIRECT_URI
    query = parse_qs(parts.query, strict_parsing=True)
    assert query["state"] == [_STATE]
    (code,) = query["code"]
    assert redirect.headers["cache-control"] == "no-store"
    assert redirect.headers["referrer-policy"] == "no-referrer"
    return code


async def _redeem(client: httpx.AsyncClient, ticket: str, code: str, verifier: str):
    return await client.post(
        "/auth/cli-redeem", json={"ticket": ticket, "code": code, "code_verifier": verifier}
    )


def _claims(token: str) -> dict[str, object]:
    return jwt.decode(token, TEST_SIGNING_KEY, algorithms=["HS256"])


# ── Registration ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "redirect_uri",
    [
        f"{_REDIRECT_URI}/",
        "http://jarvis.example.com/auth/callback",
        "https://evil.example.com/auth/callback",
        "https://JARVIS.example.com/auth/callback",
        "https://jarvis.example.com/auth/Callback",
        f"{_REDIRECT_URI}?next=/",
        f"{_REDIRECT_URI}#x",
        "https://jarvis.example.com:443/auth/callback",
        "",
        123,
        None,
    ],
)
async def test_cli_login_redirect_uri_must_match_a_registered_entry_exactly(
    redirect_uri: object,
) -> None:
    _, challenge = _pkce_pair()
    async with _client() as client:
        response = await client.post(
            "/auth/cli-login",
            json={"code_challenge": challenge, "redirect_uri": redirect_uri, "state": _STATE},
        )
    assert response.status_code == 400
    assert "ticket" not in response.json()


@pytest.mark.parametrize(
    "state",
    [
        None,
        "",
        "a" * 15,
        "a" * 513,
        "has space-in-it-xx",
        "slash/not/allowed/x",
        12345678901234567,
    ],
)
async def test_cli_login_redirect_requires_a_bounded_state(state: object) -> None:
    _, challenge = _pkce_pair()
    body: dict[str, object] = {"code_challenge": challenge, "redirect_uri": _REDIRECT_URI}
    if state is not None:
        body["state"] = state
    async with _client() as client:
        response = await client.post("/auth/cli-login", json=body)
    assert response.status_code == 400


@pytest.mark.parametrize("state", ["a" * 16, "a" * 512])
async def test_cli_login_redirect_state_length_bounds_are_inclusive(state: str) -> None:
    async with _client() as client:
        await _create_redirect_ticket(client, state=state)


async def test_cli_login_state_without_redirect_uri_is_refused() -> None:
    _, challenge = _pkce_pair()
    async with _client() as client:
        response = await client.post(
            "/auth/cli-login", json={"code_challenge": challenge, "state": _STATE}
        )
    assert response.status_code == 400


async def test_cli_login_redirect_still_requires_pkce() -> None:
    async with _client() as client:
        response = await client.post(
            "/auth/cli-login", json={"redirect_uri": _REDIRECT_URI, "state": _STATE}
        )
    assert response.status_code == 400


async def test_no_registered_uris_means_no_redirect_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LOGIN_REDIRECT_URIS_ENV)
    _, challenge = _pkce_pair()
    async with _client() as client:
        response = await client.post(
            "/auth/cli-login",
            json={"code_challenge": challenge, "redirect_uri": _REDIRECT_URI, "state": _STATE},
        )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "entry",
    [
        "http://jarvis.example.com/auth/callback",
        "http://127.0.0.1:5173/auth/callback",
        "http://localhost:5173/auth/callback",
        "https://jarvis.example.com/auth/callback?x=1",
        "https://jarvis.example.com/auth/callback#x",
        "https://user@jarvis.example.com/auth/callback",
        "https:///auth/callback",
        "jarvis.example.com/auth/callback",
        "ftp://jarvis.example.com/cb",
    ],
)
def test_registration_refuses_non_https_entries(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOGIN_REDIRECT_URIS_ENV, f"{_REDIRECT_URI},{entry}")
    with pytest.raises(RuntimeError, match=LOGIN_REDIRECT_URIS_ENV):
        login_redirect_uris_from_env()


def test_registration_allows_http_loopback_only_with_the_test_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loopback = "http://127.0.0.1:5173/auth/callback,http://localhost:5173/auth/callback"
    monkeypatch.setenv(LOGIN_REDIRECT_URIS_ENV, loopback)
    monkeypatch.setenv(LOGIN_REDIRECT_ALLOW_HTTP_LOOPBACK_ENV, "1")
    assert login_redirect_uris_from_env() == frozenset(loopback.split(","))
    monkeypatch.setenv(LOGIN_REDIRECT_URIS_ENV, "http://jarvis.example.com/auth/callback")
    with pytest.raises(RuntimeError):
        login_redirect_uris_from_env()


def test_registration_trims_and_skips_empty_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOGIN_REDIRECT_URIS_ENV, f" {_REDIRECT_URI} ,, ")
    assert login_redirect_uris_from_env() == frozenset({_REDIRECT_URI})
    monkeypatch.setenv(LOGIN_REDIRECT_URIS_ENV, "")
    assert login_redirect_uris_from_env() == frozenset()


# ── Sign-in and redeem ───────────────────────────────────────────


async def test_redirect_login_skips_consent_and_redeems_once(tmp_path: Path) -> None:
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    async with _client(store) as client:
        ticket, verifier = await _create_redirect_ticket(client)
        code = await _deliver_code(client, ticket)

        consent = await client.get(f"/auth/cli-consent?ticket={ticket}")
        assert "invalid or has expired" in consent.text
        assert "Authorize sign-in" not in consent.text

        redeemed = await _redeem(client, ticket, code, verifier)
        assert redeemed.status_code == 200, redeemed.text
        assert redeemed.headers["cache-control"] == "no-store"
        body = redeemed.json()
        assert body["user_id"] == "alice@example.com"
        assert body["refresh_token"]
        claims = _claims(body["token"])
        assert claims["sub"] == "alice@example.com"
        assert claims["azp"] == "jarvis"
        assert "cli_login_ticket" not in claims

        again = await _redeem(client, ticket, code, verifier)
        assert again.status_code == 410
        assert "token" not in again.json()


async def test_code_is_delivered_once_and_only_in_the_redirect() -> None:
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        callback = await _sign_in(client, ticket)
        assert "code=" not in callback.headers["location"]
        redirect = await client.get(callback.headers["location"])
        code = parse_qs(urlsplit(redirect.headers["location"]).query)["code"][0]
        assert code not in redirect.text

        second = await client.get(callback.headers["location"])
        assert second.status_code == 400
        assert "location" not in second.headers

        assert (await _redeem(client, ticket, code, verifier)).status_code == 200


async def test_second_sign_in_cannot_swap_the_identity_on_a_fulfilled_ticket() -> None:
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        code = await _deliver_code(client, ticket)
        callback = await _sign_in(client, ticket, email="mallory@example.com")
        swapped = await client.get(callback.headers["location"])
        assert swapped.status_code == 400
        redeemed = await _redeem(client, ticket, code, verifier)
        assert redeemed.json()["user_id"] == "alice@example.com"


@pytest.mark.parametrize(
    "wrong",
    ["code", "verifier", "missing_code", "missing_verifier", "malformed_verifier"],
)
async def test_any_wrong_proof_burns_the_ticket(wrong: str) -> None:
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        code = await _deliver_code(client, ticket)
        body: dict[str, object] = {"ticket": ticket, "code": code, "code_verifier": verifier}
        if wrong == "code":
            body["code"] = code[:-1] + ("A" if code[-1] != "A" else "B")
        elif wrong == "verifier":
            body["code_verifier"] = "b" * 64
        elif wrong == "missing_code":
            del body["code"]
        elif wrong == "missing_verifier":
            del body["code_verifier"]
        else:
            body["code_verifier"] = "!" * 43
        refused = await client.post("/auth/cli-redeem", json=body)
        assert refused.status_code == 400
        assert refused.json() == {"error": "invalid_grant"}

        burned = await _redeem(client, ticket, code, verifier)
        assert burned.status_code == 410


async def test_redeem_before_sign_in_burns_the_ticket() -> None:
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        early = await _redeem(client, ticket, "guess", verifier)
        assert early.status_code == 400
        assert early.json() == {"error": "invalid_grant"}
        callback = await _sign_in(client, ticket)
        assert callback.headers["location"] != f"/auth/cli-redirect?ticket={ticket}"


async def test_expired_ticket_redeems_as_410() -> None:
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        code = await _deliver_code(client, ticket)
        with patch("omnigent.server.routes.auth.time.time", return_value=time.time() + 301):
            expired = await _redeem(client, ticket, code, verifier)
        assert expired.status_code == 410
        assert expired.json() == {"error": "expired_token"}


@pytest.mark.parametrize(
    "body", [b"", b"not json", b"[]", b'{"code": "c", "code_verifier": "v"}', b'{"ticket": 1}']
)
async def test_redeem_rejects_malformed_bodies(body: bytes) -> None:
    async with _client() as client:
        response = await client.post(
            "/auth/cli-redeem", content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


async def test_redeem_rejects_oversized_body() -> None:
    async with _client() as client:
        response = await client.post("/auth/cli-redeem", content=b"x" * 4097)
    assert response.status_code == 413


async def test_redeem_never_touches_a_consent_ticket() -> None:
    verifier, challenge = _pkce_pair()
    async with _client() as client:
        created = await client.post("/auth/cli-login", json={"code_challenge": challenge})
        ticket = created.json()["ticket"]
        refused = await _redeem(client, ticket, "guess", verifier)
        assert refused.status_code == 400
        assert refused.json() == {"error": "invalid_request"}
        pending = await client.get(
            "/auth/cli-poll",
            params={"ticket": ticket},
            headers={"X-Omnigent-Code-Verifier": verifier},
        )
        assert pending.status_code == 202


async def test_cli_poll_never_answers_a_redirect_ticket() -> None:
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        headers = {"X-Omnigent-Code-Verifier": verifier}
        before = await client.get("/auth/cli-poll", params={"ticket": ticket}, headers=headers)
        assert before.status_code == 400
        code = await _deliver_code(client, ticket)
        after = await client.get("/auth/cli-poll", params={"ticket": ticket}, headers=headers)
        assert after.status_code == 400
        assert "token" not in after.json()
        assert "refresh_token" not in after.json()
        assert (await _redeem(client, ticket, code, verifier)).status_code == 200


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_consent_forms_refuse_a_redirect_ticket(decision: str) -> None:
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        await _sign_in(client, ticket)
        response = await client.post(
            f"/auth/cli-{decision}", data={"ticket": ticket}, headers={"Origin": "http://test"}
        )
        assert "invalid or has expired" in response.text
        code = await _deliver_code(client, ticket)
        assert (await _redeem(client, ticket, code, verifier)).status_code == 200


async def test_stale_session_bounces_to_reauth_without_a_code() -> None:
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        await _sign_in(client, ticket)
        with patch("omnigent.server.oidc.time.time", return_value=time.time() - 10):
            stale = mint_session_cookie(
                "alice@example.com", TEST_SIGNING_KEY, 8, "github", cli_login_ticket=ticket
            )
        client.cookies.set("ap_session", stale)
        bounced = await client.get(f"/auth/cli-redirect?ticket={ticket}")
        assert bounced.status_code == 302
        location = bounced.headers["location"]
        assert location.startswith("/auth/login?")
        assert "reauth=1" in location
        assert f"ticket={ticket}" in location
        assert "cli-redirect" in parse_qs(urlsplit(location).query)["return_to"][0]
        assert (await _redeem(client, ticket, "guess", verifier)).status_code == 400


@pytest.mark.parametrize("credential", ["none", "plain_session", "other_ticket"])
async def test_redirect_step_requires_this_tickets_sign_in(credential: str) -> None:
    async with _client() as client:
        ticket, _ = await _create_redirect_ticket(client)
        if credential == "plain_session":
            client.cookies.set(
                "ap_session",
                mint_session_cookie("alice@example.com", TEST_SIGNING_KEY, 8, "github"),
            )
        elif credential == "other_ticket":
            other, _ = await _create_redirect_ticket(client)
            await _sign_in(client, other)
        response = await client.get(f"/auth/cli-redirect?ticket={ticket}")
        assert response.status_code == 302
        assert response.headers["location"].startswith("/auth/login?")


async def test_redirect_step_requires_a_subject_the_server_accepts() -> None:
    """A cookie bound to the ticket is not enough when its subject is refused."""
    async with _client() as client:
        ticket, verifier = await _create_redirect_ticket(client)
        client.cookies.set(
            "ap_session",
            mint_session_cookie(
                RESERVED_USER_LOCAL, TEST_SIGNING_KEY, 8, "github", cli_login_ticket=ticket
            ),
        )
        response = await client.get(f"/auth/cli-redirect?ticket={ticket}")
        assert response.status_code == 302
        assert response.headers["location"].startswith("/auth/login?")
        assert (await _redeem(client, ticket, "guess", verifier)).status_code == 400


async def test_redirect_step_refuses_a_consent_ticket() -> None:
    _, challenge = _pkce_pair()
    async with _client() as client:
        ticket = (await client.post("/auth/cli-login", json={"code_challenge": challenge})).json()[
            "ticket"
        ]
        callback = await _sign_in(client, ticket)
        assert callback.headers["location"] == f"/auth/cli-consent?ticket={ticket}"
        response = await client.get(f"/auth/cli-redirect?ticket={ticket}")
        assert response.status_code == 400


async def test_code_goes_to_the_uri_the_ticket_named() -> None:
    async with _client() as client:
        ticket, _ = await _create_redirect_ticket(
            client, redirect_uri="https://other.example.com/cb"
        )
        callback = await _sign_in(client, ticket)
        redirect = await client.get(callback.headers["location"])
        assert redirect.headers["location"].startswith("https://other.example.com/cb?code=")


# ── azp ──────────────────────────────────────────────────────────


def _token_app(store: DeviceGrantStore) -> httpx.ASGITransport:
    provider = UnifiedAuthProvider(source="oidc", oidc_config=make_oidc_config())
    app = FastAPI()
    app.include_router(create_oauth_token_router(provider, store))
    return httpx.ASGITransport(app=app)


async def _refresh(store: DeviceGrantStore, refresh_token: str) -> dict[str, object]:
    async with httpx.AsyncClient(transport=_token_app(store), base_url="http://test") as client:
        response = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
    assert response.status_code == 200, response.text
    return _claims(response.json()["access_token"])


async def test_azp_rides_the_redirect_login_and_its_refresh_only(tmp_path: Path) -> None:
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    async with _client(store) as client:
        ticket, verifier = await _create_redirect_ticket(client)
        code = await _deliver_code(client, ticket)
        jarvis = (await _redeem(client, ticket, code, verifier)).json()

        cli_verifier, challenge = _pkce_pair()
        cli_ticket = (
            await client.post("/auth/cli-login", json={"code_challenge": challenge})
        ).json()["ticket"]
        await _sign_in(client, cli_ticket)
        browser_cookie = client.cookies["ap_session"]
        await client.post(
            "/auth/cli-approve", data={"ticket": cli_ticket}, headers={"Origin": "http://test"}
        )
        cli = (
            await client.get(
                "/auth/cli-poll",
                params={"ticket": cli_ticket},
                headers={"X-Omnigent-Code-Verifier": cli_verifier},
            )
        ).json()

    assert _claims(jarvis["token"])["azp"] == "jarvis"
    jarvis_refresh = await _refresh(store, jarvis["refresh_token"])
    assert jarvis_refresh["azp"] == "jarvis"
    assert jarvis_refresh["act"] == {"client_id": "jarvis"}
    assert "scope" not in jarvis_refresh

    assert "azp" not in _claims(cli["token"])
    assert "azp" not in _claims(browser_cookie)
    cli_refresh = await _refresh(store, cli["refresh_token"])
    assert "azp" not in cli_refresh
    assert cli_refresh["act"] == {"client_id": "omnigent-cli"}


# ── Phishing ─────────────────────────────────────────────────────


async def test_attacker_ticket_yields_nothing_when_a_victim_signs_in(tmp_path: Path) -> None:
    """The attacker makes the ticket and sends its login_url; the victim signs in.

    The code goes to the victim's browser at the registered URI, where the
    attacker has no cookie. Holding ticket and verifier is not enough.
    """
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    transport = _build_app(store)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as attacker,
        httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as victim_browser,
    ):
        ticket, verifier = await _create_redirect_ticket(attacker)
        callback = await _sign_in(victim_browser, ticket, email="victim@example.com")
        delivered = await victim_browser.get(callback.headers["location"])
        assert delivered.headers["location"].startswith(f"{_REDIRECT_URI}?code=")

        headers = {"X-Omnigent-Code-Verifier": verifier}
        polled = await attacker.get("/auth/cli-poll", params={"ticket": ticket}, headers=headers)
        assert polled.status_code == 400
        assert "token" not in polled.json()

        guessed = await attacker.post(
            "/auth/cli-redeem", json={"ticket": ticket, "code_verifier": verifier}
        )
        assert guessed.status_code == 400
        assert "token" not in guessed.json()

        # The guess burned the ticket: even the real code no longer works.
        code = parse_qs(urlsplit(delivered.headers["location"]).query)["code"][0]
        assert (await _redeem(attacker, ticket, code, verifier)).status_code == 410
