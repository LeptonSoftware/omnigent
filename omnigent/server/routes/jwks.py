"""``GET /.well-known/jwks.json``: the public keys that verify session tokens.

fork: unauthenticated by design. A service holding only this document can
verify an Omnigent session token (``alg`` EdDSA, ``kid`` header) offline,
and cannot mint one. Lists the current key, then the previous key during a
rotation. Empty when the server signs HS256 only or has no session tokens.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider, UnifiedAuthProvider
from omnigent.server.session_signing import JWKS_CACHE_CONTROL, provider_signing_keys


def create_jwks_router(auth_provider: AuthProvider | None) -> APIRouter:
    """Build the JWKS router; the document is fixed for the process lifetime.

    :param auth_provider: The app's provider. Only ``oidc`` and ``accounts``
        providers sign session tokens; anything else publishes no keys.
    """
    keys: list[dict[str, str]] = []
    if isinstance(auth_provider, UnifiedAuthProvider) and auth_provider._source in (
        "oidc",
        "accounts",
    ):
        keys = provider_signing_keys(auth_provider).jwks()["keys"]
    body = {"keys": keys}

    router = APIRouter()

    @router.get("/.well-known/jwks.json", include_in_schema=False)
    async def jwks() -> JSONResponse:
        return JSONResponse(body, headers={"Cache-Control": JWKS_CACHE_CONTROL})

    return router
