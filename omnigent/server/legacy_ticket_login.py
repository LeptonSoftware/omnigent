"""Fork: keep pre-PKCE CLI-ticket login working for clients not yet updated.

Ticket login now requires a PKCE challenge plus browser consent, and an old
client's empty ``cli-login`` request only gets an "update to sign in" page.
Store builds of the mobile apps (iOS 0.3.1) predate that, so they could not
sign in at all. This keeps the old style alongside the new one.

An empty ``cli-login`` body gets a real ticket the old way: the browser's IdP
sign-in fulfills it directly (no consent page) and ``cli-poll`` returns the
token without a verifier. Clients that send a challenge keep the full flow,
and IdP re-authentication still applies to every ticket login.

Accepted trade-off: whoever holds a legacy ticket id collects the session once
the browser signs in, so a crafted login link can still hijack a login.
Deprecated: disable with ``OMNIGENT_OIDC_LEGACY_TICKET_LOGIN=0`` and remove
once the store apps ship PKCE login (iOS/Android 0.3.2+).
"""

from __future__ import annotations

import html
import logging
import os
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from starlette.responses import HTMLResponse, JSONResponse, Response

from omnigent.server.routes._oauth import NO_STORE_HEADERS

if TYPE_CHECKING:
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.oidc import OIDCConfig

_logger = logging.getLogger(__name__)

LEGACY_TICKET_LOGIN_ENV = "OMNIGENT_OIDC_LEGACY_TICKET_LOGIN"


def enabled() -> bool:
    """Return whether pre-PKCE ticket login is accepted (on unless set to ``0``)."""
    return os.environ.get(LEGACY_TICKET_LOGIN_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def create_ticket(tickets: dict[str, Any], make_ticket: Callable[[], Any]) -> Response:
    """Mint a legacy ticket and answer with the pre-#8505 response shape.

    :param tickets: The router's pending-ticket map; the new ticket is stored here.
    :param make_ticket: Builds an empty ``_CliTicket`` (no ``code_challenge``),
        which is what marks it as legacy everywhere else.
    :returns: ``{"ticket", "login_url"}``, as old clients expect.
    """
    ticket_id = secrets.token_urlsafe(32)
    tickets[ticket_id] = make_ticket()
    _logger.info("cli-login: issued legacy (pre-PKCE) ticket for an old client")
    return JSONResponse(
        status_code=200,
        content={"ticket": ticket_id, "login_url": f"/auth/login?ticket={ticket_id}"},
        headers={"Cache-Control": "no-store"},
    )


def fulfill_in_callback(
    ticket: Any,
    *,
    email: str,
    session_jwt: str,
    ttl_seconds: int,
    config: OIDCConfig,
    device_grant_store: DeviceGrantStore | None,
    issue_login_grant: Callable[..., str],
) -> Response | None:
    """Complete a legacy ticket when the browser's IdP sign-in returns.

    :returns: The "Login successful" page, a refusal when the ticket was
        already signed in, or ``None`` when *ticket* is not a legacy ticket or
        has expired, so the caller keeps its own handling.
    """
    if ticket.code_challenge or time.time() - ticket.created_at > ttl_seconds:
        return None
    if ticket.token is not None:
        # One sign-in per ticket. A second would swap whose session the
        # waiting client receives, letting a link holder sign it in as them.
        _logger.warning("cli-login: refused a second sign-in on a legacy ticket (%s)", email)
        return HTMLResponse(
            status_code=409,
            content=(
                "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
                "<h2>This login link was already used</h2>"
                "<p>Start a new login from the app or CLI.</p>"
                "</body></html>"
            ),
            headers={"Cache-Control": "no-store"},
        )
    ticket.token = session_jwt
    ticket.user_id = email
    if device_grant_store is not None:
        try:
            ticket.refresh_token = issue_login_grant(
                device_grant_store,
                user_id=email,
                cookie_secret=config.cookie_secret,
            )
        except Exception:
            _logger.exception("cli-login: refresh grant issuance failed (legacy ticket)")
    _logger.info("cli-login: %s completed a legacy ticket login", email)
    safe_email = html.escape(email)
    return HTMLResponse(
        content=(
            "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
            "<h2>Login successful</h2>"
            f"<p>Authenticated as <strong>{safe_email}</strong>.</p>"
            "<p>You can close this tab and return to where you started signing in.</p>"
            "</body></html>"
        ),
        headers={"Cache-Control": "no-store"},
    )


def poll_response(
    tickets: dict[str, Any], ticket_id: str, ticket: Any, *, expires_in: int
) -> Response:
    """Answer ``cli-poll`` for a legacy ticket, which has no verifier to check.

    :returns: 202 while pending, else 200 with the session (once; the ticket
        is consumed).
    """
    if ticket.token is None:
        return JSONResponse(
            status_code=202, content={"status": "pending"}, headers=NO_STORE_HEADERS
        )
    del tickets[ticket_id]
    content: dict[str, object] = {
        "token": ticket.token,
        "user_id": ticket.user_id,
        "expires_in": expires_in,
    }
    if ticket.refresh_token is not None:
        content["refresh_token"] = ticket.refresh_token
    return JSONResponse(status_code=200, content=content, headers=NO_STORE_HEADERS)
