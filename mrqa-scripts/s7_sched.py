import asyncio, sys, json, time
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
REPS = {"r0": "192.168.16.3:8000", "r1": "192.168.16.4:8000", "r2": "192.168.16.5:8000"}
async def main():
    st = json.load(open("/tmp/mrqa-logs/hostA.json")); hid, aid = st["host_id"], st["agent_id"]
    ws = str(mr.Path(st["scratch"]) / "ws")
    tasks = {}
    async with mr.client() as c:
        keys = {n: await mr.key_for(c, up) for n, up in REPS.items()}
        for n in REPS:
            r = await c.post("/v1/scheduled-tasks", headers={**mr.H, mr.KEY: keys[n]}, json={
                "name": f"mrqa-sched-via-{n}", "prompt": f"sched {n}", "rrule": "FREQ=HOURLY;BYMINUTE=33;BYSECOND=0",
                "agent_id": aid, "host_id": hid, "workspace": ws})
            print("create via", n, r.status_code, r.headers.get(mr.UP), r.text[:300])
            if r.is_success: tasks[n] = r.json()["id"]
        # browser-style: keyed by body host_id
        r = await c.post("/v1/scheduled-tasks", headers={**mr.H, mr.KEY: hid}, json={
            "name": "mrqa-sched-browserstyle", "prompt": "sched browser", "rrule": "FREQ=HOURLY;BYMINUTE=33;BYSECOND=0",
            "agent_id": aid, "host_id": hid, "workspace": ws})
        print("create browser-style (key=host)", r.status_code, r.headers.get(mr.UP), r.text[:200])
        if r.is_success: tasks["browser"] = r.json()["id"]
        json.dump(tasks, open("/tmp/mrqa-logs/sched_tasks.json", "w"))
        for n, tid in tasks.items():
            for rn in REPS:
                g = await c.get(f"/v1/scheduled-tasks/{tid}", headers={**mr.H, mr.KEY: keys[rn]})
                print(f"  task {n} read on {rn}: next_run_at={g.json().get('next_run_at')}")
asyncio.run(main())
