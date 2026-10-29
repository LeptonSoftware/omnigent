"""A host and a CLI recover from a stored login the server stopped accepting.

fork (VIN-3238 QA): clients renewed their token only within 90 s of its
local ``expires_at``. A CLI login lives ``session_ttl_hours`` (30 days on
prod), so when the HS256 window closed every host and CLI holding an HS256
login would have been refused for up to a month despite holding a valid
refresh grant. A refusal now forces exactly one refresh and a retry.

The server here is a real accounts-mode app signing EdDSA with the HS256
window closed. The stored login is a correctly formed HS256 token for the
real admin, valid for 30 days, with a real login-grant refresh token.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from omnigent import cli_auth
from omnigent.host.connect import HostConnectError
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.routes.device_auth import issue_login_grant
from tests.host.test_connect import (
    _AcceptingConnect,
    _ConnectSpy,
    _HandshakeFailingConnect,
    _host,
    _invalid_status,
)
from tests.server.test_device_auth import _build_accounts_app

_SERVER = "http://localhost:8000"
_MONTH_S = 30 * 24 * 3600


@dataclass
class _World:
    client: TestClient
    hs256_login: str
    refresh_token: str
    refresh_posts: list[str]
    refresh_outages: list[int]
    db_url: str


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    key = Ed25519PrivateKey.generate()
    key_file = tmp_path / "signing.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv("OMNIGENT_SESSION_SIGNING", "eddsa")
    monkeypatch.setenv("OMNIGENT_SESSION_SIGNING_KEY_FILE", str(key_file))
    monkeypatch.delenv("OMNIGENT_LEGACY_HS256_UNTIL", raising=False)  # HS256 refused
    monkeypatch.delenv("OMNIGENT_REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli_auth, "_token_file_path", lambda: tmp_path / "auth_tokens.json")
    monkeypatch.setattr(cli_auth, "_refused_refresh_digests", set())
    monkeypatch.setattr(cli_auth, "_forced_refresh_backoff", {})

    app = _build_accounts_app(tmp_path, monkeypatch, device_grant_enabled=False)
    client = next(app)
    db_url = f"sqlite:///{tmp_path}/test.db"
    secret = bytes.fromhex(os.environ["OMNIGENT_ACCOUNTS_COOKIE_SECRET"])
    admin = SqlAlchemyAccountStore(db_url).get_user("admin")
    assert admin is not None
    # What the pre-EdDSA server handed `omnigent login`: HS256, 30 days.
    hs256_login = mint_session_cookie(
        "admin", secret, 720, "accounts", account_generation=admin.account_generation
    )
    refresh_token = issue_login_grant(
        DeviceGrantStore(db_url), user_id="admin", cookie_secret=secret
    )
    cli_auth.store_token(
        _SERVER,
        token=hs256_login,
        user_id="admin",
        expires_at=time.time() + _MONTH_S,
        refresh_token=refresh_token,
    )
    refresh_posts: list[str] = []
    refresh_outages: list[int] = [0]  # answer this many refreshes with 503

    def _post_to_server(
        url: str, *, data: dict[str, str] | None = None, timeout: float | None = None
    ) -> httpx.Response:
        assert url == f"{_SERVER}/oauth/token"
        refresh_posts.append((data or {}).get("refresh_token", ""))
        if refresh_outages[0] > 0:
            refresh_outages[0] -= 1
            return httpx.Response(503, request=httpx.Request("POST", url))
        return client.post("/oauth/token", data=data)

    monkeypatch.setattr(httpx, "post", _post_to_server)
    assert client.get("/v1/hosts", headers=_bearer(hs256_login)).status_code == 401
    yield _World(client, hs256_login, refresh_token, refresh_posts, refresh_outages, db_url)
    next(app, None)  # leave the TestClient context


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _alg(token: str) -> str:
    return str(jwt.get_unverified_header(token)["alg"])


def _stored_token() -> str:
    entry = cli_auth._load_entry(_SERVER)
    assert entry is not None
    return str(entry["token"])


# ── CLI / API calls (OmnigentClient's httpx auth) ────────────────


def _cli_client(world: _World) -> httpx.Client:
    from omnigent.chat import _DatabricksTokenAuth

    return httpx.Client(
        transport=world.client._transport,
        base_url=_SERVER,
        auth=_DatabricksTokenAuth(server_url=_SERVER),
    )


def test_cli_call_refused_with_a_month_left_refreshes_once_and_succeeds(world: _World) -> None:
    with _cli_client(world) as cli:
        resp = cli.get("/v1/hosts")
        assert resp.status_code == 200, resp.text
        assert world.refresh_posts == [world.refresh_token]
        renewed = _stored_token()
        assert _alg(renewed) == "EdDSA"
        assert resp.request.headers["Authorization"] == f"Bearer {renewed}"

        # The renewed token is used from now on: no further refreshes.
        assert cli.get("/v1/hosts").status_code == 200
        assert world.refresh_posts == [world.refresh_token]


def test_cli_refused_refresh_keeps_the_401_and_asks_only_once(world: _World) -> None:
    world.client.post("/oauth/revoke", data={"refresh_token": world.refresh_token})
    with _cli_client(world) as cli:
        assert cli.get("/v1/hosts").status_code == 401
        assert cli.get("/v1/hosts").status_code == 401
    assert world.refresh_posts == [world.refresh_token]  # invalid_grant is final
    assert _stored_token() == world.hs256_login


def test_cli_retries_once_when_the_renewed_token_is_refused_too(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No loop: one refresh and one retry per refused request."""
    import omnigent.chat as chat_mod

    sent: list[str] = []

    def _always_401(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["Authorization"])
        return httpx.Response(401, json={"detail": "nope"})

    auth = chat_mod._DatabricksTokenAuth(server_url=_SERVER)
    with httpx.Client(
        transport=httpx.MockTransport(_always_401), base_url=_SERVER, auth=auth
    ) as cli:
        assert cli.get("/v1/hosts").status_code == 401
    assert len(world.refresh_posts) == 1
    assert len(sent) == 2 and sent[0] != sent[1]


