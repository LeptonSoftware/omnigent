"""The bare ``GET /health`` probe is answered ahead of the middleware stack."""

from __future__ import annotations

import asyncio
from typing import Any

from omnigent.server.health_fast_path import HealthFastPathMiddleware


async def _call(mw: HealthFastPathMiddleware, scope: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await mw(scope, receive, send)
    return sent


def _scope(path: str, *, method: str = "GET", query: bytes = b"") -> dict[str, Any]:
    return {"type": "http", "method": method, "path": path, "query_string": query}


class _Downstream:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1


def test_bare_health_is_answered_without_the_app() -> None:
    app = _Downstream()
    sent = asyncio.run(_call(HealthFastPathMiddleware(app), _scope("/health")))
    assert app.calls == 0
    assert sent[0]["status"] == 200
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"application/json"
    assert len(headers[b"x-request-id"]) == 32
    assert sent[1]["body"] == b'{"status":"ok"}'


def test_session_liveness_and_other_paths_pass_through() -> None:
    app = _Downstream()
    mw = HealthFastPathMiddleware(app)
    asyncio.run(_call(mw, _scope("/health", query=b"session_ids=a,b")))
    asyncio.run(_call(mw, _scope("/health", method="POST")))
    asyncio.run(_call(mw, _scope("/v1/sessions")))
    assert app.calls == 3


def test_base_path_prefixed_health_is_answered() -> None:
    app = _Downstream()
    sent = asyncio.run(
        _call(HealthFastPathMiddleware(app, base_path="/omni/"), _scope("/omni/health"))
    )
    assert app.calls == 0
    assert sent[1]["body"] == b'{"status":"ok"}'
