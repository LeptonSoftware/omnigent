"""Share/unshare endpoints for shared team hosts — a FORK-OWNED router.

Upstream has no file here, so these routes never conflict on a merge and an
upstream refactor of ``hosts.py`` (it split ``sessions.py`` into a package once
already) cannot orphan them. Mounting costs one ``include_router`` call.

Every endpoint here is OWNER-ONLY, deliberately: sharing is authority over the
host itself, so a grantee cannot re-share, enumerate, or revoke. That keeps a
grant non-transitive — a teammate you let run code on your laptop cannot widen
that access to anyone else.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.stores.host_store import HOST_LEVEL_USE, HostStore


class ShareHostRequest(BaseModel):
    """Request body for ``POST /v1/hosts/{host_id}/share``.

    :param user_id: The teammate to grant access to, e.g. ``"bob@example.com"``.
    :param level: ``"read"`` to let them see the host in their picker, or
        ``"use"`` (default) to additionally let them run sessions on it and
        browse its files.
    """

    user_id: str
    level: Literal["read", "use"] = HOST_LEVEL_USE


def create_host_sharing_router(
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for host share/unshare endpoints.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/hosts/{id}/share``.

    :param host_store: Persistent store for host registrations and grants.
    :param auth_provider: Optional auth provider for user identity.
    :returns: A FastAPI router with the sharing endpoints.
    """
    router = APIRouter()

    @router.post("/hosts/{host_id}/share")
    async def share_host(request: Request, host_id: str, body: ShareHostRequest) -> dict[str, Any]:
        """Grant a teammate access to a host you own.

        Only the owner may share, and sharing does not transfer ownership — the
        grantee cannot re-share or delete the host. Idempotent: re-sharing to
        the same user updates their level.

        :param request: The incoming request (for auth).
        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        :param body: The grantee and access level.
        :returns: The resulting grant.
        :raises HTTPException: 404 if the host does not exist; 403 if the caller
            does not own it; 400 if sharing with yourself.
        """
        user_id = require_user(request, auth_provider)
        # resolve_host_owner, NOT resolve_host_access: a grantee must not be
        # able to widen access to someone else's machine.
        await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        if user_id is not None and body.user_id == user_id:
            raise HTTPException(status_code=400, detail="cannot share a host with yourself")

        await asyncio.to_thread(host_store.grant_host_access, host_id, body.user_id, body.level)
        return {"host_id": host_id, "user_id": body.user_id, "level": body.level}

    @router.get("/hosts/{host_id}/share")
    async def list_host_shares(request: Request, host_id: str) -> dict[str, list[dict[str, Any]]]:
        """List who a host you own is shared with.

        :param request: The incoming request (for auth).
        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        :returns: ``{"shares": [...]}``, oldest grant first. The owner is not
            listed — ownership is not a grant.
        :raises HTTPException: 404 if the host does not exist; 403 if the caller
            does not own it.
        """
        user_id = require_user(request, auth_provider)
        await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        grants = await asyncio.to_thread(host_store.list_host_grants, host_id)
        return {
            "shares": [
                {"user_id": g.user_id, "level": g.level, "created_at": g.created_at}
                for g in grants
            ]
        }

    @router.delete("/hosts/{host_id}/share/{shared_user_id}")
    async def unshare_host(request: Request, host_id: str, shared_user_id: str) -> dict[str, Any]:
        """Revoke a teammate's access to a host you own.

        Sessions the grantee already started keep running — this stops new
        launches. Stop them explicitly if that matters.

        :param request: The incoming request (for auth).
        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        :param shared_user_id: The grantee to revoke, e.g. ``"bob@example.com"``.
        :returns: ``{"revoked": bool}`` — ``False`` when no grant existed.
        :raises HTTPException: 404 if the host does not exist; 403 if the caller
            does not own it.
        """
        user_id = require_user(request, auth_provider)
        await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        revoked = await asyncio.to_thread(host_store.revoke_host_access, host_id, shared_user_id)
        return {"host_id": host_id, "user_id": shared_user_id, "revoked": revoked}

    return router