# ── Host tunnel ──────────────────────────────────────────────────


def _tunnel_bearers(spy: _ConnectSpy) -> list[str]:
    bearers: list[str] = []
    for call in spy.calls:
        headers = call["additional_headers"]
        assert isinstance(headers, dict)
        bearers.append(str(headers.get("Authorization", "")))
    return bearers


async def _run_host(
    monkeypatch: pytest.MonkeyPatch,
    script: Sequence[BaseException | int | None],
    *,
    base_s: float = 0.0,
    cap_s: float = 0.0,
) -> tuple[_ConnectSpy, list[float]]:
    import websockets.asyncio.client as ws_client

    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", base_s)
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_CAP_S", cap_s)
    spy = _ConnectSpy(list(script))
    real_connect = ws_client.connect
    host = _host(_SERVER)
    tunnel_url = host._tunnel_url()

    def _connect(url: str, **kwargs: object) -> object:
        # Only this host's tunnel is scripted; tasks other tests left on the
        # worker keep the real client.
        if url == tunnel_url and "additional_headers" in kwargs:
            return spy(url, **kwargs)
        return real_connect(url, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ws_client, "connect", _connect)
    waits: list[float] = []

    async def _record_wait(wait_s: float, *, watch_login: bool) -> None:
        waits.append(wait_s)

    monkeypatch.setattr(host, "_wait_before_reconnect", _record_wait)
    # A never-connected host gives up after a few refusals, as before.
    with contextlib.suppress(HostConnectError):
        await host.run()
    return spy, waits


