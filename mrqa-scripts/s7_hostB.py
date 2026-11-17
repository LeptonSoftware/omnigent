import asyncio, sys, json, time, datetime
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
R0 = "192.168.16.3:8000"
async def main():
    async with mr.client() as c:
        hid = await mr.key_for(c, R0)
        hid, scratch, p = mr.start_host("B", hid)
        up = await mr.host_online(c, hid, scratch / "ws"); print("host B on", up)
        sid, aid, _ = await mr.make_session(c, hid, scratch / "ws", f"mrqa-b-{hid[:6]}")
        json.dump({"host_id": hid, "scratch": str(scratch), "pid": p.pid, "sid": sid, "agent_id": aid, "replica": up}, open("/tmp/mrqa-logs/hostB.json", "w"))
        m = (datetime.datetime.utcnow() + datetime.timedelta(minutes=2)).minute
        r = await c.post("/v1/scheduled-tasks", headers={**mr.H, mr.KEY: hid}, json={
            "name": "mrqa-sched-hostB-r0", "prompt": "sched hostB", "rrule": f"FREQ=HOURLY;BYMINUTE={m};BYSECOND=0",
            "agent_id": aid, "host_id": hid, "workspace": str(scratch / "ws")})
        print("create on host B", r.status_code, r.headers.get(mr.UP), r.json().get("id"), "next_run_at", r.json().get("next_run_at"), "minute", m)
        t = json.load(open("/tmp/mrqa-logs/sched_tasks.json")); t["hostB"] = r.json()["id"]; json.dump(t, open("/tmp/mrqa-logs/sched_tasks.json", "w"))
asyncio.run(main())
