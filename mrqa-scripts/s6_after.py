import asyncio, sys, json, time
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main():
    async with mr.client(timeout=30) as c:
        for tag in ("A", "D"):
            h = json.load(open(f"/tmp/mrqa-logs/host{tag}.json")); t0 = time.time()
            try:
                up = await mr.host_online(c, h["host_id"], mr.Path(h["scratch"]) / "ws"); print(tag, "host back online via", up, f"after {time.time()-t0:.1f}s")
            except TimeoutError as e: print(tag, "host NOT back", e)
            s = (await c.get(f"/v1/sessions/{h['sid']}", headers={**mr.H, mr.KEY: h["host_id"]})).json()
            it = await c.get(f"/v1/sessions/{h['sid']}/items", headers=mr.H, params={"limit": 6, "order": "desc"})
            texts = [ (i.get("type"), str(i.get("content") or i.get("status"))[:80]) for i in it.json()["data"]]
            print(tag, "session status", s.get("status"), "last items", texts[:4])
            r = await c.post(f"/v1/sessions/{h['sid']}/events", headers={**mr.H, mr.KEY: h["host_id"]}, json=mr.msg("hello after kill"))
            print(tag, "follow-up post", r.status_code, r.text[:150])
            ok=False
            for _ in range(40):
                it = await c.get(f"/v1/sessions/{h['sid']}/items", headers=mr.H, params={"limit": 3, "order": "desc"})
                if "pong" in it.text and "hello after kill" in (await c.get(f"/v1/sessions/{h['sid']}/items", headers=mr.H, params={"limit": 6, "order": "desc"})).text: ok=True; break
                await asyncio.sleep(1)
            print(tag, "follow-up replied:", ok, "status", (await c.get(f"/v1/sessions/{h['sid']}", headers=mr.H)).json().get("status"))
asyncio.run(main())
