"""One-pass pydantic JSON for ``response_model=None`` routes."""

from __future__ import annotations

import datetime as dt
import enum
import json
import math
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Response
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from omnigent.server import fast_json


class Color(enum.Enum):
    RED = "red"


class Inner(BaseModel):
    when: dt.datetime
    color: Color
    ident: uuid.UUID


class Outer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    display_name: str = Field(alias="displayName")
    cost: float
    tiny: float
    missing: float | None
    unicode: str
    inner: Inner
    items: list[Inner]
    free: dict[str, Any]
    big: int


def _sample() -> Outer:
    inner = Inner(
        when=dt.datetime(2026, 10, 18, 17, 53, 1, 123456, tzinfo=dt.timezone.utc),
        color=Color.RED,
        ident=uuid.UUID("12345678-1234-5678-1234-567812345678"),
    )
    return Outer(
        displayName="naïve 🚀",
        cost=0.1 + 0.2,
        tiny=1e-05,
        missing=None,
        unicode='एआई   "quoted" \\',
        inner=inner,
        items=[inner, inner],
        free={"nested": inner, "list": [1, 2.5, None, True], "when": inner.when},
        big=2**63 + 1,
    )


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/model", response_model=None)
    async def model_route(response: Response) -> Outer:
        response.headers["x-from-route"] = "kept"
        response.status_code = 201
        return _sample()

    @app.get("/dict", response_model=None)
    async def dict_route() -> dict[str, Any]:
        return {"inner": _sample().inner, "_sa_state": 1, "n": 1}

    @app.get("/typed")
    async def typed_route() -> Inner:
        return _sample().inner

    return app


@pytest.fixture
def fast() -> Iterator[None]:
    assert fast_json.install()
    try:
        yield
    finally:
        fast_json.uninstall()


def _get(path: str) -> Any:
    return TestClient(_app()).get(path)


def test_model_route_uses_one_pass_encoding(fast: None) -> None:
    resp = _get("/model")
    assert resp.content == _sample().model_dump_json(by_alias=True).encode("utf-8")


def test_model_route_parses_identically_to_jsonable_encoder(fast: None) -> None:
    resp = _get("/model")
    assert resp.json() == json.loads(json.dumps(jsonable_encoder(_sample())))
    assert "displayName" in resp.json()


def test_route_status_and_headers_still_apply(fast: None) -> None:
    resp = _get("/model")
    assert resp.status_code == 201
    assert resp.headers["x-from-route"] == "kept"
    assert resp.headers["content-type"] == "application/json"
    assert int(resp.headers["content-length"]) == len(resp.content)


def test_dict_and_typed_routes_are_unchanged(fast: None) -> None:
    fast_dict, fast_typed = _get("/dict"), _get("/typed")
    fast_json.uninstall()
    assert fast_dict.content == _get("/dict").content
    assert "_sa_state" not in fast_dict.json()
    assert fast_typed.content == _get("/typed").content


def test_nan_becomes_null_where_the_slow_path_cannot_render(fast: None) -> None:
    model = _sample().model_copy(update={"cost": math.nan})
    assert json.loads(model.model_dump_json(by_alias=True))["cost"] is None
    with pytest.raises(ValueError):
        json.dumps(jsonable_encoder(model), allow_nan=False)


def test_env_switch_disables_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fast_json.FAST_JSON_ENV, "0")
    assert fast_json.install() is False
    resp = _get("/model")
    assert resp.content != _sample().model_dump_json(by_alias=True).encode("utf-8")
    assert resp.json() == json.loads(json.dumps(jsonable_encoder(_sample())))
