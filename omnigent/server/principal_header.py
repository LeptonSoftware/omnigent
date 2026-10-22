"""Name the authenticated caller on every HTTP response, for the access log.

This module is FORK-OWNED. The proxy in front of the server logs each request,
but behind NAT every office machine shares one address, so the log cannot say
who sent a request. :func:`install` records the identity the auth provider
resolves, and :class:`PrincipalHeaderMiddleware` returns it as
``X-Omnigent-Principal``. The proxy logs that header and strips it before the
response leaves, so clients never see it. ``OMNIGENT_PRINCIPAL_HEADER=0``
turns it off at install time.
"""

from __future__ import annotations

import functools
import os
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omnigent.server.auth import UnifiedAuthProvider

PRINCIPAL_HEADER_ENV = "OMNIGENT_PRINCIPAL_HEADER"
HEADER = b"x-omnigent-principal"
_STATE_KEY = "omnigent_principal"
_original_get_user_id = UnifiedAuthProvider.get_user_id


def enabled() -> bool:
    """Whether ``OMNIGENT_PRINCIPAL_HEADER`` leaves the header on (default on)."""
    return os.environ.get(PRINCIPAL_HEADER_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


@functools.wraps(_original_get_user_id)
def _recording_get_user_id(self: UnifiedAuthProvider, request: Any) -> str | None:
    user_id = _original_get_user_id(self, request)
    if user_id is not None:
        state = request.scope.get("state")
        if isinstance(state, dict):
            state[_STATE_KEY] = user_id
    return user_id


def _header_value(user_id: str) -> bytes:
    # Header values must be visible ASCII; an id that is not becomes "?".
    cleaned = "".join(c if 0x21 <= ord(c) <= 0x7E else "?" for c in user_id[:200])
    return cleaned.encode("ascii")


class PrincipalHeaderMiddleware:
    """Add ``X-Omnigent-Principal`` to HTTP responses whose caller authenticated."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        # Created here so every inner layer shares this one dict, even if it copies the scope.
        state = scope.setdefault("state", {})

        async def send_with_principal(message: Message) -> None:
            if message["type"] == "http.response.start":
                user_id = state.get(_STATE_KEY)
                if user_id:
                    headers = list(message.get("headers", []))
                    headers.append((HEADER, _header_value(user_id)))
                    message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_principal)


def install() -> bool:
    """Record resolved identities once per process. Returns whether it is active."""
    if not enabled():
        return False
    UnifiedAuthProvider.get_user_id = _recording_get_user_id  # type: ignore[method-assign]
    return True


def uninstall() -> None:
    """Restore the original ``get_user_id`` (tests)."""
    UnifiedAuthProvider.get_user_id = _original_get_user_id  # type: ignore[method-assign]
