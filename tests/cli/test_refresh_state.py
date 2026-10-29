"""The machine-wide refresh state and ``omnigent auth refresh``.

fork (VIN-3238 QA round 4): the state file must never break a refresh (bad
types, a counter that would overflow), a token that lived before it was
refused earns a refresh (key rotation), routine refreshes back off too, a
dead grant is re-checked hourly, and a login takes the refresh lock. Non-
Python clients refresh only through the subcommand, whose exit codes are a
contract with jarvis-cli.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from omnigent import cli_auth
from omnigent.cli_auth_refresh import auth_group

_SERVER = "http://localhost:8000"


@dataclass
class _TokenEndpoint:
    """Stand-in for ``/oauth/token``: ``ok``, ``503``, ``dead`` or ``404``."""

    mode: str = "ok"
    posts: list[str] = field(default_factory=list)
    issued: int = 0

    def __call__(
        self, url: str, *, data: dict[str, str] | None = None, timeout: float | None = None
    ) -> httpx.Response:
        assert data is not None
        self.posts.append(data["refresh_token"])
        request = httpx.Request("POST", url)
        if self.mode == "503":
            return httpx.Response(503, json={"error": "temporarily_unavailable"}, request=request)
        if self.mode == "dead":
            return httpx.Response(400, json={"error": "invalid_grant"}, request=request)
        if self.mode == "404":
            return httpx.Response(404, request=request)
        self.issued += 1
        body = {
            "access_token": f"issued-{self.issued}",
            "refresh_token": data["refresh_token"],
            "expires_in": 3600,
        }
        return httpx.Response(200, json=body, request=request)


@dataclass
class _Box:
    root: Path
    endpoint: _TokenEndpoint
    now: list[float]

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds

    def state(self) -> dict[str, object]:
        return json.loads((self.root / cli_auth.REFRESH_STATE_FILE_NAME).read_text())

    def record(self, refresh_token: str = "refresh-1") -> dict[str, object]:
        server = self.state()[_SERVER]
        assert isinstance(server, dict)
        return server["grants"][_sha(refresh_token)]

    def write_record(self, record: object, refresh_token: str = "refresh-1") -> None:
        state = {_SERVER: {"grants": {_sha(refresh_token): record}, "refused_access": []}}
        (self.root / cli_auth.REFRESH_STATE_FILE_NAME).write_text(json.dumps(state))


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Box]:
    monkeypatch.setattr(cli_auth, "_token_file_path", lambda: tmp_path / "auth_tokens.json")
    monkeypatch.setattr(cli_auth, "_jitter", lambda low, high: 1.0)
    now = [1_000_000.0]
    monkeypatch.setattr(cli_auth, "_clock", lambda: now[0])
    endpoint = _TokenEndpoint()
    monkeypatch.setattr(httpx, "post", endpoint)
    yield _Box(tmp_path, endpoint, now)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _store(token: str, *, expires_in: float = 3600.0, refresh_token: str = "refresh-1") -> None:
    cli_auth.store_token(
        _SERVER,
        token=token,
        user_id="u",
        expires_at=time.time() + expires_in,
        refresh_token=refresh_token,
    )


# ── The state never breaks a refresh ─────────────────────────────


def test_a_huge_failure_count_cannot_overflow_the_backoff(box: _Box) -> None:
    _store("login")
    box.endpoint.mode = "503"
    box.write_record({"outcome": "transient", "failures": 10**6})
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") is None
    wait = float(box.record()["next_allowed"]) - box.now[0]  # type: ignore[arg-type]
    assert wait == cli_auth.FORCED_REFRESH_BACKOFF_CAP_S


def test_any_successful_refresh_resets_the_failure_count(box: _Box) -> None:
    _store("login")
    box.write_record({"outcome": "transient", "failures": 7})
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") == "issued-1"
    assert box.record()["failures"] == 0


@pytest.mark.parametrize(
    "record",
    [
        {"failures": "x", "outcome": "transient", "next_allowed": "soon"},
        {"failures": True, "next_allowed": float("inf"), "outcome": 7},
        {"produced": 5, "produced_at": "yesterday", "outcome": "ok", "fresh_refusals": -3},
        "not a record",
        ["nor", "this"],
    ],
)
def test_a_malformed_state_record_reads_as_absent(box: _Box, record: object) -> None:
    _store("login")
    box.write_record(record)
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") == "issued-1"
    assert len(box.endpoint.posts) == 1


@pytest.mark.parametrize("counter", ["x", True, -3, 2.5, None])
def test_a_malformed_failure_count_reads_as_zero(box: _Box, counter: object) -> None:
    """The failure path reads the counter too: it must back off, not error."""
    _store("login")
    box.endpoint.mode = "503"
    box.write_record({"outcome": "transient", "failures": counter})
    result = cli_auth.refresh_login(_SERVER, refused_sha256=_sha("login"))
    assert (result.status, result.retry_after) == (
        "backoff",
        cli_auth.FORCED_REFRESH_BACKOFF_BASE_S,
    )
    assert box.record()["failures"] == 1


def test_a_malformed_state_file_reads_as_absent(box: _Box) -> None:
    _store("login")
    (box.root / cli_auth.REFRESH_STATE_FILE_NAME).write_text(
        json.dumps({_SERVER: {"grants": [1], "refused_access": {"a": 1}}})
    )
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") == "issued-1"


def test_a_state_error_never_costs_the_host_its_bearer(
    box: _Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner._entry import _invalidate_auth_token_factory, _make_auth_token_factory

    _store("login")
    factory = _make_auth_token_factory(server_url=_SERVER)
    assert factory is not None and factory() == "login"

    def _broken(attempts: int) -> float:
        raise OverflowError("state arithmetic")

    monkeypatch.setattr(cli_auth, "_backoff_step", _broken)
    box.endpoint.mode = "503"
    assert _invalidate_auth_token_factory(factory)
    assert factory() == "login"  # the forced refresh failed; the bearer did not
    assert cli_auth.refresh_login(_SERVER, refused_sha256=_sha("login")).status == "error"


# ── A refused token that lived earns a refresh ───────────────────


def test_a_routine_token_refused_after_a_key_rotation_is_refreshed(box: _Box) -> None:
    _store("login", expires_in=30)  # inside the renewal window
    assert cli_auth.refresh_stored_token(_SERVER) == "issued-1"
    box.advance(3000)  # an hour later the server rotates its key
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="issued-1") == "issued-2"
    assert len(box.endpoint.posts) == 2


def test_a_token_refused_right_after_its_refresh_is_not_refreshed_again(box: _Box) -> None:
    _store("login", expires_in=30)
    assert cli_auth.refresh_stored_token(_SERVER) == "issued-1"
    box.advance(cli_auth.FRESH_TOKEN_WINDOW_S - 1)
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="issued-1") is None
    assert len(box.endpoint.posts) == 1


def test_a_server_refusing_every_fresh_token_stays_bounded(box: _Box) -> None:
    _store("login")
    token, start = "login", box.now[0]
    while box.now[0] - start < 6 * 3600:  # a caller every 5 s for 6 h
        token = cli_auth.refresh_stored_token(_SERVER, rejected_token=token) or token
        box.advance(5)
    assert len(box.endpoint.posts) <= 6 * 3600 / 150


# ── Routine refreshes back off too ───────────────────────────────


def test_a_failing_routine_refresh_backs_off_and_keeps_the_valid_token(box: _Box) -> None:
    from omnigent.runner._entry import _make_auth_token_factory

    _store("near-expiry", expires_in=30)
    box.endpoint.mode = "503"
    factory = _make_auth_token_factory(server_url=_SERVER)
    assert factory is not None
    for _ in range(10):
        assert factory() == "near-expiry"
        box.advance(0.1)
    assert len(box.endpoint.posts) == 1
    assert cli_auth.refresh_login(_SERVER).status == "ok"
    box.advance(cli_auth.FORCED_REFRESH_BACKOFF_BASE_S)
    box.endpoint.mode = "ok"
    assert factory() == "issued-1"


def test_an_expired_token_reports_the_backoff(box: _Box) -> None:
    _store("expired", expires_in=-10)
    box.endpoint.mode = "503"
    assert cli_auth.refresh_login(_SERVER).status == "backoff"
    result = cli_auth.refresh_login(_SERVER)
    assert result.status == "backoff" and result.retry_after > 0
    assert len(box.endpoint.posts) == 1


# ── A dead grant is re-checked hourly ────────────────────────────


def test_a_dead_grant_is_rechecked_once_an_hour(box: _Box) -> None:
    _store("login")
    box.endpoint.mode = "dead"
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") is None
    box.endpoint.mode = "ok"  # the invalid_grant was spurious
    box.advance(cli_auth.DEAD_GRANT_RECHECK_S - 1)
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") is None
    assert len(box.endpoint.posts) == 1
    box.advance(2)
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") == "issued-1"


def test_a_really_dead_grant_costs_one_post_an_hour(box: _Box) -> None:
    _store("login")
    box.endpoint.mode = "dead"
    for _ in range(3 * 60):  # a caller every minute for 3 h
        cli_auth.refresh_stored_token(_SERVER, rejected_token="login")
        box.advance(60)
    assert len(box.endpoint.posts) == 3


def test_the_dead_recheck_is_jittered(box: _Box, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_auth, "_jitter", lambda low, high: high)
    _store("login")
    box.endpoint.mode = "dead"
    result = cli_auth.refresh_login(_SERVER, refused_sha256=_sha("login"))
    assert result.retry_after == cli_auth.DEAD_GRANT_RECHECK_S * cli_auth.FORCED_REFRESH_JITTER[1]


# ── A login takes the refresh lock ───────────────────────────────


def test_storing_a_login_waits_for_a_refresh_in_progress(box: _Box) -> None:
    held = threading.Event()
    release = threading.Event()

    def _refresh_in_progress() -> None:
        with cli_auth._token_file_lock():
            held.set()
            release.wait(5)

    holder = threading.Thread(target=_refresh_in_progress)
    holder.start()
    held.wait(5)
    stored = threading.Event()
    writer = threading.Thread(target=lambda: (_store("login"), stored.set()))
    writer.start()
    assert not stored.wait(0.3)  # blocked on the lock
    release.set()
    assert stored.wait(5)
    holder.join()
    writer.join()


def test_a_refresh_stores_its_result_under_its_own_lock(box: _Box) -> None:
    """The lock is re-entrant, so the refresh's own store does not deadlock."""
    _store("login")
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") == "issued-1"
    assert cli_auth.load_token(_SERVER) == "issued-1"


