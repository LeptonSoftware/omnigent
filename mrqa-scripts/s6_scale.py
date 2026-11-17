"""Scenario 6: add replica-3 + reload router during active turns + open terminals; then kill a replica mid-turn."""
import asyncio, sys, json, time, subprocess
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
from websockets.asyncio.client import connect
T0 = time.time()
def ts(): return f"{time.time()-T0:6.1f}"
def log(*a): print(ts(), *a, flush=True)
def run(cmd): 
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True); return (r.stdout + r.stderr).strip()
ENV = "set -a; . /tmp/mrqa-stack/env.sh; set +a; cd /tmp/mrqa-stack; "
async def term_watch(name, sid, hid, c, rec):
    r = await c.post(f"/v1/sessions/{sid}/resources/terminals", headers={**mr.H, mr.KEY: hid}, json={"terminal": "shell", "session_key": "mrqa-" + name})
    if not r.is_success: rec["term"] = f"create {r.status_code} {r.text[:100]}"; return
    url = mr.URL.replace("http", "ws") + f"/v1/sessions/{sid}/resources/terminals/{r.json()['id']}/attach?omnigent_slice_key={hid}"
    try:
        async with connect(url, origin=mr.URL) as ws:
            rec["term"] = "open"
            while True:
                await ws.send(b"echo tick\n")
                try:
                    while True: await asyncio.wait_for(ws.recv(), 0.2)
                except asyncio.TimeoutError: pass
                await asyncio.sleep(2)
    except Exception as e:
        rec["term"] = f"CLOSED at {ts()}: {e!r}"[:160]; log(name, "terminal", rec["term"])
async def sse_watch(name, sid, hid, c, rec):
    try:
        async with c.stream("GET", f"/v1/sessions/{sid}/stream", headers={**mr.H, mr.KEY: hid}, timeout=None) as r:
            rec["sse"] = f"open {r.status_code}"; n = 0
            async for line in r.aiter_lines():
                if "output_text.delta" in line: n += 1; rec["deltas"] = n
            rec["sse"] = f"ENDED at {ts()} after {n} deltas"; log(name, "sse", rec["sse"])
    except Exception as e:
        rec["sse"] = f"ERR at {ts()}: {e!r}"[:160]; log(name, "sse", rec["sse"])
async def probe(c, hosts):
    out = {}
    for name, h in hosts.items():
        p = f"/v1/hosts/{h['host_id']}/filesystem/{str(mr.Path(h['scratch'])/'ws').lstrip('/')}"
        try:
            r = await c.get(p, headers={**mr.H, mr.KEY: h["host_id"]}, timeout=5)
            out[name] = f"{r.status_code}@{r.headers.get(mr.UP,'?').split('.')[-1]}"
        except Exception as e: out[name] = type(e).__name__
    return out
async def turn_done(c, sid, word):
    it = await c.get(f"/v1/sessions/{sid}/items", headers=mr.H, params={"limit": 10, "order": "desc"})
    return word in it.text
async def phase(c, hosts, label, action, slow=40):
    recs = {n: {} for n in hosts}; tasks = []
    for n, h in hosts.items():
        tasks.append(asyncio.create_task(term_watch(n, h["sid"], h["host_id"], c, recs[n])))
        tasks.append(asyncio.create_task(sse_watch(n, h["sid"], h["host_id"], c, recs[n])))
    await asyncio.sleep(2)
    for n, h in hosts.items():
        r = await c.post(f"/v1/sessions/{h['sid']}/events", headers={**mr.H, mr.KEY: h["host_id"]}, json=mr.msg(f"slow {slow}"))
        log(n, "post slow", r.status_code)
    await asyncio.sleep(6)
    log("BEFORE", label, await probe(c, hosts))
    log("ACTION", label, await asyncio.to_thread(action))
    for i in range(slow + 25):
        await asyncio.sleep(1)
        if i % 3 == 0: log("probe", await probe(c, hosts), {n: (r.get("term","")[:10], r.get("deltas")) for n, r in recs.items()})
    for n, h in hosts.items():
        s = (await c.get(f"/v1/sessions/{h['sid']}", headers=mr.H)).json()
        log(n, "turn complete:", await turn_done(c, h["sid"], f"w{slow-1}"), "status", s.get("status"), "| term:", recs[n].get("term"), "| sse:", recs[n].get("sse"), "deltas", recs[n].get("deltas"))
    for t in tasks: t.cancel()
    for n, h in hosts.items():
        r = await c.post(f"/v1/sessions/{h['sid']}/events", headers={**mr.H, mr.KEY: h["host_id"]}, json=mr.msg("hello after"))
        ok = False
        if r.is_success:
            try: await mr.wait(lambda: turn_done(c, h["sid"], "pong"), timeout=30); ok = True
            except TimeoutError: pass
        log(n, "follow-up message:", r.status_code, r.text[:100] if not r.is_success else "", "replied:", ok)
async def main():
    hosts = {"A": json.load(open("/tmp/mrqa-logs/hostA.json")), "B": json.load(open("/tmp/mrqa-logs/hostB.json"))}
    async with mr.client(timeout=30) as c:
        for tag in ("C", "D", "E", "F"):
            f = mr.Path(f"/tmp/mrqa-logs/host{tag}.json")
            if f.exists():
                hosts[tag] = json.load(open(f)); continue
            hid, scratch, p = mr.start_host(tag)
            up = await mr.host_online(c, hid, scratch / "ws")
            sid, aid, _ = await mr.make_session(c, hid, scratch / "ws", f"mrqa-{tag.lower()}-{hid[:6]}")
            hosts[tag] = {"host_id": hid, "scratch": str(scratch), "pid": p.pid, "sid": sid, "agent_id": aid, "replica": up}
            json.dump(hosts[tag], open(f"/tmp/mrqa-logs/host{tag}.json", "w"))
        for n, h in hosts.items():
            h["replica"] = await mr.host_online(c, h["host_id"], mr.Path(h["scratch"]) / "ws")
        log("hosts", {n: h["replica"] for n, h in hosts.items()})
        mode = sys.argv[1]
        if mode == "scale":
            def add():
                r1 = run(ENV + "sudo -nE docker compose --profile scale4 up -d replica-3 2>&1 | tail -2")
                for _ in range(90):
                    if "(healthy)" in run("sudo -n docker ps --format '{{.Status}}' --filter name=mrqa-replica-3"): break
                    time.sleep(2)
                run("python3 -c \"p='/tmp/mrqa-stack/upstreams.conf';s=open(p).read();open(p,'w').write(s.replace('    server replica-2:8000 resolve max_fails=0;\\n','    server replica-2:8000 resolve max_fails=0;\\n    server replica-3:8000 resolve max_fails=0;\\n'))\"")
                r2 = run("sudo -n docker exec mrqa-router-1 nginx -s reload")
                return f"replica-3 up ({r1}); reload: {r2 or 'ok'} at {ts()}"
            await phase(c, hosts, "scale-to-4", add)
        elif mode == "reload":
            await phase(c, hosts, "reload", lambda: run("sudo -n docker exec mrqa-router-1 nginx -s reload") + f" at {ts()}", slow=int(sys.argv[2]) if len(sys.argv) > 2 else 40)
        else:
            victim = sys.argv[2]
            await phase(c, hosts, f"kill {victim}", lambda: run(f"sudo -n docker kill {victim}") + f" at {ts()}")
            log("victim state", run(f"sudo -n docker ps -a --format '{{{{.Names}}}} {{{{.Status}}}}' --filter name={victim}"))
asyncio.run(main())
