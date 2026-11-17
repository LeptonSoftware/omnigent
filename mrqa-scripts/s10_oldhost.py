import asyncio, sys, json, time, collections
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main():
    async with mr.client() as c:
        hid, scratch, p = mr.start_host("G", slice_key=False)
        json.dump({"host_id": hid, "scratch": str(scratch), "pid": p.pid}, open("/tmp/mrqa-logs/hostG.json", "w"))
        path = f"/v1/hosts/{hid}/filesystem/{str(scratch/'ws').lstrip('/')}"
        await asyncio.sleep(8)
        for label, h in (("keyed (browser)", {**mr.H, mr.KEY: hid}), ("unkeyed", mr.H)):
            cnt = collections.Counter()
            for _ in range(12):
                r = await c.get(path, headers=h); cnt[(r.status_code, r.headers.get(mr.UP))] += 1
            print("host without slice key,", label, dict(cnt))
asyncio.run(main())
