"""Check host-sharded routing through the compose router with a real host.

Adapted from upstream ``deploy/kubernetes/multi_replica/verify.py`` for the
docker-compose layout in this directory. It starts a real host daemon from
this checkout (with ``OMNIGENT_HOST_SLICE_KEY_ENABLED=1``), launches a real
runner on it, and asserts that:

- slice-key hashing reaches every replica;
- the host tunnel, the runner tunnel and every keyed request (header or the
  browser's ``?omnigent_slice_key=``) land on ONE replica;
- the same host request keyed to another replica gets 400 ``wrong_replica``
  (proves ``OMNIGENT_HOST_SHARDED=1`` reached ``create_app``);
- unkeyed session and host lists work on every replica.

The router's access log (``docker logs <router>``) is the ground truth for
which replica served each tunnel. Needs the servers running with auth off
(``OMNIGENT_AUTH_ENABLED=0``); never point it at a real deployment.

    python deploy/lepton/multi_replica/verify.py --url http://127.0.0.1:18000 \
        --router-container omni-mr-router-1 --replicas 2
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[3]
KEY_HEADER = "X-Databricks-Omnigent-Slice-Key"
UPSTREAM_HEADER = "x-omnigent-upstream"


async def eventually(check: Callable[[], Awaitable[Any]], *, timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            result = await check()
            if result:
                return result
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            last = exc
        await asyncio.sleep(0.5)
    raise TimeoutError(f"condition not met in {timeout}s; last error: {last}")


def agent_bundle(name: str) -> bytes:
    connection = {"api_key": "unused", "base_url": "http://127.0.0.1:9/v1"}
    os_env = {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}
    spec = {
        "spec_version": 1,
        "name": name,
        "prompt": "You are a helpful assistant.",
        "executor": {
            "type": "omnigent",
            "model": "unused",
            "config": {"harness": "openai-agents"},
            "auth": {"type": "api_key", **connection},
            "connection": connection,
        },
        "os_env": os_env,
        "terminals": {"shell": {"command": "bash", "os_env": os_env}},
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = yaml.safe_dump(spec).encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def router_log(container: str) -> list[str]:
    result = subprocess.run(
        ["sudo", "-n", "docker", "logs", container],
        capture_output=True,
        text=True,
        check=True,
    )
    return (result.stdout + result.stderr).splitlines()


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, **detail: Any) -> None:
        self.checks.append({"check": name, "ok": ok, **detail})
        print(f"{'PASS' if ok else 'FAIL'} {name} {json.dumps(detail, default=str)}", flush=True)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(entry["ok"] for entry in self.checks)


async def verify(args: argparse.Namespace) -> Report:
    if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("only a local rehearsal stack may be verified")
    report = Report()
    host_id = uuid.uuid4().hex
    marker = uuid.uuid4().hex
    scratch = Path(tempfile.mkdtemp(prefix="omni-mr-verify-"))
    workspace = scratch / "workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text(marker)
    host_log = (scratch / "host.log").open("w")
    host_process: subprocess.Popen[bytes] | None = None
    home = ""
    headers = {"Origin": "omnigent://internal"}
    keyed = {**headers, KEY_HEADER: host_id}
    async with httpx.AsyncClient(base_url=args.url, timeout=10, trust_env=False) as client:
        try:
            await eventually(lambda: client.get("/health"), timeout=120)

            # 1. Hashing reaches every replica.
            backends: dict[str, str] = {}
            for _ in range(64):
                probe = uuid.uuid4().hex
                response = await client.get("/health", headers={KEY_HEADER: probe})
                response.raise_for_status()
                backends.setdefault(response.headers[UPSTREAM_HEADER], probe)
                if len(backends) == args.replicas:
                    break
            report.check(
                "slice keys hash onto every replica",
                len(backends) == args.replicas,
                backends=sorted(backends),
            )

            # 2. A real host with the slice key enabled, isolated HOME.
            env = {
                **os.environ,
                "HOME": str(scratch / "home"),
                "OMNIGENT_HOST_ID": host_id,
                "OMNIGENT_HOST_NAME": f"mr-verify-{host_id[:8]}",
                "OMNIGENT_HOST_SLICE_KEY_ENABLED": "1",
                "OMNIGENT_AUTH_ENABLED": "0",
            }
            env.pop("OMNIGENT_RUNNER_SLICE_KEY", None)
            host_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omnigent",
                    "host",
                    "--server",
                    args.url,
                    "--non-interactive",
                ],
                cwd=scratch,
                env=env,
                stdout=host_log,
                stderr=subprocess.STDOUT,
            )
            # The route takes an absolute host path; FastAPI strips its leading "/".
            host_path = f"/v1/hosts/{host_id}/filesystem/{str(workspace).lstrip('/')}"

            async def host_ready() -> httpx.Response | None:
                if host_process is not None and host_process.poll() is not None:
                    raise RuntimeError(f"host exited; see {scratch / 'host.log'}")
                response = await client.get(host_path, headers=keyed)
                return response if response.is_success and "marker.txt" in response.text else None

            host_response = await eventually(host_ready)
            home = host_response.headers[UPSTREAM_HEADER]
            report.check("host reachable through its key", True, replica=home)

            # 3. The same host request keyed onto another replica is refused.
            other_key = next((key for addr, key in backends.items() if addr != home), None)
            if other_key is not None:
                wrong = await client.get(host_path, headers={**headers, KEY_HEADER: other_key})
                body = wrong.text
                report.check(
                    "other replica answers 400 wrong_replica",
                    wrong.status_code == 400 and "wrong_replica" in body,
                    status=wrong.status_code,
                    replica=wrong.headers.get(UPSTREAM_HEADER),
                    body=body[:200],
                )

            # 4. A real runner on that host.
            name = f"mr-verify-{host_id[:8]}"
            created = await client.post(
                "/v1/sessions",
                headers=headers,
                data={"metadata": "{}"},
                files={"bundle": ("agent.tar.gz", agent_bundle(name), "application/gzip")},
            )
            created.raise_for_status()
            listing = await client.get(
                "/v1/sessions",
                headers=headers,
                params={"agent_name": name, "limit": 1, "visibility": "all"},
            )
            listing.raise_for_status()
            agent_id = listing.json()["data"][0]["agent_id"]
            launched = await client.post(
                "/v1/sessions",
                headers=keyed,
                json={
                    "agent_id": agent_id,
                    "host_id": host_id,
                    "host_type": "external",
                    "workspace": str(workspace),
                    "title": "multi-replica verify",
                },
            )
            launched.raise_for_status()
            session_id = launched.json()["id"]
            report.check(
                "session launch lands on the host's replica",
                launched.headers[UPSTREAM_HEADER] == home,
                replica=launched.headers[UPSTREAM_HEADER],
            )
            file_path = (
                f"/v1/sessions/{session_id}/resources/environments/default/filesystem/marker.txt"
            )

            async def runner_ready() -> httpx.Response | None:
                response = await client.get(file_path, headers=keyed)
                return response if response.is_success and marker in response.text else None

            runner_response = await eventually(runner_ready)
            report.check(
                "runner file read lands on the host's replica",
                runner_response.headers[UPSTREAM_HEADER] == home,
                replica=runner_response.headers[UPSTREAM_HEADER],
            )

            # 5. Browser-style keying via the query parameter, no header.
            browser = await client.get(
                file_path, headers=headers, params={"omnigent_slice_key": host_id}
            )
            report.check(
                "?omnigent_slice_key request lands on the host's replica",
                browser.is_success
                and marker in browser.text
                and browser.headers[UPSTREAM_HEADER] == home,
                status=browser.status_code,
                replica=browser.headers.get(UPSTREAM_HEADER),
            )

            terminal = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                headers=keyed,
                json={"terminal": "shell", "session_key": "verify"},
            )
            terminal.raise_for_status()
            ws_url = (
                args.url.replace("http://", "ws://")
                + f"/v1/sessions/{session_id}/resources/terminals/{terminal.json()['id']}/attach"
                + f"?omnigent_slice_key={host_id}"
            )
            async with connect(ws_url, origin=args.url) as ws:
                await ws.send(f"echo mr-{marker}-ok\n".encode())
                seen = ""
                async with asyncio.timeout(15):
                    while f"mr-{marker}-ok" not in seen.replace(f"echo mr-{marker}-ok", ""):
                        frame = await ws.recv()
                        seen += (
                            frame.decode(errors="replace") if isinstance(frame, bytes) else frame
                        )
            report.check("terminal websocket keyed by query parameter works", True)

            # 6. Unkeyed reads work on every replica.
            served: dict[str, bool] = {}
            for _ in range(32):
                sessions = await client.get(
                    "/v1/sessions", headers=headers, params={"limit": 50, "visibility": "all"}
                )
                hosts = await client.get("/v1/hosts", headers=headers)
                for response in (sessions, hosts):
                    addr = response.headers[UPSTREAM_HEADER]
                    served[addr] = served.get(addr, True) and response.is_success
                ids = {row["id"] for row in sessions.json()["data"]}
                live = {row["host_id"]: row["status"] for row in hosts.json()["hosts"]}
                if session_id not in ids or live.get(host_id) != "online":
                    served[sessions.headers[UPSTREAM_HEADER]] = False
                if len(served) == args.replicas:
                    break
            report.check(
                "unkeyed session and host lists work on every replica",
                len(served) == args.replicas and all(served.values()),
                served=served,
            )

        finally:
            if host_process is not None:
                host_process.terminate()
                try:
                    host_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    host_process.kill()
            host_log.close()
            print(f"host log: {scratch / 'host.log'}", flush=True)
        # 7. Router log, after the host exits: NGINX logs a WebSocket when it
        # closes. Every tunnel for this host must be keyed and on one replica.
        await asyncio.sleep(1)
        lines = [
            line for line in router_log(args.router_container) if re.search(r"/tunnel[ ?]", line)
        ]
        ours = [line for line in lines if f'key="{host_id}"' in line]
        upstreams = {
            match.group(1)
            for line in ours
            if (match := re.search(r"upstream=(\S+)", line)) is not None
        }
        runner_tunnels = [line for line in ours if "/v1/runners/" in line]
        report.check(
            "host and runner tunnels keyed and on the host's replica",
            bool(ours) and bool(runner_tunnels) and upstreams == {home},
            tunnel_requests=len(ours),
            runner_tunnel_requests=len(runner_tunnels),
            upstreams=sorted(upstreams),
            unkeyed_tunnel_requests=sum('key=""' in line for line in lines),
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--router-container", required=True)
    parser.add_argument("--replicas", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(verify(args))
    if args.output is not None:
        args.output.write_text(json.dumps(report.checks, indent=2, default=str))
    print("PASS" if report.ok else "FAIL", flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
