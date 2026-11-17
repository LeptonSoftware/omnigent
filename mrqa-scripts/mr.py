"""Shared helpers for mrqa scenarios. Run with the worktree venv, TMPDIR=/tmp."""
from __future__ import annotations
import asyncio, io, json, os, subprocess, sys, tarfile, time, uuid
from pathlib import Path
import httpx, yaml

URL = os.environ.get("MRQA_URL", "http://127.0.0.1:25492")
WT = Path.home() / "omnigent-worktrees/mrqa-live"
PY = str(WT / ".venv/bin/python")
KEY = "X-Databricks-Omnigent-Slice-Key"
UP = "x-omnigent-upstream"
H = {"Origin": "omnigent://internal"}
LLM = "http://127.0.0.1:25491/v1"

def bundle(name: str, *, ask_policy: bool = False) -> bytes:
    conn = {"api_key": "fake", "base_url": LLM}
    os_env = {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}
    spec = {"spec_version": 1, "name": name, "prompt": "You are a test agent.",
            "executor": {"type": "omnigent", "model": "fake-model", "use_responses": False,
                         "config": {"harness": "openai-agents"},
                         "auth": {"type": "api_key", **conn}, "connection": conn},
            "os_env": os_env, "terminals": {"shell": {"command": "bash", "os_env": os_env}}}
    if ask_policy:
        spec["guardrails"] = {"policies": {"ask_tools": {"type": "function", "function": {
            "path": "omnigent.policies.builtins.cel.cel_policy",
            "arguments": {"expression": 'event.type == "tool_call" ? {"result": "ASK", "reason": "mrqa approve?"} : {"result": "ALLOW"}',
                          "reason": "mrqa approve?"}}}}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        p = yaml.safe_dump(spec).encode(); ti = tarfile.TarInfo("config.yaml"); ti.size = len(p)
        tf.addfile(ti, io.BytesIO(p))
    return buf.getvalue()

def start_host(tag: str, host_id: str | None = None, slice_key: bool = True):
    host_id = host_id or uuid.uuid4().hex
    scratch = Path(f"/tmp/mrqa-h-{tag}-{host_id[:6]}"); (scratch / "ws").mkdir(parents=True, exist_ok=True)
    (scratch / "ws" / "marker.txt").write_text(host_id)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_")}
    env.update(HOME=str(scratch / "home"), OMNIGENT_CONFIG_HOME=str(scratch / "cfg"), TMPDIR="/tmp",
               OMNIGENT_HOST_ID=host_id, OMNIGENT_HOST_NAME=f"mrqa-{tag}-{host_id[:6]}",
               OMNIGENT_AUTH_ENABLED="0")
    if slice_key:
        env["OMNIGENT_HOST_SLICE_KEY_ENABLED"] = "1"
    log = open(scratch / "host.log", "w")
    p = subprocess.Popen([PY, "-m", "omnigent", "host", "--server", URL, "--non-interactive"],
                         cwd=scratch, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (scratch / "host.pid").write_text(str(p.pid))
    return host_id, scratch, p

async def wait(check, timeout=90, every=0.5):
    end = time.monotonic() + timeout; last = None
    while time.monotonic() < end:
        try:
            r = await check()
            if r: return r
        except Exception as e:  # noqa
            last = e
        await asyncio.sleep(every)
    raise TimeoutError(f"timeout; last={last!r}")

async def host_online(c: httpx.AsyncClient, host_id: str, ws: Path):
    path = f"/v1/hosts/{host_id}/filesystem/{str(ws).lstrip('/')}"
    async def chk():
        r = await c.get(path, headers={**H, KEY: host_id})
        return r if r.is_success and "marker.txt" in r.text else None
    r = await wait(chk)
    return r.headers[UP]

async def make_session(c, host_id, ws, name, *, ask_policy=False, title="mrqa"):
    r = await c.post("/v1/sessions", headers=H, data={"metadata": "{}"},
                     files={"bundle": ("a.tar.gz", bundle(name, ask_policy=ask_policy), "application/gzip")})
    r.raise_for_status()
    lst = await c.get("/v1/sessions", headers=H, params={"agent_name": name, "limit": 1, "visibility": "all"})
    agent_id = lst.json()["data"][0]["agent_id"]
    r = await c.post("/v1/sessions", headers={**H, KEY: host_id}, json={"agent_id": agent_id, "host_id": host_id,
                     "host_type": "external", "workspace": str(ws), "title": title})
    r.raise_for_status()
    return r.json()["id"], agent_id, r.headers.get(UP)

def msg(text):
    return {"type": "message", "data": {"role": "user", "content": [{"type": "input_text", "text": text}]}}

async def key_for(c, target_upstream: str, exclude: bool = False):
    """Return a random slice key that hashes to (or away from) target_upstream."""
    for _ in range(300):
        k = uuid.uuid4().hex
        r = await c.get("/health", headers={KEY: k})
        if (r.headers[UP] == target_upstream) != exclude:
            return k
    raise RuntimeError("no key found")

def client(**kw):
    return httpx.AsyncClient(base_url=URL, timeout=kw.pop("timeout", 30), trust_env=False, **kw)

def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
