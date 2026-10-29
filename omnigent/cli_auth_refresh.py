"""fork: ``omnigent auth refresh``, the login refresh for clients in other languages.

A client that shares ``auth_tokens.json`` but is not Python (jarvis-cli) must
not refresh on its own: its POSTs would bypass the machine-wide lock, backoff
and refused-token memory. It calls this instead and re-reads the token file.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sys
from typing import NoReturn

import click

from omnigent import cli_auth

EXIT_OK = 0
EXIT_ERROR = 1
# Not 2: click and `omnigent` exit 2 on usage errors.
EXIT_DEAD = 10
EXIT_BACKOFF = 11
_EXIT_CODES = {"ok": EXIT_OK, "dead": EXIT_DEAD, "backoff": EXIT_BACKOFF, "error": EXIT_ERROR}
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")


@click.group("auth")
def auth_group() -> None:
    """Work with the stored login of an Omnigent server."""


@auth_group.command(
    "refresh",
    epilog=(
        "\b\n"
        "Exit codes:\n"
        "   0  the stored token is usable (refreshed, or already newer)\n"
        "   1  any other error\n"
        "   2  usage error (click)\n"
        "  10  the login is dead: run `omnigent login <url>`\n"
        "  11  backing off: stderr's last line is `retry_after=<seconds>`\n"
        "\b\n"
        'With --json, stdout is {"status": "ok|dead|backoff|error", "retry_after": n};\n'
        "the status is authoritative. Logs go to stderr, never to a file.\n"
        "\b\n"
        "omnigent does not add the current directory to the import path for\n"
        "`auth`; run it with PYTHONSAFEPATH=1 and a clean PYTHONPATH when\n"
        "calling it from code, as jarvis does.\n"
        "No token is ever printed: re-read auth_tokens.json after exit 0."
    ),
)
@click.option(
    "--server",
    "server_url",
    required=True,
    metavar="URL",
    help="The server whose stored login to refresh, e.g. https://agents.example.com.",
)
@click.option(
    "--refused-sha256",
    default=None,
    metavar="HEX",
    help="SHA-256 hex of the access token the server just refused.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the outcome as JSON on stdout.")
def refresh_command(server_url: str, refused_sha256: str | None, as_json: bool) -> None:
    """Refresh the stored login, sharing this machine's lock and backoff.

    Use this instead of POSTing /oauth/token yourself. At most one refresh
    per refused token goes out on this machine, whoever asks.
    """
    if refused_sha256 is not None and not _SHA256_HEX.fullmatch(refused_sha256):
        bad_digest = "--refused-sha256 must be 64 hex digits"
        _finish(cli_auth.RefreshResult(None, "error"), as_json, bad_digest)
    try:
        # stdout carries only the --json line, whatever the logging setup.
        with contextlib.redirect_stdout(sys.stderr):
            result = cli_auth.refresh_login(
                server_url, refused_sha256=refused_sha256.lower() if refused_sha256 else None
            )
    except Exception as exc:  # noqa: BLE001 — every failure must map to exit 1
        _finish(cli_auth.RefreshResult(None, "error"), as_json, f"refresh failed: {exc}")
    _finish(result, as_json, None)


def _finish(result: cli_auth.RefreshResult, as_json: bool, detail: str | None) -> NoReturn:
    """Print the outcome and exit with its code."""
    retry_after = math.ceil(result.retry_after) if result.retry_after > 0 else 0
    if as_json:
        click.echo(json.dumps({"status": result.status, "retry_after": retry_after}))
    if result.status == "dead":
        click.echo("The stored login is dead: run `omnigent login`.", err=True)
    elif result.status == "backoff":
        click.echo(f"retry_after={retry_after}", err=True)
    elif result.status == "error":
        click.echo(detail or result.detail or "The refresh failed.", err=True)
    raise SystemExit(_EXIT_CODES[result.status])