# ── `omnigent auth refresh` ──────────────────────────────────────


def _run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(auth_group, ["refresh", "--server", _SERVER, *args])
    assert result.exception is None or isinstance(result.exception, SystemExit)
    return result.exit_code, result.stdout, result.stderr


def test_subcommand_refreshes_and_exits_0_without_printing_a_token(box: _Box) -> None:
    _store("login")
    code, out, err = _run("--refused-sha256", _sha("login"), "--json")
    assert code == 0
    assert json.loads(out) == {"status": "ok", "retry_after": 0}
    assert cli_auth.load_token(_SERVER) == "issued-1"
    assert "issued-1" not in out + err and "login" not in out


def test_subcommand_exits_0_when_the_stored_token_is_already_newer(box: _Box) -> None:
    _store("newer")
    code, _, _ = _run("--refused-sha256", _sha("older"))
    assert code == 0 and box.endpoint.posts == []


def test_subcommand_exits_10_for_a_dead_login(box: _Box) -> None:
    _store("login")
    box.endpoint.mode = "dead"
    code, out, err = _run("--refused-sha256", _sha("login"), "--json")
    assert code == 10
    assert json.loads(out)["status"] == "dead"
    assert "omnigent login" in err


def test_subcommand_exits_10_with_no_stored_login(box: _Box) -> None:
    code, _, _ = _run()
    assert code == 10


