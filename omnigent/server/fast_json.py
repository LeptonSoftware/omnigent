"""Encode pydantic responses in one pass instead of FastAPI's Python walk.

This module is FORK-OWNED. Most hot routes declare ``response_model=None`` and
return a pydantic model, so FastAPI skips its own fast path and serializes with
``jsonable_encoder``: ``model_dump(mode="json")`` followed by a recursive
Python walk over the result, then ``json.dumps``. On large session snapshots
that walk was about half of the event loop's busy time.

:func:`install` swaps in ``model_dump_json(by_alias=True)``, the same pydantic
serializers in one Rust pass. The parsed JSON is the same. Two byte-level
differences are accepted: float formatting (``1e-05`` vs ``0.00001``, same
number), and dict keys starting with ``_sa`` inside free-form fields are kept,
where ``jsonable_encoder`` drops them as SQLAlchemy state. A NaN or infinity
becomes ``null``; the slow path fails with a 500 on it (``allow_nan=False``).

Only the ``field is None`` + ``BaseModel`` case changes. Routes with a
``response_model`` already use FastAPI's own fast path, and plain dicts,
lists and other values still go through ``jsonable_encoder``. Status codes,
background tasks and headers set on an injected ``Response`` are still applied
by FastAPI, because the hook sits at ``serialize_response``, not the endpoint.
``OMNIGENT_FAST_JSON=0`` turns it off at install time.
"""

from __future__ import annotations

import os
from typing import Any

import fastapi.routing
from pydantic import BaseModel
from starlette.responses import JSONResponse

FAST_JSON_ENV = "OMNIGENT_FAST_JSON"


class PreEncodedJSON(bytes):
    """Response body bytes already encoded as JSON."""


_original_serialize_response = fastapi.routing.serialize_response
_original_render = JSONResponse.render


async def _serialize_response(*, field: Any = None, response_content: Any, **kwargs: Any) -> Any:
    # FastAPI's field-less branch calls ``jsonable_encoder(response_content)``
    # with defaults only (by_alias=True, no include/exclude), so this mirrors it.
    if field is None and isinstance(response_content, BaseModel):
        return PreEncodedJSON(response_content.model_dump_json(by_alias=True).encode("utf-8"))
    return await _original_serialize_response(
        field=field, response_content=response_content, **kwargs
    )


def _render(self: JSONResponse, content: Any) -> bytes:
    if isinstance(content, PreEncodedJSON):
        return bytes(content)
    return _original_render(self, content)


def enabled() -> bool:
    """Whether ``OMNIGENT_FAST_JSON`` leaves the fast path on (default on)."""
    return os.environ.get(FAST_JSON_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def install() -> bool:
    """Install the fast path once per process. Returns whether it is active."""
    if not enabled():
        return False
    fastapi.routing.serialize_response = _serialize_response  # type: ignore[assignment]
    JSONResponse.render = _render  # type: ignore[method-assign]
    return True


def uninstall() -> None:
    """Restore FastAPI's and Starlette's originals (tests)."""
    fastapi.routing.serialize_response = _original_serialize_response
    JSONResponse.render = _original_render  # type: ignore[method-assign]
