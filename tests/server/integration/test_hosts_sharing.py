"""Integration tests for shared team hosts (the /v1/hosts/{id}/share API).

Alice registers a host and shares it with Bob so he can run sessions on
her machine. These tests pin the authorization boundary that sharing
moves: what a grant lets Bob do, and — more importantly — what it must
never let him do.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_launch import resolve_host_access, resolve_host_owner
from omnigent.server.routes.host_sharing import create_host_sharing_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
_HOST_ID = "33296f9b15e02671c34e013dd711407e"


def _as(user: str) -> dict[str, str]:
    """Build request headers authenticating as *user*.

    :param user: The caller's identity, e.g. ``"bob@example.com"``.
    :returns: Headers for the trusted-proxy identity header.
    """
    return {"X-Forwarded-Email": user}


@pytest.fixture()
def share_app(db_uri: str) -> tuple[FastAPI, HostStore]:
    """Auth-enabled FastAPI app with the host REST routes.

    :param db_uri: SQLite URI from the shared fixture.
    :returns: Tuple of (app, host_store).
    """
    host_store = HostStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_hosts_router(
            HostRegistry(),
            host_store,
            SqlAlchemyConversationStore(db_uri),
            auth_provider=UnifiedAuthProvider(source="header"),
        ),
        prefix="/v1",
    )
    app.include_router(
        create_host_sharing_router(host_store, auth_provider=UnifiedAuthProvider(source="header")),
        prefix="/v1",
    )
    return app, host_store


@pytest_asyncio.fixture()
async def client(share_app: tuple[FastAPI, HostStore]) -> AsyncClient:
    """HTTP client wired to the auth-enabled host app.

    :param share_app: The app fixture.
    :yields: A ready-to-use :class:`httpx.AsyncClient`.
    """
    app, _ = share_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture()
def alices_host(share_app: tuple[FastAPI, HostStore]) -> HostStore:
    """Register a host owned by Alice.

    :param share_app: The app fixture.
    :returns: The host store, with Alice's host registered.
    """
    _, host_store = share_app
    host_store.upsert_on_connect(host_id=_HOST_ID, name="alice-laptop", user_id=ALICE)
    return host_store


async def test_share_makes_host_visible_to_grantee(
    client: AsyncClient, alices_host: HostStore
) -> None:
    """
    Verify Bob's picker shows Alice's host only after she shares it.

    The is_owned_by_me flag is what lets the UI label it as someone
    else's machine and hide owner-only affordances.
    """
    before = await client.get("/v1/hosts", headers=_as(BOB))
    assert before.json()["hosts"] == []

    shared = await client.post(
        f"/v1/hosts/{_HOST_ID}/share", json={"user_id": BOB}, headers=_as(ALICE)
    )
    assert shared.status_code == 200
    assert shared.json()["level"] == "use"

    after = await client.get("/v1/hosts", headers=_as(BOB))
    hosts = after.json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["host_id"] == _HOST_ID
    assert hosts[0]["owner"] == ALICE
    assert hosts[0]["is_owned_by_me"] is False

    # Alice still sees it as her own.
    mine = await client.get("/v1/hosts", headers=_as(ALICE))
    assert mine.json()["hosts"][0]["is_owned_by_me"] is True


async def test_grantee_can_reach_host_for_session_launch(
    client: AsyncClient, alices_host: HostStore
) -> None:
    """
    Verify a shared user passes the host-access gate that guards
    session creation.

    This is the whole point of the feature: seeing the host in the
    picker is worthless if the launch path still rejects Bob. A
    visibility-only change (adding shared hosts to the listing without
    relaxing this resolver) leaves Bob 403ing the moment he clicks it.
    """
    alices_host.grant_host_access(_HOST_ID, BOB)

    host = resolve_host_access(user_id=BOB, host_id=_HOST_ID, host_store=alices_host)
    assert host.host_id == _HOST_ID


async def test_non_grantee_still_rejected_everywhere(
    client: AsyncClient, alices_host: HostStore
) -> None:
    """
    Verify sharing with Bob grants Carol nothing.

    Guards the cross-user RCE boundary: the grant must authorize
    exactly its grantee, not "anyone once the host is shared at all".
    """
    alices_host.grant_host_access(_HOST_ID, BOB)

    assert (await client.get("/v1/hosts", headers=_as(CAROL))).json()["hosts"] == []
    detail = await client.get(f"/v1/hosts/{_HOST_ID}", headers=_as(CAROL))
    assert detail.status_code == 403

    with pytest.raises(Exception) as exc:
        resolve_host_access(user_id=CAROL, host_id=_HOST_ID, host_store=alices_host)
    assert getattr(exc.value, "status_code", None) == 403


async def test_read_grant_cannot_reach_the_filesystem(
    client: AsyncClient, alices_host: HostStore
) -> None:
    """
    Verify a "read" grant sees the host but cannot use it.

    "read" is metadata-only, so it must pass the detail endpoint and
    fail the "use"-level gate that guards code execution and file I/O.
    """
    alices_host.grant_host_access(_HOST_ID, BOB, "read")

    detail = await client.get(f"/v1/hosts/{_HOST_ID}", headers=_as(BOB))
    assert detail.status_code == 200

    with pytest.raises(Exception) as exc:
        resolve_host_access(user_id=BOB, host_id=_HOST_ID, host_store=alices_host)
    assert getattr(exc.value, "status_code", None) == 403


async def test_grantee_cannot_reshare_or_manage_host(
    client: AsyncClient, alices_host: HostStore
) -> None:
    """
    Verify a grant confers no authority over the host itself.

    Bob may run code on Alice's laptop, but must not be able to widen
    access to it — otherwise a single share silently becomes a
    transitive one Alice never authorized.
    """
    alices_host.grant_host_access(_HOST_ID, BOB)

    resold = await client.post(
        f"/v1/hosts/{_HOST_ID}/share", json={"user_id": CAROL}, headers=_as(BOB)
    )
    assert resold.status_code == 403
    assert alices_host.get_host_access_level(_HOST_ID, CAROL) is None

    revoked = await client.delete(f"/v1/hosts/{_HOST_ID}/share/{BOB}", headers=_as(BOB))
    assert revoked.status_code == 403
    # Bob also can't enumerate who else Alice shared with.
    assert (await client.get(f"/v1/hosts/{_HOST_ID}/share", headers=_as(BOB))).status_code == 403

    # The owner-only resolver rejects him even though access passes.
    with pytest.raises(Exception) as exc:
        resolve_host_owner(user_id=BOB, host_id=_HOST_ID, host_store=alices_host)
    assert getattr(exc.value, "status_code", None) == 403


async def test_unshare_revokes_access(client: AsyncClient, alices_host: HostStore) -> None:
    """
    Verify the owner can revoke, and the host leaves Bob's picker.
    """
    alices_host.grant_host_access(_HOST_ID, BOB)

    resp = await client.delete(f"/v1/hosts/{_HOST_ID}/share/{BOB}", headers=_as(ALICE))
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True

    assert (await client.get("/v1/hosts", headers=_as(BOB))).json()["hosts"] == []
    with pytest.raises(Exception) as exc:
        resolve_host_access(user_id=BOB, host_id=_HOST_ID, host_store=alices_host)
    assert getattr(exc.value, "status_code", None) == 403


async def test_list_shares_is_owner_only_and_excludes_owner(
    client: AsyncClient, alices_host: HostStore
) -> None:
    """
    Verify the owner-facing "shared with" list reports grantees only.

    The owner isn't a grant, so listing them would imply a revocable
    row that doesn't exist.
    """
    alices_host.grant_host_access(_HOST_ID, BOB, "use")
    alices_host.grant_host_access(_HOST_ID, CAROL, "read")

    resp = await client.get(f"/v1/hosts/{_HOST_ID}/share", headers=_as(ALICE))
    assert resp.status_code == 200
    shares = resp.json()["shares"]
    assert {s["user_id"]: s["level"] for s in shares} == {BOB: "use", CAROL: "read"}


async def test_share_unknown_host_is_404_not_403(client: AsyncClient) -> None:
    """
    Verify an unknown host 404s rather than leaking existence.
    """
    resp = await client.post(
        "/v1/hosts/deadbeefdeadbeefdeadbeefdeadbeef/share",
        json={"user_id": BOB},
        headers=_as(ALICE),
    )
    assert resp.status_code == 404


async def test_share_with_self_rejected(client: AsyncClient, alices_host: HostStore) -> None:
    """
    Verify Alice can't grant herself a redundant row on her own host.
    """
    resp = await client.post(
        f"/v1/hosts/{_HOST_ID}/share", json={"user_id": ALICE}, headers=_as(ALICE)
    )
    assert resp.status_code == 400
    assert alices_host.list_host_grants(_HOST_ID) == []


async def test_share_rejects_unknown_level(client: AsyncClient, alices_host: HostStore) -> None:
    """
    Verify an unknown level is refused at the request boundary.
    """
    resp = await client.post(
        f"/v1/hosts/{_HOST_ID}/share",
        json={"user_id": BOB, "level": "admin"},
        headers=_as(ALICE),
    )
    assert resp.status_code == 422
    assert alices_host.list_host_grants(_HOST_ID) == []


async def test_unauthenticated_caller_cannot_share(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Verify a caller with no identity header cannot share a host.

    The suite sets OMNIGENT_LOCAL_SINGLE_USER, which lets header-auth
    fall back to the reserved "local" user. The provider reads that at
    construction, so this builds its own app with the flag unset to pin
    the deployed multi-user behavior (401) rather than the harness's.
    """
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)

    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(host_id=_HOST_ID, name="alice-laptop", user_id=ALICE)
    app = FastAPI()
    app.include_router(
        create_hosts_router(
            HostRegistry(),
            host_store,
            SqlAlchemyConversationStore(db_uri),
            auth_provider=UnifiedAuthProvider(source="header"),
        ),
        prefix="/v1",
    )
    app.include_router(
        create_host_sharing_router(host_store, auth_provider=UnifiedAuthProvider(source="header")),
        prefix="/v1",
    )

    # require_user raises; the assembled app maps this to 401, but this
    # bare router app has no handler mounted, so assert the raise.
    with pytest.raises(OmnigentError, match="Authentication required"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post(f"/v1/hosts/{_HOST_ID}/share", json={"user_id": BOB})

    assert host_store.list_host_grants(_HOST_ID) == []


async def test_local_fallback_user_cannot_share_another_users_host(
    client: AsyncClient, alices_host: HostStore
) -> None:
    """
    Verify the single-user "local" fallback identity can't share a host
    owned by a real user.

    With OMNIGENT_LOCAL_SINGLE_USER set (as the suite does), a
    header-less caller resolves to "local". That identity must still
    fail the owner check rather than inherit Alice's authority.
    """
    resp = await client.post(f"/v1/hosts/{_HOST_ID}/share", json={"user_id": BOB})
    assert resp.status_code == 403
    assert alices_host.list_host_grants(_HOST_ID) == []
