import asyncio, sys, json, time
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
from websockets.asyncio.client import connect
REPS = {"r0": "192.168.16.3:8000", "r1": "192.168.16.4:8000", "r2": "192.168.16.5:8000"}
T0 = time.time()
def ts(): return round(time.time() - T0, 2)
async def sse(c, name, sid, headers, params, out):
    try:
        async with c.stream("GET", f"/v1/sessions/{sid}/stream", headers=headers, params=params, timeout=None) as r:
            out[name] = {"status": r.status_code, "via": r.headers.get(mr.UP), "events": [], "first_delta": None}
            if r.status_code != 200:
                out[name]["body"] = (await r.aread()).decode()[:200]; return
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip(); out[name]["events"].append(ev)
                    if "delta" in ev and out[name]["first_delta"] is None: out[name]["first_delta"] = ts()
    except Exception as e:
        out.setdefault(name, {})["exc"] = repr(e)
async def upd(name, key, sids, out):
    url = mr.URL.replace("http", "ws") + "/v1/sessions/updates" + (f"?omnigent_slice_key={key}" if key else "")
    rec = out[name] = {"frames": []}
    try:
        async with connect(url, origin=mr.URL) as ws:
            await ws.send(json.dumps({"type": "watch", "session_ids": sids}))
            while True:
                f = json.loads(await ws.recv())
                if f["type"] == "heartbeat": continue
                items = [(i.get("id"), i.get("status")) for i in f.get("items", [])]
                rec["frames"].append((ts(), f["type"], items))
    except Exception as e:
        rec["exc"] = repr(e)
async def main():
    st = json.load(open("/tmp/mrqa-logs/hostA.json")); sid, hid = st["sid"], st["host_id"]
    out = {}
    async with mr.client(timeout=None) as c:
        keys = {n: await mr.key_for(c, up) for n, up in REPS.items()}
        tasks = []
        tasks.append(asyncio.create_task(sse(c, "sse_keyed_header", sid, {**mr.H, mr.KEY: hid}, {}, out)))
        tasks.append(asyncio.create_task(sse(c, "sse_keyed_query", sid, mr.H, {"omnigent_slice_key": hid}, out)))
        for n in ("r0", "r2"):
            tasks.append(asyncio.create_task(sse(c, f"sse_pinned_{n}", sid, {**mr.H, mr.KEY: keys[n]}, {}, out)))
        for i in range(3):
            tasks.append(asyncio.create_task(sse(c, f"sse_unkeyed_{i}", sid, mr.H, {}, out)))
        for n in REPS:
            tasks.append(asyncio.create_task(upd(f"upd_{n}", keys[n], [sid], out)))
        await asyncio.sleep(3)
        print(ts(), "posting slow 6", flush=True)
        r = await c.post(f"/v1/sessions/{sid}/events", headers={**mr.H, mr.KEY: hid}, json=mr.msg("slow 6"))
        print(ts(), "post", r.status_code, r.headers.get(mr.UP))
        await asyncio.sleep(12)
        # discovery: create a NEW session on host A via its replica; who learns of it?
        print(ts(), "creating new session")
        nsid, _, nup = await mr.make_session(c, hid, mr.Path(st["scratch"]) / "ws", f"mrqa-a-{hid[:6]}", title="mrqa-discovery")
        print(ts(), "new session", nsid, "created on", nup)
        await asyncio.sleep(10)
        for t in tasks: t.cancel()
    for k, v in out.items():
        if k.startswith("sse"):
            ev = v.get("events", [])
            print(k, "status", v.get("status"), "via", v.get("via"), "n_events", len(ev), "first_delta", v.get("first_delta"), "kinds", sorted(set(ev))[:8], v.get("body", ""), v.get("exc", ""))
        else:
            print(k, v.get("exc", ""))
            for f in v["frames"]: print("   ", f)
asyncio.run(main())
