"""Answer the bare liveness probe ahead of the middleware stack.

This module is FORK-OWNED. A container health check polls ``GET /health`` with
no query. Its answer is a constant, but on the normal path it still runs every
middleware (request ids, audit events, identity lookup, routing) and queues
behind them on the shared event loop. This outermost ASGI layer replies before
any of that. Requests with a query string (the per-session liveness batch) and
every other path pass through untouched.
"""

from __future__ import annotations

import uuid

from starlette.types import ASGIApp, Receive, Scope, Send

_BODY = b'{"status":"ok"}'
_HEADERS = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_BODY)).encode("ascii")),
]


class HealthFastPathMiddleware:
    """Reply to a bare ``GET /health`` without entering the app."""

    def __init__(self, app: ASGIApp, base_path: str = "") -> None:
        self._app = app
        prefix = base_path.rstrip("/")
        self._paths = {"/health", f"{prefix}/health"} if prefix else {"/health"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and scope.get("path") in self._paths
            and not scope.get("query_string")
        ):
            # Clients still get a correlation id, as on every other response.
            headers = [*_HEADERS, (b"x-request-id", uuid.uuid4().hex.encode("ascii"))]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": _BODY})
            return
        await self._app(scope, receive, send)
