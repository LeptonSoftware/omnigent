import asyncio, sys, json, time
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
R1 = "192.168.16.4:8000"
async def main():
    async with mr.client() as c:
        hid = await mr.key_for(c, R1)
        hid, scratch, p = mr.start_host("A", hid)
        print("host", hid, "pid", p.pid, scratch)
        up = await mr.host_online(c, hid, scratch / "ws"); print("host replica", up)
        sid, aid, up2 = await mr.make_session(c, hid, scratch / "ws", f"mrqa-a-{hid[:6]}")
        print("session", sid, "agent", aid, "created on", up2)
        r = await c.post(f"/v1/sessions/{sid}/events", headers={**mr.H, mr.KEY: hid}, json=mr.msg("hello"))
        print("post", r.status_code, r.headers.get(mr.UP), r.text[:200])
        t0 = time.time()
        while time.time() - t0 < 60:
            s = await c.get(f"/v1/sessions/{sid}", headers=mr.H); j = s.json()
            it = await c.get(f"/v1/sessions/{sid}/items", headers=mr.H, params={"limit": 20})
            if it.status_code == 200 and "pong" in it.text: print("REPLY OK after", round(time.time()-t0,1), "status", j.get("status")); break
            await asyncio.sleep(1)
        else: print("no reply; status", j.get("status"), it.status_code, it.text[:400])
        json.dump({"host_id": hid, "scratch": str(scratch), "pid": p.pid, "sid": sid, "agent_id": aid, "replica": up}, open("/tmp/mrqa-logs/hostA.json", "w"))
asyncio.run(main())
