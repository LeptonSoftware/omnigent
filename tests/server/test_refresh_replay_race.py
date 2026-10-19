"""Two concurrent presentations of one rotating refresh token revoke the grant.

The refresh handler runs in a worker thread, so both requests can pass the
current-token lookup before either rotates. The loser must still be treated as
a replay and revoke the grant, as a sequential second presentation would be.
"""

from __future__ import annotations

import concurrent.futures
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.server.test_device_auth import _build_accounts_app, _exchange_device_code, _login_admin


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_accounts_app(tmp_path, monkeypatch)


def _rotating_refresh_token(app: TestClient) -> str:
    r = app.post("/oauth/device/authorize", json={"client_id": "slack"})
    data = r.json()
    _login_admin(app)
    app.post(
        "/oauth/device/approve",
        data={"user_code": data["user_code"]},
        headers={"Origin": "http://localhost:8000"},
    )
    r = _exchange_device_code(app, data["device_code"])
    assert r.status_code == 200, r.text
    return r.json()["refresh_token"]


def test_concurrent_replay_with_slow_lookup(
    app: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same race with a 200 ms store lookup (a loaded DB), so the two reads overlap."""
    import time

    from omnigent.server import device_grant_store as dg

    token = _rotating_refresh_token(app)
    app.cookies.clear()
    original = dg.DeviceGrantStore.get_by_refresh_hash

    def slow(self, *a, **k):
        out = original(self, *a, **k)
        time.sleep(0.2)
        return out

    monkeypatch.setattr(dg.DeviceGrantStore, "get_by_refresh_hash", slow)

    def refresh(_: int):
        return app.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token}
        )

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(refresh, range(2)))
    codes = Counter(r.status_code for r in responses)
    winners = [r.json()["refresh_token"] for r in responses if r.status_code == 200]
    monkeypatch.setattr(dg.DeviceGrantStore, "get_by_refresh_hash", original)
    assert len(winners) == 1, dict(codes)
    follow = app.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": winners[0]}
    )
    assert follow.status_code != 200, (
        "replayed rotating refresh token raced past reuse detection; grant still live "
        f"(outcomes={dict(codes)}, losers={[r.json() for r in responses if r.status_code != 200]})"
    )