def test_subcommand_exits_11_with_the_wait_on_stderr(box: _Box) -> None:
    _store("login")
    box.endpoint.mode = "503"
    code, out, err = _run("--refused-sha256", _sha("login"), "--json")
    assert code == 11
    assert json.loads(out) == {"status": "backoff", "retry_after": 5}
    assert err.strip().splitlines()[-1] == "retry_after=5"
    code, _, err = _run("--refused-sha256", _sha("login"))
    assert code == 11 and err.strip().splitlines()[-1] == "retry_after=5"
    assert len(box.endpoint.posts) == 1


@pytest.mark.parametrize("digest", ["abc", "z" * 64, _sha("x") + "0"])
def test_subcommand_exits_1_for_a_malformed_digest(box: _Box, digest: str) -> None:
    _store("login")
    code, out, _ = _run("--refused-sha256", digest, "--json")
    assert code == 1 and json.loads(out)["status"] == "error"
    assert box.endpoint.posts == []


def test_subcommand_exits_1_when_the_server_has_no_refresh_endpoint(box: _Box) -> None:
    _store("login")
    box.endpoint.mode = "404"
    code, _, _ = _run("--refused-sha256", _sha("login"))
    assert code == 1


def test_subcommand_exits_1_on_an_unexpected_error(
    box: _Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> cli_auth.RefreshResult:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli_auth, "refresh_login", _boom)
    code, _, err = _run()
    assert code == 1 and "unexpected" in err


