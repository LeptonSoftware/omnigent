import asyncio, sys, json, time
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main(text, keyed=True, wait_for="pong", timeout=60):
    st = json.load(open("/tmp/mrqa-logs/hostA.json")); sid, hid = st["sid"], st["host_id"]
    async with mr.client() as c:
        h = {**mr.H, mr.KEY: hid} if keyed else mr.H
        r = await c.post(f"/v1/sessions/{sid}/events", headers=h, json=mr.msg(text))
        print("post", r.status_code, r.headers.get(mr.UP), r.text[:200])
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = (await c.get(f"/v1/sessions/{sid}", headers=mr.H)).json()
            it = await c.get(f"/v1/sessions/{sid}/items", headers=mr.H, params={"limit": 5, "order": "desc"})
            if wait_for in it.text: print("REPLY OK", round(time.time()-t0,1), "status", s.get("status")); return
            await asyncio.sleep(1)
        print("no reply; status", s.get("status"), it.text[-600:])
asyncio.run(main(sys.argv[1]))
