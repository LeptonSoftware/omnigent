import asyncio, sys, json, time, collections
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main():
    st = json.load(open("/tmp/mrqa-logs/hostA.json")); sid, hid = st["sid"], st["host_id"]
    res = []
    async with mr.client() as c:
        for i in range(9):
            tag = f"unkeyed-{int(time.time())}-{i}"
            r = await c.post(f"/v1/sessions/{sid}/events", headers=mr.H, json=mr.msg(tag))
            res.append((tag, r.status_code, r.headers.get(mr.UP), r.text[:160]))
            print(tag, r.status_code, r.headers.get(mr.UP), r.text[:160], flush=True)
            await asyncio.sleep(3)
        await asyncio.sleep(3)
        it = await c.get(f"/v1/sessions/{sid}/items", headers=mr.H, params={"limit": 100, "order": "desc"})
        text = it.text
        for tag, code, up, body in res:
            print(f"{tag}: http={code} via={up} persisted={tag in text}")
        # also the CLI-style path: does `omnigent` client have a slice key? check runner->server: n/a
        s = (await c.get(f"/v1/sessions/{sid}", headers=mr.H)).json(); print("session status", s.get("status"))
asyncio.run(main())