async def test_host_refused_with_a_month_left_refreshes_once_and_reconnects(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tunnel closes before accept() for a bad bearer, which reaches the
    # host as HTTP 403; then the renewed token is accepted.
    spy, _ = await _run_host(monkeypatch, [_invalid_status(403), None, asyncio.CancelledError()])

    first, second = _tunnel_bearers(spy)[:2]
    assert first == f"Bearer {world.hs256_login}"
    renewed = _stored_token()
    assert _alg(renewed) == "EdDSA"
    assert second == f"Bearer {renewed}"
    assert world.refresh_posts == [world.refresh_token]
    # And the server really accepts it.
    assert world.client.get("/v1/hosts", headers=_bearer(renewed)).status_code == 200


async def test_host_dials_at_once_with_the_renewed_token(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal after an outage does not sit out the grown backoff."""
    spy, waits = await _run_host(
        monkeypatch,
        [_invalid_status(503)] * 3 + [_invalid_status(403), None, asyncio.CancelledError()],
        base_s=0.01,
        cap_s=100.0,
    )

    assert waits[2] > 0.02  # the 503s grew the backoff
    assert waits[3] == 0.01  # the refusal's refresh resets it
    assert _tunnel_bearers(spy)[4] == f"Bearer {_stored_token()}"


async def test_host_forces_one_refresh_not_one_per_retry(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that refuses the renewed token too costs one refresh."""
    spy, _ = await _run_host(monkeypatch, [_invalid_status(401)] * 4 + [asyncio.CancelledError()])

    bearers = _tunnel_bearers(spy)
    assert len(bearers) >= 3
    renewed = _stored_token()
    assert bearers[0] == f"Bearer {world.hs256_login}"
    assert set(bearers[1:]) == {f"Bearer {renewed}"}
    assert world.refresh_posts == [world.refresh_token]


async def test_host_refused_refresh_keeps_todays_behaviour(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.client.post("/oauth/revoke", data={"refresh_token": world.refresh_token})
    spy, _ = await _run_host(monkeypatch, [_invalid_status(403)] * 3 + [asyncio.CancelledError()])

    bearers = _tunnel_bearers(spy)
    assert len(bearers) >= 3
    assert set(bearers) == {f"Bearer {world.hs256_login}"}
    assert world.refresh_posts == [world.refresh_token]


# ── Runner callbacks (same factory, existing on-401 retry) ───────


def test_runner_callback_refused_with_a_month_left_refreshes_once(world: _World) -> None:
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    factory = _make_auth_token_factory(server_url=_SERVER)
    assert factory is not None
    with httpx.Client(
        transport=world.client._transport,
        base_url=_SERVER,
        auth=_RunnerDatabricksAuth(factory, server_url=_SERVER),
    ) as runner:
        assert runner.get("/v1/hosts").status_code == 200
        assert runner.get("/v1/hosts").status_code == 200
    assert world.refresh_posts == [world.refresh_token]
    assert _alg(_stored_token()) == "EdDSA"


# ── A failed refresh: backoff keeps growing, refreshes stay bounded ──


class _Clock:
    """Monotonic time for cli_auth's refresh backoff, advanced by host waits."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Server:
    """Tunnel stand-in: accepts the first dial (an already-connected host) and
    any EdDSA bearer, refuses everything else with 401."""

    def __init__(self, *, max_dials: int = 45) -> None:
        self.bearers: list[str] = []
        self.recovered_at: int | None = None
        self._max_dials = max_dials

    def __call__(self, url: str, **kwargs: object) -> object:
        headers = kwargs["additional_headers"]
        assert isinstance(headers, dict)
        bearer = str(headers.get("Authorization", ""))
        self.bearers.append(bearer)
        dial = len(self.bearers)
        if self.recovered_at is not None or dial > self._max_dials:
            return _HandshakeFailingConnect(asyncio.CancelledError())
        if dial == 1:
            return _AcceptingConnect(0)
        if bearer.startswith("Bearer ") and _alg(bearer[7:]) == "EdDSA":
            self.recovered_at = dial
            return _AcceptingConnect(0)
        return _HandshakeFailingConnect(_invalid_status(401))


async def _run_connected_host(
    monkeypatch: pytest.MonkeyPatch,
    server: _Server,
    *,
    on_wait: object = None,
) -> tuple[list[float], list[float]]:
    """Run a real HostProcess; host waits are recorded and advance the clock.

    :returns: The waits, and the clock reading at each refresh POST.
    """
    import websockets.asyncio.client as ws_client

    clock = _Clock()
    monkeypatch.setattr(cli_auth, "_clock", clock)
    monkeypatch.setattr("omnigent.host.connect._AUTH_REJECT_SLOW_RETRY_S", 60.0)
    host = _host(_SERVER)
    tunnel_url = host._tunnel_url()
    real_connect = ws_client.connect

    def _connect(url: str, **kwargs: object) -> object:
        if url == tunnel_url and "additional_headers" in kwargs:
            return server(url, **kwargs)
        return real_connect(url, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ws_client, "connect", _connect)
    post_times: list[float] = []
    real_post = httpx.post

    def _timed_post(url: str, **kwargs: object) -> httpx.Response:
        post_times.append(clock.now)
        return real_post(url, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "post", _timed_post)
    waits: list[float] = []

    async def _record_wait(wait_s: float, *, watch_login: bool) -> None:
        waits.append(wait_s)
        clock.now += wait_s
        if callable(on_wait):
            on_wait(len(waits))

    monkeypatch.setattr(host, "_wait_before_reconnect", _record_wait)
    await host.run()
    return waits, post_times


async def test_host_with_a_revoked_grant_backs_off_and_refreshes_once(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.client.post("/oauth/revoke", data={"refresh_token": world.refresh_token})
    server = _Server()

    waits, _ = await _run_connected_host(monkeypatch, server)

    from omnigent.host.connect import _AUTH_REJECT_ESCALATE_ATTEMPTS, _RECONNECT_BASE_S

    refusals = waits[1:]  # waits[0] follows the first, accepted tunnel
    assert len(refusals) > _AUTH_REJECT_ESCALATE_ATTEMPTS
    assert all(w > _RECONNECT_BASE_S for w in refusals[1:])  # never reset to 0.5 s
    assert refusals[_AUTH_REJECT_ESCALATE_ATTEMPTS:] == [60.0] * len(
        refusals[_AUTH_REJECT_ESCALATE_ATTEMPTS:]
    )
    assert world.refresh_posts == [world.refresh_token]
    assert server.recovered_at is None


async def test_host_with_a_failing_refresh_endpoint_backs_off_then_recovers(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.refresh_outages[0] = 3
    server = _Server()

    waits, post_times = await _run_connected_host(monkeypatch, server)

    assert server.recovered_at is not None
    dials_refused = server.recovered_at - 2
    assert len(world.refresh_posts) == 4  # 3 refused with 503, then success
    assert dials_refused > len(world.refresh_posts)  # not one POST per dial
    gaps = [b - a for a, b in itertools.pairwise(post_times)]
    assert gaps[0] >= cli_auth.FORCED_REFRESH_BACKOFF_BASE_S
    assert gaps == sorted(gaps)  # the refresh's own backoff grows
    assert server.bearers[server.recovered_at - 1] == f"Bearer {_stored_token()}"
    # The renewed token goes out at once.
    assert waits[server.recovered_at - 2] == 0.5


async def test_host_with_a_revoked_grant_recovers_after_a_new_login(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.client.post("/oauth/revoke", data={"refresh_token": world.refresh_token})
    secret = bytes.fromhex(os.environ["OMNIGENT_ACCOUNTS_COOKIE_SECRET"])

    def _login_on_fifth_wait(count: int) -> None:
        if count != 5:
            return
        # `omnigent login` again: a new HS256-era login and a new grant.
        cli_auth.store_token(
            _SERVER,
            token=world.hs256_login + "x",  # differs; the server refuses it too
            user_id="admin",
            expires_at=time.time() + _MONTH_S,
            refresh_token=issue_login_grant(
                DeviceGrantStore(world.db_url), user_id="admin", cookie_secret=secret
            ),
        )

    server = _Server()
    await _run_connected_host(monkeypatch, server, on_wait=_login_on_fifth_wait)

    assert server.recovered_at is not None
    assert len(world.refresh_posts) == 2  # the revoked grant once, the new one once


def test_cli_with_a_failing_refresh_endpoint_posts_once_per_backoff(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(cli_auth, "_clock", clock)
    world.refresh_outages[0] = 1
    with _cli_client(world) as cli:
        for _ in range(5):
            assert cli.get("/v1/hosts").status_code == 401
        assert len(world.refresh_posts) == 1
        clock.now += cli_auth.FORCED_REFRESH_BACKOFF_BASE_S
        assert cli.get("/v1/hosts").status_code == 200
    assert len(world.refresh_posts) == 2


async def test_async_cli_refresh_runs_off_the_event_loop(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from omnigent.chat import _DatabricksTokenAuth

    loop_thread = threading.get_ident()
    refresh_threads: list[int] = []
    real_refresh = cli_auth.refresh_stored_token

    def _spy(*args: object, **kwargs: object) -> str | None:
        refresh_threads.append(threading.get_ident())
        return real_refresh(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_auth, "refresh_stored_token", _spy)
    transport = httpx.ASGITransport(app=world.client.app)
    async with httpx.AsyncClient(
        transport=transport, base_url=_SERVER, auth=_DatabricksTokenAuth(server_url=_SERVER)
    ) as cli:
        assert (await cli.get("/v1/hosts")).status_code == 200
    assert refresh_threads and loop_thread not in refresh_threads


def test_runner_with_a_failing_refresh_endpoint_posts_once_per_backoff(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    monkeypatch.setattr(cli_auth, "_clock", _Clock())
    world.refresh_outages[0] = 1
    factory = _make_auth_token_factory(server_url=_SERVER)
    assert factory is not None
    with httpx.Client(
        transport=world.client._transport,
        base_url=_SERVER,
        auth=_RunnerDatabricksAuth(factory, server_url=_SERVER),
    ) as runner:
        for _ in range(5):
            assert runner.get("/v1/hosts").status_code == 401
    assert len(world.refresh_posts) == 1