def test_subcommand_is_registered_on_the_omnigent_cli() -> None:
    from omnigent.cli import cli

    result = CliRunner().invoke(cli, ["auth", "refresh", "--help"])
    assert result.exit_code == 0
    assert "--refused-sha256" in result.stdout and "retry_after=<seconds>" in result.stdout


def test_subcommand_exit_codes_do_not_collide_with_usage_errors(box: _Box) -> None:
    from omnigent import cli_auth_refresh

    usage = CliRunner().invoke(auth_group, ["refresh"])  # --server missing
    assert usage.exit_code == 2
    codes = {cli_auth_refresh.EXIT_DEAD, cli_auth_refresh.EXIT_BACKOFF}
    assert codes == {10, 11}


# ── Round 5: lock file, org id, process hygiene ──────────────────


def test_a_read_only_lock_file_does_not_break_login_or_refresh(box: _Box) -> None:
    """A lock file left 0444 (e.g. by `sudo -E omnigent`) is still lockable."""
    lock = box.root / "auth_tokens.lock"
    lock.touch()
    lock.chmod(0o444)
    _store("login")
    cli_auth.clear_token(_SERVER)
    _store("login")
    assert cli_auth.refresh_stored_token(_SERVER, rejected_token="login") == "issued-1"


def test_an_org_id_write_never_clobbers_a_concurrent_refresh(box: _Box) -> None:
    _store("login")
    held = threading.Event()
    release = threading.Event()

    def _refresh_in_progress() -> None:
        with cli_auth._token_file_lock():
            held.set()
            release.wait(5)
            _store("renewed")  # the refresh's result, stored under its lock

    holder = threading.Thread(target=_refresh_in_progress)
    holder.start()
    held.wait(5)
    writer = threading.Thread(target=cli_auth.store_databricks_org_id, args=(_SERVER, "42"))
    writer.start()
    time.sleep(0.2)  # the writer is now blocked on the lock
    release.set()
    holder.join()
    writer.join()
    entry = cli_auth._load_entry(_SERVER)
    assert entry is not None
    assert (entry["token"], entry.get("org_id")) == ("renewed", "42")


def _clean_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_")}
    env["HOME"] = str(home)
    return env


def _run_omnigent(cwd: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run `omnigent` as its console script does: no cwd on sys.path (-P)."""
    env = _clean_env(home)
    code = "import sys; from omnigent.cli import main; sys.argv[0] = 'omnigent'; main()"
    return subprocess.run(
        [sys.executable, "-P", "-c", code, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_auth_refresh_imports_nothing_from_the_cwd_and_writes_no_log(tmp_path: Path) -> None:
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    marker = tmp_path / "planted-module-ran"
    for name in ("queue", "sniffio", "anyio", "h11", "certifi", "idna"):
        (work / f"{name}.py").write_text(f"open({str(marker)!r}, 'a').write({name!r})\n")
    server = "http://127.0.0.1:9"  # nothing listens: the refresh POST fails
    seed = (
        "import time; from omnigent import cli_auth; "
        f"cli_auth.store_token({server!r}, token='t', user_id='u', "
        "expires_at=time.time() + 3600, refresh_token='r')"
    )
    subprocess.run(
        [sys.executable, "-P", "-c", seed], env=_clean_env(home), check=True, timeout=120
    )
    refused = hashlib.sha256(b"t").hexdigest()

    done = _run_omnigent(
        work, home, "auth", "refresh", "--server", server, "--refused-sha256", refused, "--json"
    )

    assert json.loads(done.stdout)["status"] == "backoff", done.stderr
    assert done.returncode == 11
    assert not marker.exists(), marker.read_text()
    assert not list(home.glob(".omnigent/logs/cli/*")), "auth refresh wrote a CLI log file"
